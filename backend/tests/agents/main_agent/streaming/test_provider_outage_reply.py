"""A provider outage names the model and the service, and says it isn't ours.

Prod 2026-11-13: ``global.anthropic.claude-opus-5-5`` returned
``ServiceUnavailableException`` on every call for about eight hours. Users got
"The model service is temporarily unavailable" with the raw AWS text quoted,
including "(reached max retries: 1)", which read as if nothing had been
retried. The reply now says which model, which service runs it, that the fault
is the provider's and not this app's or the user's message, and carries the
model on the event so the SPA can offer "Try again" and another model.
"""

import json
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import patch

import pytest

from agents.main_agent.streaming.stream_coordinator import (
    StreamCoordinator,
    _describe_provider_outage,
)
from agents.main_agent.streaming.stream_processor import _handle_completion_events
from apis.shared.errors import (
    ConversationalErrorEvent,
    ErrorCode,
    build_conversational_error_event,
    provider_unavailable_message,
)

_RAW_503 = (
    "An error occurred (ServiceUnavailableException) when calling the "
    "ConverseStream operation (reached max retries: 1): Bedrock is unable to "
    "process your request."
)


class TestProviderUnavailableMessage:
    def test_names_the_model_and_aws(self):
        message = provider_unavailable_message("Claude Opus 5.5", "bedrock")
        assert message.startswith("⚠️ **Claude Opus 5.5 isn't responding right now**")
        assert "Amazon Bedrock, the AWS service that runs this model" in message
        assert "a problem on AWS's side, not with this app or your message" in message
        assert "Send your message again in a moment, or switch models" in message

    @pytest.mark.parametrize("provider", ["bedrock-responses", "mantle"])
    def test_other_bedrock_surfaces_are_aws_too(self, provider):
        assert "AWS's side" in provider_unavailable_message("GPT-6.1 Sol", provider)

    def test_openai_direct_names_openai(self):
        message = provider_unavailable_message("GPT-6.1", "openai")
        assert "OpenAI, the OpenAI service" in message
        assert "OpenAI's side" in message
        assert "AWS" not in message

    def test_unknown_model_and_provider_stay_generic(self):
        message = provider_unavailable_message()
        assert message.startswith("⚠️ **This model isn't responding right now**")
        assert "the provider's side, not with this app or your message" in message

    def test_never_carries_a_product_name(self):
        """Other organizations deploy this stack under their own name."""
        assert "boise" not in provider_unavailable_message("X", "bedrock").lower()


class TestClassification:
    @pytest.mark.parametrize(
        "code", [ErrorCode.MODEL_ERROR, ErrorCode.STREAM_ERROR, ErrorCode.AGENT_ERROR]
    )
    def test_raised_outage_becomes_provider_unavailable(self, code):
        event = build_conversational_error_event(code=code, error=Exception(_RAW_503))
        assert event.code == ErrorCode.PROVIDER_UNAVAILABLE
        assert event.metadata == {"error_kind": "provider_unavailable"}
        assert event.recoverable is True

    def test_raw_provider_text_stays_out_of_the_reply(self):
        event = build_conversational_error_event(
            code=ErrorCode.STREAM_ERROR, error=Exception(_RAW_503)
        )
        assert "reached max retries" not in event.message
        assert "ServiceUnavailableException" not in event.message

    def test_force_stop_outage_gets_its_own_code_and_keeps_the_raw_detail(self):
        events, _ = _handle_completion_events(
            {"force_stop": True, "force_stop_reason": _RAW_503}
        )
        data = events[0]["data"]
        assert data["code"] == "provider_unavailable"
        assert data["detail"] == _RAW_503
        assert "reached max retries" not in data["error"]

    def test_other_force_stops_keep_agent_error(self):
        events, _ = _handle_completion_events(
            {"force_stop": True, "force_stop_reason": "ThrottlingException: slow down"}
        )
        assert events[0]["data"]["code"] == "agent_error"


def _wrapper(model_id: str = "global.anthropic.claude-opus-5-5", provider: str = "bedrock"):
    return SimpleNamespace(
        model_config=SimpleNamespace(
            model_id=model_id,
            get_provider=lambda: SimpleNamespace(value=provider),
        )
    )


def _outage_event() -> ConversationalErrorEvent:
    return ConversationalErrorEvent(
        code=ErrorCode.PROVIDER_UNAVAILABLE,
        message=provider_unavailable_message(),
        recoverable=True,
        metadata={"session_id": "sess-1"},
    )


class TestDescribeProviderOutage:
    @pytest.mark.asyncio
    async def test_names_the_turn_model_and_puts_it_on_metadata(self):
        async def _record(model_id: str):
            return SimpleNamespace(model_name="Claude Opus 5.5")

        with patch("apis.shared.costs.pricing_config.get_model_by_model_id", _record):
            event = await _describe_provider_outage(_outage_event(), _wrapper())

        assert event.message.startswith("⚠️ **Claude Opus 5.5 isn't responding right now**")
        assert "AWS's side" in event.message
        assert event.metadata == {
            "session_id": "sess-1",
            "error_kind": "provider_unavailable",
            "model_id": "global.anthropic.claude-opus-5-5",
            "model_name": "Claude Opus 5.5",
            "provider": "bedrock",
        }

    @pytest.mark.asyncio
    async def test_a_catalog_failure_still_replies_unnamed(self):
        async def _boom(model_id: str):
            raise RuntimeError("catalog down")

        with patch("apis.shared.costs.pricing_config.get_model_by_model_id", _boom):
            event = await _describe_provider_outage(_outage_event(), _wrapper())

        assert event.message.startswith("⚠️ **This model isn't responding right now**")
        assert event.metadata["model_id"] == "global.anthropic.claude-opus-5-5"
        assert "model_name" not in event.metadata

    @pytest.mark.asyncio
    async def test_no_wrapper_keeps_the_generic_reply(self):
        event = await _describe_provider_outage(_outage_event(), None)
        assert event.message == provider_unavailable_message()
        assert event.metadata == {"session_id": "sess-1", "error_kind": "provider_unavailable"}


# ---------------------------------------------------------------------------
# End to end through the coordinator: the live frames and the persisted reply.
# ---------------------------------------------------------------------------


class _ForceStopAgent:
    def __init__(self) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            yield {"force_stop": True, "force_stop_reason": _RAW_503}

        return _gen()


class _RaisingAgent:
    def __init__(self) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            raise RuntimeError(_RAW_503)
            yield  # pragma: no cover

        return _gen()


class _NoopSessionManager:
    async def update_after_turn(self, input_tokens: int, current_messages=None):
        return None


class _RecordingPersist:
    def __init__(self) -> None:
        self.texts: List[str] = []

    def create_message(self, session_id: str, agent_id: str, session_message: Any) -> None:
        inner = session_message.message
        content = inner.get("content") if isinstance(inner, dict) else inner.content
        self.texts.append("".join(block.get("text", "") for block in content))


def _stream_error(frames: List[str]) -> Optional[Dict[str, Any]]:
    for frame in frames:
        if frame.startswith("event: stream_error"):
            return json.loads(frame.split("data: ", 1)[1])
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_cls", [_ForceStopAgent, _RaisingAgent])
async def test_coordinator_streams_and_persists_the_named_reply(agent_cls):
    async def _record(model_id: str):
        return SimpleNamespace(model_name="Claude Opus 5.5")

    persist = _RecordingPersist()
    frames: List[str] = []
    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=persist,
    ), patch("apis.shared.costs.pricing_config.get_model_by_model_id", _record):
        async for sse in StreamCoordinator().stream_response(
            agent=agent_cls(),
            prompt="hello",
            session_manager=_NoopSessionManager(),
            session_id="sess-outage",
            user_id="user-1",
            main_agent_wrapper=_wrapper(),
        ):
            frames.append(sse)

    event = _stream_error(frames)
    assert event is not None
    assert event["code"] == "provider_unavailable"
    assert event["metadata"]["model_id"] == "global.anthropic.claude-opus-5-5"
    assert event["metadata"]["model_name"] == "Claude Opus 5.5"
    assert event["message"].startswith("⚠️ **Claude Opus 5.5 isn't responding right now**")
    # Not double-wrapped by the generic template.
    assert "Something went wrong" not in event["message"]
    # What the user saw live is what a reload shows.
    assert persist.texts == [event["message"]]
