"""DeclinedAnswerHook: a provider-declined call's message is never left empty.

The notice must land in the message object Strands then appends, only for a
declining stop reason, only when the call said nothing, and never raise.
"""

from __future__ import annotations

from types import SimpleNamespace

from agents.main_agent.session.hooks.declined_answer import (
    DECLINE_STOP_REASONS,
    DECLINED_NOTICE,
    DeclinedAnswerHook,
    needs_notice,
)
from apis.shared.evals.score import looks_like_refusal


def _event(stop_reason, content):
    message = {"role": "assistant", "content": content}
    return SimpleNamespace(stop_response=SimpleNamespace(stop_reason=stop_reason, message=message)), message


def test_a_declined_empty_message_gets_the_notice_in_place():
    for reason in sorted(DECLINE_STOP_REASONS):
        event, message = _event(reason, [])
        DeclinedAnswerHook()._on_after_model_call(event)
        assert message["content"] == [{"text": DECLINED_NOTICE}]


def test_blank_text_counts_as_empty():
    event, message = _event("content_filtered", [{"text": "   "}])
    DeclinedAnswerHook()._on_after_model_call(event)
    assert message["content"][-1] == {"text": DECLINED_NOTICE}


def test_a_message_with_text_or_a_tool_use_is_left_alone():
    for content in ([{"text": "Here is what the guardrail said."}], [{"toolUse": {"name": "calculator"}}]):
        event, message = _event("guardrail_intervened", list(content))
        DeclinedAnswerHook()._on_after_model_call(event)
        assert message["content"] == content


def test_other_stop_reasons_and_failed_calls_are_ignored():
    for reason in ("end_turn", "tool_use", "max_tokens", "cancelled"):
        event, message = _event(reason, [])
        DeclinedAnswerHook()._on_after_model_call(event)
        assert message["content"] == []
    DeclinedAnswerHook()._on_after_model_call(SimpleNamespace(stop_response=None))


def test_needs_notice_only_for_a_silent_declined_call():
    assert needs_notice("content_filtered", False) and needs_notice("guardrail_intervened", False)
    assert not needs_notice("content_filtered", True) and not needs_notice("end_turn", False)


def test_the_eval_harness_reads_the_notice_as_a_refusal():
    # score.py cannot import from agents/ (import boundary), so it carries the
    # notice's opening words as a literal; this pins the two together.
    assert looks_like_refusal(DECLINED_NOTICE)


def test_a_real_strands_agent_keeps_the_notice_in_its_conversation():
    """The hook mutates the message Strands appends: proven with Strands itself, not a mock."""
    import asyncio
    from typing import Any, AsyncIterator

    from strands import Agent
    from strands.models import Model

    class DecliningModel(Model):
        """Streams a call that opens and stops with content_filtered, saying nothing."""

        def update_config(self, **model_config: Any) -> None:
            pass

        def get_config(self) -> Any:
            return {}

        async def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover - unused
            raise NotImplementedError
            yield

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs) -> AsyncIterator[dict]:
            yield {"messageStart": {"role": "assistant"}}
            yield {"messageStop": {"stopReason": "content_filtered"}}
            yield {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 0, "totalTokens": 10},
                                "metrics": {"latencyMs": 1}}}

    agent = Agent(model=DecliningModel(), hooks=[DeclinedAnswerHook()], callback_handler=None)
    result = asyncio.run(agent.invoke_async("hello"))

    assert result.stop_reason == "content_filtered"
    assert agent.messages[-1]["role"] == "assistant"
    assert agent.messages[-1]["content"] == [{"text": DECLINED_NOTICE}]
