"""Tests for the voice-availability backfill (#625).

What matters: only the listed tools are turned on, nothing is turned off,
``updatedAt`` is untouched, and a dry run writes nothing.
"""

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backfill_voice_tool_flags import VOICE_TOOL_IDS, backfill, list_flags  # noqa: E402

REGION = "us-east-1"
TABLE = "test-app-roles"


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _tool(table, tool_id, **attrs):
    table.put_item(Item={"PK": f"TOOL#{tool_id}", "SK": "METADATA", "toolId": tool_id,
                         "updatedAt": "2026-01-01T00:00:00Z", **attrs})


def _flag(table, tool_id):
    return table.get_item(Key={"PK": f"TOOL#{tool_id}", "SK": "METADATA"})["Item"].get("voiceAvailable")


def test_sets_only_the_listed_tools(table):
    _tool(table, "calculator")
    _tool(table, "browse_web")
    table.put_item(Item={"PK": "ROLE#student", "SK": "METADATA", "roleId": "student"})

    stats = backfill(table, ["calculator", "not_here"], apply=True)

    assert _flag(table, "calculator") is True
    assert _flag(table, "browse_web") is None
    assert stats == {"listed": 2, "already": 0, "set": 1, "missing": 1, "failed": 0}
    row = table.get_item(Key={"PK": "TOOL#calculator", "SK": "METADATA"})["Item"]
    assert row["updatedAt"] == "2026-01-01T00:00:00Z"


def test_a_dry_run_writes_nothing(table):
    _tool(table, "calculator")
    assert backfill(table, ["calculator"], apply=False)["set"] == 1
    assert _flag(table, "calculator") is None


def test_is_idempotent_and_never_turns_a_tool_off(table):
    _tool(table, "calculator", voiceAvailable=True)
    _tool(table, "gateway_brave_search", voiceAvailable=True)  # an admin's own choice

    stats = backfill(table, ["calculator"], apply=True)

    assert stats["already"] == 1 and stats["set"] == 0
    assert _flag(table, "gateway_brave_search") is True


def test_list_shows_every_tools_flag(table):
    _tool(table, "calculator", voiceAvailable=True, displayName="Calculator")
    _tool(table, "browse_web", displayName="Web Browser")
    lines = list_flags(table)
    assert lines[0].startswith("-") and "browse_web" in lines[0]
    assert lines[1].startswith("voice") and "calculator" in lines[1]


def test_the_list_leaves_out_slow_and_visual_tools():
    for tool_id in ("browse_web", "generate_diagram_and_validate", "create_visualization",
                    "fetch_url_content", "ask_user_question", "create_artifact"):
        assert tool_id not in VOICE_TOOL_IDS
    assert "calculator" in VOICE_TOOL_IDS
