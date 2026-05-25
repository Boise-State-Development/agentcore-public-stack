"""Regression: force_stop errors persist the friendly classified markdown
to AgentCore Memory verbatim, and only the assistant turn — not a
duplicate user turn.

When the Strands agent loop force-stops (Bedrock rejects a content block,
throttling, access denied, etc.) ``stream_processor._format_force_stop_message``
turns the raw AWS reason into user-facing markdown. ``StreamCoordinator``
intercepts the resulting ``error`` event and writes a synthetic assistant
message to the session repository so the model sees what happened on the
next turn.

USER-VISIBLE BUG GUARDED HERE: previously the AGENT_ERROR branch also
re-persisted the user turn, even though Strands' MessageAddedEvent hook had
already written it at turn start. AgentCore Memory rejected the duplicate
write, the surrounding try/except caught the failure, and the assistant
error message was abandoned along with it — so the user saw the error
live, then saw it vanish on refresh. We now persist only the assistant
turn (mirroring the MAX_TOKENS reasoning at lines 540-555). Other error
codes (STREAM_ERROR, MODEL_ERROR, …) can fire before the user turn enters
agent.messages and keep the prior persist-both shape with the wrapped
message.
"""

from typing import Any, AsyncIterator, Dict, List
from unittest.mock import patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


class _FakeAgent:
    def __init__(self, raw_events: List[Dict[str, Any]]) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self._raw_events = raw_events

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            for ev in self._raw_events:
                yield ev

        return _gen()


class _RaisingAgent:
    """Mimics the path-B failure mode: Strands' Bedrock ValidationException
    bypasses the force_stop event and propagates as a raw exception out of
    stream_async into stream_response's outer except handler. Reproduces
    the GeneratorExit-during-yield case described in the user's log.
    """

    def __init__(self, exc: BaseException) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self._exc = exc

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        exc = self._exc

        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            raise exc
            yield  # pragma: no cover — make this an async generator

        return _gen()


class _NoopSessionManager:
    """Outer session manager passed into stream_response. The persistence
    path under test creates its OWN session manager via SessionFactory, so
    this stub only needs to satisfy the surface stream_response touches
    when no compaction is configured."""

    async def update_after_turn(self, input_tokens: int, current_messages=None):
        return None


class _RecordingPersistSessionManager:
    """Mimics the SDK's ``AgentCoreMemorySessionManager`` shape — exposes
    ``create_message`` directly. The persist path used to require a nested
    ``.base_manager`` wrapper that no longer exists in the current SDK;
    keeping this stub flat catches a regression to that broken guard.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def create_message(self, session_id: str, agent_id: str, session_message: Any) -> None:
        self.calls.append(
            {
                "session_id": session_id,
                "agent_id": agent_id,
                "message": session_message,
            }
        )


async def _collect(agent: Any, sm: _NoopSessionManager) -> List[str]:
    coordinator = StreamCoordinator()
    frames: List[str] = []
    async for sse in coordinator.stream_response(
        agent=agent,
        prompt="please read this file",
        session_manager=sm,
        session_id="sess-force-stop",
        user_id="user-1",
        main_agent_wrapper=None,
    ):
        frames.append(sse)
    return frames


def _force_stop_event(reason: str) -> Dict[str, Any]:
    """Raw Strands event shape that triggers the force_stop branch in
    stream_processor._handle_completion_events."""
    return {"force_stop": True, "force_stop_reason": reason}


@pytest.mark.asyncio
async def test_unsupported_documents_force_stop_persists_only_assistant_turn():
    """The gpt-oss-120b case: Bedrock rejects the document content block
    outright.

    Regression for the "error visible live, gone on refresh" bug:
    - Exactly ONE create_message call (assistant), not two — the user
      turn was already persisted by Strands' hook at turn start and
      re-persisting caused AgentCore Memory to reject the conflicting
      write and drop the assistant message along with it.
    - The persisted text is the friendly classified markdown from
      _format_force_stop_message, NOT the "Something went wrong" wrapper.
    """
    raw_reason = (
        "An error occurred (ValidationException) when calling the "
        "ConverseStream operation: This model doesn't support documents."
    )
    persist_sm = _RecordingPersistSessionManager()

    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=persist_sm,
    ):
        await _collect(_FakeAgent([_force_stop_event(raw_reason)]), _NoopSessionManager())

    # Exactly one call — assistant only. Re-persisting the user turn
    # here would conflict with the hook write and (in real AgentCore
    # Memory) abort the assistant write too.
    assert len(persist_sm.calls) == 1, (
        f"expected exactly 1 create_message call (assistant only), got "
        f"{len(persist_sm.calls)}: {persist_sm.calls}"
    )

    assistant_call = persist_sm.calls[0]
    assert assistant_call["session_id"] == "sess-force-stop"

    # Role must be assistant — not user. A regression where this writes
    # the user turn would re-introduce the duplicate-write conflict.
    persisted_msg = assistant_call["message"]
    inner = getattr(persisted_msg, "message", None)
    assert inner is not None, f"SessionMessage shape unexpected: {persisted_msg!r}"
    role = inner.get("role") if isinstance(inner, dict) else getattr(inner, "role", None)
    assert role == "assistant", f"expected role='assistant', got {role!r}"

    # The persisted assistant text must be the friendly classified
    # markdown from _format_force_stop_message, not the generic wrapper.
    assistant_text = _extract_text(persisted_msg)
    assert "can't read attached files" in assistant_text
    assert "switch to a model that supports documents" in assistant_text
    # Must NOT be wrapped in build_conversational_error_event's generic
    # template — that template would add "Something went wrong" and a
    # blockquote and "Please try again."
    assert "Something went wrong" not in assistant_text
    assert "Please try again." not in assistant_text


@pytest.mark.asyncio
async def test_document_size_force_stop_persists_only_assistant_turn():
    """Size-limit force_stop persists the spreadsheet-analysis advice
    verbatim, assistant-only, un-wrapped."""
    raw_reason = (
        "ValidationException: The provided document exceeds the maximum "
        "document size."
    )
    persist_sm = _RecordingPersistSessionManager()

    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=persist_sm,
    ):
        await _collect(_FakeAgent([_force_stop_event(raw_reason)]), _NoopSessionManager())

    assert len(persist_sm.calls) == 1
    assistant_text = _extract_text(persist_sm.calls[0]["message"])
    assert "too large" in assistant_text
    assert "4.5 MB" in assistant_text
    assert "Spreadsheet Analysis" not in assistant_text
    assert "Something went wrong" not in assistant_text


@pytest.mark.asyncio
async def test_fallthrough_force_stop_still_persists_assistant_only():
    """Force_stops that don't match any classified branch fall through
    to "Agent force-stopped: <raw>". Per design, all force_stops are
    persisted (matched + fallthrough), still assistant-only."""
    raw_reason = "ServiceQuotaExceeded: some unfamiliar upstream condition"
    persist_sm = _RecordingPersistSessionManager()

    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=persist_sm,
    ):
        await _collect(_FakeAgent([_force_stop_event(raw_reason)]), _NoopSessionManager())

    assert len(persist_sm.calls) == 1
    assistant_text = _extract_text(persist_sm.calls[0]["message"])
    assert "force-stopped" in assistant_text
    assert raw_reason in assistant_text


# ---------------------------------------------------------------------------
# Path B: raw exception inside process_agent_stream.
#
# Bedrock ValidationException (e.g. gpt-oss-120b + a document) doesn't reach
# stream_processor's force_stop branch — Strands' `yield ForceStopEvent` hits
# a GeneratorExit during the throw (visible as OpenTelemetry detach
# tracebacks in inference-api logs). The raw exception propagates out of
# agent.stream_async into process_agent_stream's outer `except Exception` at
# stream_processor.py:1532, which emits a STREAM_ERROR event. That flows
# into stream_coordinator's in-loop error handler — the SAME persistence
# path as AGENT_ERROR. Before this fix it re-persisted the user turn there
# too, AgentCore Memory rejected the conflicting write, and the assistant
# error was abandoned along with it — so the user saw the error live and
# then saw it vanish on refresh.
#
# Fix: in-loop handler persists assistant-only for ALL error codes (not just
# AGENT_ERROR). The conversational template embeds the raw error in a
# blockquote, so the model still has full context on follow-up turns.
# ---------------------------------------------------------------------------


def _last_assistant_call_text(persist_sm: "_RecordingPersistSessionManager") -> str:
    assert len(persist_sm.calls) == 1, (
        f"expected exactly 1 create_message call (assistant only), got "
        f"{len(persist_sm.calls)}: {persist_sm.calls}"
    )
    return _extract_text(persist_sm.calls[0]["message"])


@pytest.mark.asyncio
async def test_stream_error_unsupported_documents_persists_assistant_only():
    """gpt-oss-120b ValidationException path: raw exception caught by
    process_agent_stream's outer except, emitted as STREAM_ERROR, flows
    through the in-loop handler. Persisted exactly once, assistant role,
    and the raw error must appear in the persisted text so the model has
    context on follow-up turns."""
    exc = Exception(
        "An error occurred (ValidationException) when calling the "
        "ConverseStream operation: This model doesn't support documents."
    )
    persist_sm = _RecordingPersistSessionManager()

    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=persist_sm,
    ):
        await _collect(_RaisingAgent(exc), _NoopSessionManager())

    # Exactly one create_message call — assistant only. Re-persisting
    # the user turn would conflict with the hook write in real
    # AgentCore Memory and abort the assistant write too.
    assistant_text = _last_assistant_call_text(persist_sm)

    # Assistant role
    persisted_msg = persist_sm.calls[0]["message"]
    inner = getattr(persisted_msg, "message", None)
    role = inner.get("role") if isinstance(inner, dict) else None
    assert role == "assistant"

    # The raw Bedrock reason is embedded (the STREAM_ERROR template
    # places it in a blockquote). The model sees enough to know what
    # happened on the next turn.
    assert "This model doesn't support documents" in assistant_text


@pytest.mark.asyncio
async def test_stream_error_unknown_exception_persists_assistant_only():
    """Unrecognized exception classes still persist assistant-only —
    the duplicate-user-write bug applied to all STREAM_ERROR codes."""
    exc = RuntimeError("some unfamiliar upstream condition")
    persist_sm = _RecordingPersistSessionManager()

    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=persist_sm,
    ):
        await _collect(_RaisingAgent(exc), _NoopSessionManager())

    assistant_text = _last_assistant_call_text(persist_sm)
    assert "some unfamiliar upstream condition" in assistant_text


def _extract_text(session_message: Any) -> str:
    """Pull the text content out of a Strands SessionMessage, regardless
    of whether the SDK exposes it as ``.message`` (dict) or
    ``.message_content`` / ``.text``. The persistence path uses
    ``SessionMessage.from_message`` so the round-tripped shape contains a
    ``content`` list with ``{"text": ...}`` blocks."""
    # SessionMessage stores the original Strands Message dict on .message
    msg = getattr(session_message, "message", None)
    if msg is None and isinstance(session_message, dict):
        msg = session_message.get("message", session_message)
    assert msg is not None, f"could not find message on {session_message!r}"
    content = msg.get("content", []) if isinstance(msg, dict) else []
    parts = [block.get("text", "") for block in content if isinstance(block, dict)]
    return "".join(parts)
