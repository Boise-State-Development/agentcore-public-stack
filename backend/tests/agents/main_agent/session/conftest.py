"""
Shared fixtures for session submodule tests.

Provides moto-backed DynamoDB table with SessionLookupIndex GSI,
mock AgentCore Memory components, and message builder helpers.
"""

import json
import threading
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from agents.main_agent.session.compaction_models import CompactionConfig, CompactionState


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TABLE_NAME = "test-sessions-metadata"
REGION = "us-east-1"
TEST_SESSION_ID = "session-test-001"
TEST_USER_ID = "user-test-001"
TEST_MEMORY_ID = "mem-test-001"
TEST_ACTOR_ID = "actor-test-001"


# ---------------------------------------------------------------------------
# Message builder helpers
# ---------------------------------------------------------------------------

def make_user_message(text: str) -> dict:
    return {"role": "user", "content": [{"text": text}]}


def make_assistant_message(text: str) -> dict:
    return {"role": "assistant", "content": [{"text": text}]}


def make_tool_use_message(tool_id: str, name: str, tool_input: dict) -> dict:
    return {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": tool_id, "name": name, "input": tool_input}}],
    }


def make_tool_result_message(tool_id: str, text: str) -> dict:
    return {
        "role": "user",
        "content": [{"toolResult": {"toolUseId": tool_id, "content": [{"text": text}]}}],
    }


def make_tool_result_json_message(tool_id: str, json_content: dict) -> dict:
    return {
        "role": "user",
        "content": [{"toolResult": {"toolUseId": tool_id, "content": [{"json": json_content}]}}],
    }


def make_tool_result_image_message(
    tool_id: str, image_bytes: bytes = b"fake-img", fmt: str = "png"
) -> dict:
    return {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": tool_id,
                    "content": [{"image": {"format": fmt, "source": {"bytes": image_bytes}}}],
                }
            }
        ],
    }


def make_image_message(image_bytes: bytes = b"fake-img", fmt: str = "png") -> dict:
    return {
        "role": "user",
        "content": [{"image": {"format": fmt, "source": {"bytes": image_bytes}}}],
    }


def make_tool_use_string_input_message(tool_id: str, name: str, string_input: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": tool_id, "name": name, "input": string_input}}],
    }


def make_conversation(num_turns: int) -> list:
    """Build a multi-turn conversation: [user, assistant, user, assistant, ...]."""
    messages = []
    for i in range(num_turns):
        messages.append(make_user_message(f"Question {i}"))
        messages.append(make_assistant_message(f"Answer {i}"))
    return messages


# ---------------------------------------------------------------------------
# Mock AgentCore Memory config
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_agentcore_config():
    config = MagicMock()
    config.session_id = TEST_SESSION_ID
    config.memory_id = TEST_MEMORY_ID
    config.actor_id = TEST_ACTOR_ID
    config.batch_size = 1
    config.flush_interval_seconds = None
    return config


# ---------------------------------------------------------------------------
# Compaction config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def compaction_config() -> CompactionConfig:
    return CompactionConfig(
        enabled=True,
        token_threshold=1000,
        protected_turns=3,
        max_tool_content_length=50,
    )


@pytest.fixture
def compaction_config_disabled() -> CompactionConfig:
    return CompactionConfig(enabled=False)


# ---------------------------------------------------------------------------
# Moto DynamoDB table with SessionLookupIndex GSI
# ---------------------------------------------------------------------------

@pytest.fixture
def dynamodb_sessions_table():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI_PK", "AttributeType": "S"},
                {"AttributeName": "GSI_SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "SessionLookupIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName=TABLE_NAME)
        yield table


def seed_session_record(table, session_id: str, user_id: str, compaction=None) -> dict:
    """Insert a session metadata record into the moto table."""
    item = {
        "PK": f"USER#{user_id}",
        "SK": f"S#ACTIVE#2026-03-08T00:00:00Z#{session_id}",
        "GSI_PK": f"SESSION#{session_id}",
        "GSI_SK": "META",
        "userId": user_id,
        "sessionId": session_id,
    }
    if compaction is not None:
        item["compaction"] = compaction
    table.put_item(Item=item)
    return item


# ---------------------------------------------------------------------------
# Mock parent __init__ — sets required attributes without AWS calls
# ---------------------------------------------------------------------------

def _mock_parent_init(config):
    """Return a replacement __init__ for AgentCoreMemorySessionManager."""
    def _init(self, agentcore_memory_config=None, region_name=None, **kwargs):
        cfg = agentcore_memory_config or config
        self.config = cfg
        self.memory_client = MagicMock()
        self.session_repository = self
        self.session_id = cfg.session_id
        self._is_new_session = True
        self.session = MagicMock()
        self._latest_agent_message = {}
        self._last_synced_internal_state = {}
        self.has_existing_agent = False
        self.converter = MagicMock()
        self._message_buffer = []
        self._message_lock = threading.Lock()
        self._agent_state_buffer = []
        self._agent_state_lock = threading.Lock()
        self._agent_created_at_cache = {}
        self._flush_timer = None
        self._timer_lock = threading.Lock()
        self._shutdown = False
    return _init


# ---------------------------------------------------------------------------
# TurnBasedSessionManager factory fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def make_session_manager(mock_agentcore_config):
    """
    Factory fixture — returns a callable that creates a TurnBasedSessionManager
    with AgentCoreMemorySessionManager.__init__ mocked out so no AWS calls are made.

    The resulting manager inherits all TurnBasedSessionManager methods and has
    the parent's required attributes set via the mock __init__.
    """
    def _factory(compaction_config=None, user_id=TEST_USER_ID, **kwargs):
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )
        from agents.main_agent.session.turn_based_session_manager import TurnBasedSessionManager

        # Reset class-level state between tests
        TurnBasedSessionManager._dynamodb_table = None
        TurnBasedSessionManager._dynamodb_table_name = None

        with patch.object(
            AgentCoreMemorySessionManager,
            "__init__",
            _mock_parent_init(mock_agentcore_config),
        ):
            mgr = TurnBasedSessionManager(
                agentcore_memory_config=mock_agentcore_config,
                region_name=REGION,
                compaction_config=compaction_config,
                user_id=user_id,
                **kwargs,
            )

        # Set up mock methods for session repository operations
        # (These are inherited from AgentCoreMemorySessionManager and called via self)
        mgr.read_agent = MagicMock(return_value=None)
        mgr.list_messages = MagicMock(return_value=[])
        mgr.create_agent = MagicMock()
        mgr.create_message = MagicMock()

        return mgr

    return _factory
