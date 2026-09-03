"""
Bedrock prompt-cache resilience — three cachePoints per request.

A message-level cache lookup can miss structurally: Anthropic's cache lookback
checks only ~20 content blocks behind the breakpoint, so a wide parallel tool
fan-out pushes the previous checkpoint out of range (prod session aecd387d:
cacheRead=0, cacheWrite=134k mid-turn). Dedicated cachePoints on toolConfig and
the system prompt keep the stable prefix readable from cache on those turns.

Contract under test (see ModelConfig.to_bedrock_config comment):
  1. toolConfig.tools tail  — via cache_tools="default"
  2. system tail            — via SystemContentBlock list from AgentFactory
  3. last user message tail — via CacheConfig(strategy="auto")
Bedrock allows max 4 cachePoints per request; nothing else may add one, so the
formatted request must contain exactly 3.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.main_agent.core.model_config import ModelConfig, ModelProvider

CLAUDE_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _count_cache_points(node) -> int:
    """Count every cachePoint block anywhere in a formatted request."""
    if isinstance(node, dict):
        return sum(_count_cache_points(v) for v in node.values()) + (
            1 if "cachePoint" in node else 0
        )
    if isinstance(node, list):
        return sum(_count_cache_points(item) for item in node)
    return 0


# ---------------------------------------------------------------------------
# ModelConfig: cache_tools + support predicate
# ---------------------------------------------------------------------------
class TestCacheToolsConfig:
    def test_cache_tools_set_for_claude_with_caching(self):
        config = ModelConfig(model_id=CLAUDE_MODEL_ID, caching_enabled=True)
        assert config.to_bedrock_config()["cache_tools"] == "default"

    def test_no_cache_tools_when_caching_disabled(self):
        config = ModelConfig(model_id=CLAUDE_MODEL_ID, caching_enabled=False)
        assert "cache_tools" not in config.to_bedrock_config()

    def test_no_cache_tools_for_non_anthropic_bedrock_model(self):
        """A model Strands' auto strategy would no-op on must not get explicit
        cachePoints either — Bedrock would reject them with ValidationException."""
        config = ModelConfig(model_id="amazon.nova-pro-v1:0", caching_enabled=True)
        assert "cache_tools" not in config.to_bedrock_config()

    def test_support_predicate_false_for_non_bedrock_provider(self):
        config = ModelConfig(
            model_id="gpt-4o", provider=ModelProvider.OPENAI, caching_enabled=True
        )
        assert config.bedrock_cache_points_supported() is False


# ---------------------------------------------------------------------------
# AgentFactory: system prompt wrapped as SystemContentBlock list
# ---------------------------------------------------------------------------
class TestFactorySystemPromptCachePoint:
    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.CountTokensBedrockModel")
    def test_system_prompt_gets_trailing_cache_point(self, _mock_model, mock_agent_cls):
        from agents.main_agent.core.agent_factory import AgentFactory

        AgentFactory.create_agent(
            model_config=ModelConfig(model_id=CLAUDE_MODEL_ID, caching_enabled=True),
            system_prompt="You are a helpful assistant.",
            tools=[],
            session_manager=MagicMock(),
        )

        assert mock_agent_cls.call_args.kwargs["system_prompt"] == [
            {"text": "You are a helpful assistant."},
            {"cachePoint": {"type": "default"}},
        ]

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.CountTokensBedrockModel")
    def test_plain_string_when_caching_disabled(self, _mock_model, mock_agent_cls):
        from agents.main_agent.core.agent_factory import AgentFactory

        AgentFactory.create_agent(
            model_config=ModelConfig(model_id=CLAUDE_MODEL_ID, caching_enabled=False),
            system_prompt="You are a helpful assistant.",
            tools=[],
            session_manager=MagicMock(),
        )

        assert (
            mock_agent_cls.call_args.kwargs["system_prompt"]
            == "You are a helpful assistant."
        )

    @patch("agents.main_agent.core.agent_factory.Agent")
    @patch("agents.main_agent.core.agent_factory.CountTokensBedrockModel")
    def test_empty_prompt_never_wrapped(self, _mock_model, mock_agent_cls):
        """Bedrock rejects a cachePoint with no preceding content."""
        from agents.main_agent.core.agent_factory import AgentFactory

        AgentFactory.create_agent(
            model_config=ModelConfig(model_id=CLAUDE_MODEL_ID, caching_enabled=True),
            system_prompt="",
            tools=[],
            session_manager=MagicMock(),
        )

        assert mock_agent_cls.call_args.kwargs["system_prompt"] == ""


# ---------------------------------------------------------------------------
# End-to-end: the formatted ConverseStream request
# ---------------------------------------------------------------------------
class TestFormattedRequestCachePoints:
    @pytest.fixture
    def model(self, monkeypatch):
        """Real Strands BedrockModel built from our production config path.

        boto3 client construction needs a region but no credentials, and
        format_request never touches the network.
        """
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel

        config = ModelConfig(model_id=CLAUDE_MODEL_ID, caching_enabled=True)
        return CountTokensBedrockModel(**config.to_bedrock_config())

    @pytest.fixture
    def request_parts(self, model):
        """Format a fan-out-shaped conversation and return the request."""
        system_prompt_content = [
            {"text": "You are a helpful assistant."},
            {"cachePoint": {"type": "default"}},
        ]
        tool_specs = [
            {
                "name": "get_thread",
                "description": "Fetch a thread",
                "inputSchema": {"json": {"type": "object", "properties": {}}},
            }
        ]
        messages = [
            {
                "role": "user",
                # Stale message-level point from the previous turn — auto
                # strategy must strip it (it manages message points itself).
                "content": [{"text": "first turn"}, {"cachePoint": {"type": "default"}}],
            },
            {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "t1", "name": "get_thread", "input": {}}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "t1",
                            "content": [{"text": "thread body"}],
                            "status": "success",
                        }
                    }
                ],
            },
        ]
        return model.format_request(
            messages, tool_specs, system_prompt_content=system_prompt_content
        )

    def test_tool_config_tail_is_cache_point(self, request_parts):
        assert request_parts["toolConfig"]["tools"][-1] == {
            "cachePoint": {"type": "default"}
        }

    def test_system_tail_is_cache_point(self, request_parts):
        assert request_parts["system"][-1] == {"cachePoint": {"type": "default"}}
        # Strands' auto strategy strips only message-level points — the
        # system point must survive.
        assert request_parts["system"][0] == {"text": "You are a helpful assistant."}

    def test_last_user_message_tail_is_cache_point(self, request_parts):
        last_user = [m for m in request_parts["messages"] if m["role"] == "user"][-1]
        assert last_user["content"][-1] == {"cachePoint": {"type": "default"}}

    def test_stale_message_cache_point_stripped(self, request_parts):
        first_msg_blocks = request_parts["messages"][0]["content"]
        assert all("cachePoint" not in block for block in first_msg_blocks)

    def test_exactly_three_cache_points_total(self, request_parts):
        """Bedrock's hard limit is 4 cachePoints per request; we budget 3
        (tools, system, auto message point). If this fails at >3, something
        new started adding cachePoints — rebalance the budget before shipping."""
        assert _count_cache_points(request_parts) == 3, json.dumps(
            request_parts, default=str, indent=2
        )

    def test_caching_disabled_yields_zero_cache_points(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel

        config = ModelConfig(model_id=CLAUDE_MODEL_ID, caching_enabled=False)
        model = CountTokensBedrockModel(**config.to_bedrock_config())
        request = model.format_request(
            [{"role": "user", "content": [{"text": "hi"}]}],
            None,
            system_prompt_content=[{"text": "You are a helpful assistant."}],
        )
        assert _count_cache_points(request) == 0


class TestSteeringInjectionDoesNotDisturbCachePoints:
    """A mid-turn steering injection rides the tool-result message.

    Mid-turn steering (docs/specs/mid-turn-steering.md) appends the user's
    words as a ``{"text": ...}`` block to the same user-role message that
    carries the tool results, so a steered turn's history ends on a *mixed*
    ``toolResult`` + ``text`` message. The cost claim in the spec rests on that
    injection being append-only against the cached prefix: it must land inside
    the segment the ``strategy="auto"`` message point already covers, behind
    both static points, so the next call still reads the stable prefix from
    cache rather than rewriting it.

    These lock the placement. A regression here is a prompt-cache **cost** bug
    — the class this repo's cost tenet exists to catch — not a correctness one,
    so it would not surface in any behavioural test.
    """

    @pytest.fixture
    def model(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel

        config = ModelConfig(model_id=CLAUDE_MODEL_ID, caching_enabled=True)
        return CountTokensBedrockModel(**config.to_bedrock_config())

    @staticmethod
    def _messages(steered: bool):
        result_content = [
            {
                "toolResult": {
                    "toolUseId": "t1",
                    "content": [{"text": "thread body"}],
                    "status": "success",
                }
            }
        ]
        if steered:
            result_content.append(
                {"text": "<user_message_during_turn>\nuse the other file\n</user_message_during_turn>"}
            )
        return [
            {"role": "user", "content": [{"text": "first turn"}]},
            {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "t1", "name": "get_thread", "input": {}}}
                ],
            },
            {"role": "user", "content": result_content},
        ]

    def _format(self, model, steered: bool):
        return model.format_request(
            self._messages(steered),
            [
                {
                    "name": "get_thread",
                    "description": "Fetch a thread",
                    "inputSchema": {"json": {"type": "object", "properties": {}}},
                }
            ],
            system_prompt_content=[
                {"text": "You are a helpful assistant."},
                {"cachePoint": {"type": "default"}},
            ],
        )

    def test_still_exactly_three_cache_points(self, model):
        request = self._format(model, steered=True)
        assert _count_cache_points(request) == 3, json.dumps(
            request, default=str, indent=2
        )

    def test_the_injection_sits_behind_the_message_cache_point(self, model):
        """Append-only: the text lands before the trailing point, so every
        block ahead of that point is byte-identical to the unsteered turn."""
        request = self._format(model, steered=True)
        last_user = [m for m in request["messages"] if m["role"] == "user"][-1]

        assert last_user["content"][-1] == {"cachePoint": {"type": "default"}}
        assert "toolResult" in last_user["content"][0]
        assert last_user["content"][1]["text"].startswith("<user_message_during_turn>")

    def test_the_static_points_are_untouched(self, model):
        """The tools and system points are what the ~28k-token prefix rides on.

        A steering injection that shifted either would rewrite that prefix at
        the cache-write premium on every steered turn.
        """
        steered = self._format(model, steered=True)
        plain = self._format(model, steered=False)

        assert steered["toolConfig"] == plain["toolConfig"]
        assert steered["system"] == plain["system"]
        # Everything before the mixed message is identical too.
        assert steered["messages"][:-1] == plain["messages"][:-1]
