"""Explicit prompt-cache controls on the bedrock-runtime Responses transport.

⛔ **This feature is OPT-IN and DEFAULT OFF.** It was built on the premise that
a breakpoint after the static prefix would turn a history change from a full
re-write into a read. Measured live, that premise was wrong: GPT-5.6's default
implicit caching appends the history delta rather than re-writing, so the
breakpoint only stops history being cached — explicit cost ~57% MORE on a
churning conversation. The mechanism works; the placement is what failed.

These tests therefore split in two: the behaviour tests opt in explicitly via
the `model` fixture, and `TestOptInFlag` pins that the DEFAULT path — what
production actually sends — is byte-identical to the stock Strands request.

The request shape asserted here is AWS's documented one for explicit prompt
caching: ``prompt_cache_breakpoint`` on a content block of a ``developer``
message, ``prompt_cache_options`` at request level, ``prompt_cache_key``
top-level. Strands emits the system prompt as the top-level ``instructions``
string, which has no content block to mark — so the override re-expresses it
as that developer message.

These drive the real model class through the real ``_format_request``; nothing
here stubs the SDK's request assembly.
"""

import pytest

from apis.shared.models.bedrock_responses import (
    EXPLICIT_CACHE_ENABLED_ENV,
    EXPLICIT_CACHE_TTL,
    apply_explicit_prompt_cache,
    build_bedrock_responses_model,
    build_prompt_cache_key,
    explicit_prompt_cache_enabled,
)

SYSTEM = "You are a helpful assistant with a long stable preamble."
TOOLS = [
    {
        "name": "search",
        "description": "search things",
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }
]
MESSAGES = [
    {"role": "user", "content": [{"text": "first"}]},
    {"role": "assistant", "content": [{"text": "reply"}]},
    {"role": "user", "content": [{"text": "second"}]},
]


@pytest.fixture
def explicit_on(monkeypatch):
    """Opt in. Explicit mode is DEFAULT OFF — it measured ~57% more expensive
    than the model's implicit caching on a conversation with growing history
    (see the module docstring in bedrock_responses.py)."""
    monkeypatch.setenv(EXPLICIT_CACHE_ENABLED_ENV, "true")


@pytest.fixture
def model(explicit_on):
    return build_bedrock_responses_model("us.openai.gpt-5.6-sol", region="us-west-2")


@pytest.fixture
def default_model():
    """The model as it behaves with no opt-in — i.e. in production."""
    return build_bedrock_responses_model("us.openai.gpt-5.6-sol", region="us-west-2")


def _format(model, *, system_prompt=SYSTEM, tool_specs=TOOLS, messages=MESSAGES):
    return model._format_request(messages, tool_specs, system_prompt)


class TestBreakpointPlacement:
    def test_instructions_become_a_developer_message_carrying_the_breakpoint(self, model):
        request = _format(model)

        assert "instructions" not in request, (
            "the system prompt has to live in `input` to carry a content block"
        )
        first = request["input"][0]
        assert first["type"] == "message"
        assert first["role"] == "developer"
        assert first["content"] == [
            {
                "type": "input_text",
                "text": SYSTEM,
                "prompt_cache_breakpoint": {"mode": "explicit"},
            }
        ]

    def test_conversation_history_follows_the_breakpoint_in_order(self, model):
        request = _format(model)

        # The boundary sits after tools + system and before any history, so
        # a history change costs a read of the prefix, not a re-write.
        history = request["input"][1:]
        assert len(history) == len(MESSAGES)
        assert [m["role"] for m in history] == ["user", "assistant", "user"]

    def test_only_one_breakpoint_is_emitted(self, model):
        """The API caps breakpoints at 4; we spend exactly one, on the prefix."""
        request = _format(model)

        marked = [
            block
            for item in request["input"]
            if isinstance(item.get("content"), list)
            for block in item["content"]
            if isinstance(block, dict) and "prompt_cache_breakpoint" in block
        ]
        assert len(marked) == 1

    def test_tools_stay_top_level_and_untouched(self, model):
        request = _format(model)

        assert request["tools"][0]["name"] == "search"


class TestCacheOptions:
    def test_options_ride_extra_body(self, model):
        # `prompt_cache_options` is not a named parameter on the OpenAI SDK's
        # responses.create, so it has to travel in extra_body.
        request = _format(model)

        assert request["extra_body"]["prompt_cache_options"] == {
            "mode": "explicit",
            "ttl": EXPLICIT_CACHE_TTL,
        }

    def test_ttl_agrees_with_the_classifier_window(self):
        """The string here and the seconds the classifier uses must not drift."""
        from apis.shared.observability import OPENAI_RESPONSES_CACHE_TTL_SECONDS

        assert EXPLICIT_CACHE_TTL.endswith("m")
        assert int(EXPLICIT_CACHE_TTL[:-1]) * 60 == OPENAI_RESPONSES_CACHE_TTL_SECONDS

    def test_an_existing_extra_body_is_merged_not_clobbered(self):
        request = {
            "instructions": SYSTEM,
            "input": [],
            "extra_body": {"something_else": 1},
        }
        apply_explicit_prompt_cache(request, system_prompt=SYSTEM, tool_specs=TOOLS)

        assert request["extra_body"]["something_else"] == 1
        assert "prompt_cache_options" in request["extra_body"]

    def test_a_caller_supplied_option_wins(self):
        request = {
            "instructions": SYSTEM,
            "input": [],
            "extra_body": {"prompt_cache_options": {"mode": "implicit"}},
        }
        apply_explicit_prompt_cache(request, system_prompt=SYSTEM, tool_specs=TOOLS)

        assert request["extra_body"]["prompt_cache_options"] == {"mode": "implicit"}


class TestPromptCacheKey:
    def test_is_a_top_level_request_parameter(self, model):
        # It IS a named SDK parameter, unlike prompt_cache_options.
        request = _format(model)

        assert request["prompt_cache_key"] == build_prompt_cache_key(SYSTEM, TOOLS)

    def test_is_stable_across_turns_of_one_conversation(self, model):
        """Keyed on the static prefix only — history must not rotate it.

        A key that changed every turn would be the exact cache-busting this
        feature exists to prevent.
        """
        turn_one = _format(model, messages=MESSAGES[:1])
        turn_two = _format(model, messages=MESSAGES)

        assert turn_one["prompt_cache_key"] == turn_two["prompt_cache_key"]

    def test_rotates_when_the_system_prompt_changes(self, model):
        assert (
            _format(model)["prompt_cache_key"]
            != _format(model, system_prompt=SYSTEM + " extra")["prompt_cache_key"]
        )

    def test_rotates_when_the_tool_set_changes(self, model):
        other = [dict(TOOLS[0], name="different")]

        assert _format(model)["prompt_cache_key"] != _format(model, tool_specs=other)["prompt_cache_key"]

    def test_rotates_when_only_tool_order_changes(self, model):
        """Prefix matching is order-sensitive, so the key must be too."""
        two = TOOLS + [dict(TOOLS[0], name="second")]
        flipped = list(reversed(two))

        assert (
            _format(model, tool_specs=two)["prompt_cache_key"]
            != _format(model, tool_specs=flipped)["prompt_cache_key"]
        )

    def test_no_tools_is_stable_and_distinct_from_having_tools(self, model):
        none_key = _format(model, tool_specs=None)["prompt_cache_key"]

        assert none_key == _format(model, tool_specs=[])["prompt_cache_key"]
        assert none_key != _format(model)["prompt_cache_key"]

    def test_a_caller_supplied_key_wins(self):
        request = {"instructions": SYSTEM, "input": [], "prompt_cache_key": "mine"}
        apply_explicit_prompt_cache(request, system_prompt=SYSTEM, tool_specs=TOOLS)

        assert request["prompt_cache_key"] == "mine"


class TestNoSystemPrompt:
    """With no static prefix to bound, stay on implicit caching."""

    def test_request_is_left_untouched(self, model):
        request = _format(model, system_prompt=None)

        assert "prompt_cache_key" not in request
        assert "extra_body" not in request
        assert request["input"][0]["role"] == "user"

    def test_explicit_mode_is_not_forced_on(self, model):
        # Switching to explicit mode opts OUT of the model's default implicit
        # caching. With a badly placed boundary that is worse than not
        # switching at all, so absent a system prompt we do not switch.
        request = _format(model, system_prompt=None)

        assert "prompt_cache_options" not in (request.get("extra_body") or {})


class TestOptInFlag:
    """Default OFF, and the default path must be byte-identical to stock.

    Explicit mode measured ~57% MORE expensive than implicit on a churning
    conversation: the breakpoint after the static prefix stops history being
    cached, so uncached input grows every turn. The mechanism works; the
    placement is what failed. Nobody re-enables this without re-running
    `scripts/probe_gpt56_cache_rates.py --mode both --grow-history` and beating
    the implicit arm.
    """

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(EXPLICIT_CACHE_ENABLED_ENV, raising=False)

        assert explicit_prompt_cache_enabled() is False

    def test_empty_string_stays_disabled(self, monkeypatch):
        # Workflow env vars can materialize as "" — that must not opt in.
        monkeypatch.setenv(EXPLICIT_CACHE_ENABLED_ENV, "")

        assert explicit_prompt_cache_enabled() is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "True"])
    def test_only_the_literal_true_enables(self, monkeypatch, value):
        monkeypatch.setenv(EXPLICIT_CACHE_ENABLED_ENV, value)

        assert explicit_prompt_cache_enabled() is True

    @pytest.mark.parametrize("value", ["false", "1", "yes", "on", "explicit"])
    def test_nothing_else_enables(self, monkeypatch, value):
        monkeypatch.setenv(EXPLICIT_CACHE_ENABLED_ENV, value)

        assert explicit_prompt_cache_enabled() is False

    def test_the_default_request_is_the_stock_shape(self, default_model, monkeypatch):
        """What production actually sends: untouched, on implicit caching."""
        monkeypatch.delenv(EXPLICIT_CACHE_ENABLED_ENV, raising=False)

        request = _format(default_model)

        assert request["instructions"] == SYSTEM
        assert "prompt_cache_key" not in request
        assert "extra_body" not in request
        assert request["input"][0]["role"] == "user"
        assert not any(
            "prompt_cache_breakpoint" in block
            for item in request["input"]
            if isinstance(item.get("content"), list)
            for block in item["content"]
            if isinstance(block, dict)
        )


class TestOtherTransportsUnaffected:
    def test_mantle_responses_gets_no_explicit_controls(self, explicit_on):
        """openai.gpt-5.4 on Mantle is implicit-only — it has no breakpoints.

        Sending explicit controls there would at best be ignored and at worst
        rejected, so the Mantle builder must not inherit any of this.
        """
        from apis.shared.models.mantle import MantleApiMode, build_mantle_model

        model = build_mantle_model(
            model_id="openai.gpt-5.4",
            api_mode=MantleApiMode.RESPONSES,
            region="us-east-1",
        )
        request = model._format_request(MESSAGES, TOOLS, SYSTEM)

        assert request["instructions"] == SYSTEM
        assert "prompt_cache_key" not in request
        assert "extra_body" not in request


class TestUsageNormalizationStillApplies:
    def test_disjoint_buckets_survive_the_format_override(self, model):
        """PR-1's normalization and this override live on the same class."""
        from openai.types.responses.response_usage import ResponseUsage

        usage_obj = ResponseUsage.model_validate(
            {
                "input_tokens": 30_500,
                "input_tokens_details": {"cached_tokens": 30_000, "cache_write_tokens": 400},
                "output_tokens": 120,
                "output_tokens_details": {"reasoning_tokens": 64},
                "total_tokens": 30_620,
            }
        )
        usage = model._format_chunk({"chunk_type": "metadata", "data": usage_obj})[
            "metadata"
        ]["usage"]

        assert usage["inputTokens"] == 100
        assert usage["cacheReadInputTokens"] == 30_000
        assert usage["cacheWriteInputTokens"] == 400
