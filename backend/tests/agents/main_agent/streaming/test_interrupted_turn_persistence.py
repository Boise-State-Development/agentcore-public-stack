"""Regression: a turn interrupted mid-stream (client Stop / refresh / dropped
socket) persists the in-flight partial assistant text + an interrupted marker
instead of leaving an orphan user turn.

A client teardown surfaces inside the coordinator generator as
``asyncio.CancelledError`` (cancellation delivered at an inner ``await``) or
``GeneratorExit`` (thrown at a ``yield`` via ``aclose()``). Both subclass
``BaseException``, so they slip past ``process_agent_stream``'s ``except
Exception`` and the coordinator's own ``except Exception`` — without a
dedicated arm the partial is lost and the turn stays a dangling user message.

Key invariants under test:
- The partial persisted is ONLY the in-flight message's text. Completed
  mid-turn messages were already committed by the ``append_message`` hook
  (``TurnBasedSessionManager`` persists per-message; ``flush`` is a no-op),
  so re-persisting their text would duplicate history.
- Assistant-only persistence — the user turn was already committed at turn
  start by Strands' MessageAddedEvent hook.
- The empty-partial placeholder exists solely to repair user→user role
  alternation, so it is gated on the history tail being a user message.
- The marker's fallback reason is ``connection_lost``; the client's
  ``user_stopped`` signal wins via precedence in ``set_interrupted_turn``
  (covered in tests/shared/test_sessions_metadata.py).
"""

import asyncio
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


class _InterruptingAgent:
    """Agent whose stream yields raw Strands events then raises the given
    teardown exception, mimicking a client disconnect mid-generation."""

    def __init__(self, events: List[Dict[str, Any]] = None, exc: BaseException = None) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self._events = events or []
        self._exc = exc if exc is not None else asyncio.CancelledError()

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            for event in self._events:
                yield event
            raise self._exc

        return _gen()


class _NoopSessionManager:
    async def update_after_turn(self, input_tokens: int, current_messages=None):
        return None


class _RecordingPersistSessionManager:
    """Flat ``create_message`` shape matching the current SDK."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def create_message(self, session_id: str, agent_id: str, session_message: Any) -> None:
        self.calls.append({"session_id": session_id, "agent_id": agent_id, "message": session_message})


def _extract_text(session_message: Any) -> str:
    inner = getattr(session_message, "message", None)
    content = inner.get("content") if isinstance(inner, dict) else getattr(inner, "content", None)
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def _raw_message_start() -> Dict[str, Any]:
    return {"event": {"messageStart": {"role": "assistant"}}}


def _raw_text_delta(text: str) -> Dict[str, Any]:
    return {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}}}


def _raw_message_stop() -> Dict[str, Any]:
    return {"event": {"messageStop": {"stopReason": "end_turn"}}}


async def _drive_until_teardown(agent: Any, expected_exc: type) -> None:
    coordinator = StreamCoordinator()
    with pytest.raises(expected_exc):
        async for _sse in coordinator.stream_response(
            agent=agent,
            prompt="please write a long essay",
            session_manager=_NoopSessionManager(),
            session_id="sess-interrupt",
            user_id="user-1",
            main_agent_wrapper=None,
        ):
            pass


@pytest.mark.asyncio
async def test_cancellation_invokes_interruption_persistence():
    """The CancelledError arm fires and re-raises so cancellation still unwinds."""
    called: Dict[str, Any] = {}

    async def _fake_persist(self, *, agent, session_id, user_id, partial_text):
        called.update(session_id=session_id, user_id=user_id, partial_text=partial_text)

    with patch.object(StreamCoordinator, "_persist_interruption", _fake_persist):
        await _drive_until_teardown(_InterruptingAgent(), asyncio.CancelledError)

    assert called.get("session_id") == "sess-interrupt"
    assert called.get("user_id") == "user-1"


@pytest.mark.asyncio
async def test_generator_exit_also_invokes_interruption_persistence():
    """Starlette teardown can surface as GeneratorExit (aclose at a yield)
    instead of CancelledError — the arm must catch both."""
    called: Dict[str, Any] = {}

    async def _fake_persist(self, *, agent, session_id, user_id, partial_text):
        called.update(session_id=session_id, user_id=user_id, partial_text=partial_text)

    with patch.object(StreamCoordinator, "_persist_interruption", _fake_persist):
        await _drive_until_teardown(
            _InterruptingAgent(exc=GeneratorExit()), GeneratorExit
        )

    assert called.get("session_id") == "sess-interrupt"


@pytest.mark.asyncio
async def test_partial_covers_only_in_flight_message():
    """Text from a COMPLETED mid-turn message (already persisted by the
    append_message hook) must not re-enter the partial — only deltas of the
    message still streaming at teardown count."""
    called: Dict[str, Any] = {}

    async def _fake_persist(self, *, agent, session_id, user_id, partial_text):
        called.update(partial_text=partial_text)

    events = [
        _raw_message_start(),
        _raw_text_delta("Completed first message."),
        _raw_message_stop(),
        _raw_message_start(),
        _raw_text_delta("In-flight par"),
    ]
    with patch.object(StreamCoordinator, "_persist_interruption", _fake_persist):
        await _drive_until_teardown(
            _InterruptingAgent(events=events), asyncio.CancelledError
        )

    assert called.get("partial_text") == "In-flight par"


@pytest.mark.asyncio
async def test_persist_interruption_writes_partial_assistant_only():
    """Non-empty partial → exactly one assistant create_message with that text,
    and the connection_lost fallback marker."""
    persist_sm = _RecordingPersistSessionManager()
    marker_calls: List[Dict[str, Any]] = []

    async def _fake_set_interrupted(session_id, user_id, reason="unknown", source="cancellation"):
        marker_calls.append({"session_id": session_id, "user_id": user_id, "reason": reason, "source": source})

    coordinator = StreamCoordinator()
    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=persist_sm,
    ), patch("apis.shared.sessions.metadata.set_interrupted_turn", _fake_set_interrupted):
        await coordinator._persist_interruption(
            agent=_InterruptingAgent(),
            session_id="sess-interrupt",
            user_id="user-1",
            partial_text="Here is the start of my answ",
        )

    assert len(persist_sm.calls) == 1
    call = persist_sm.calls[0]
    inner = getattr(call["message"], "message", None)
    role = inner.get("role") if isinstance(inner, dict) else getattr(inner, "role", None)
    assert role == "assistant"
    assert _extract_text(call["message"]) == "Here is the start of my answ"

    assert marker_calls == [
        {"session_id": "sess-interrupt", "user_id": "user-1", "reason": "connection_lost", "source": "cancellation"}
    ]


@pytest.mark.asyncio
async def test_empty_partial_uses_placeholder_when_user_turn_dangles():
    """Interruption before any token, history tail = user → persist a minimal
    placeholder assistant turn so user/assistant role alternation stays valid."""
    persist_sm = _RecordingPersistSessionManager()

    async def _fake_set_interrupted(session_id, user_id, reason="unknown", source="cancellation"):
        return None

    agent = _InterruptingAgent()  # messages tail is the user turn
    coordinator = StreamCoordinator()
    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=persist_sm,
    ), patch("apis.shared.sessions.metadata.set_interrupted_turn", _fake_set_interrupted):
        await coordinator._persist_interruption(
            agent=agent,
            session_id="sess-interrupt",
            user_id="user-1",
            partial_text="   ",
        )

    assert len(persist_sm.calls) == 1
    text = _extract_text(persist_sm.calls[0]["message"])
    assert "interrupted" in text.lower()


@pytest.mark.asyncio
async def test_empty_partial_skips_synthetic_write_when_tail_is_assistant():
    """No in-flight text and history tail = assistant (continuation/resume
    teardown) → alternation needs no repair, so no synthetic message; the
    marker is still set."""
    persist_sm = _RecordingPersistSessionManager()
    marker_calls: List[Dict[str, Any]] = []

    async def _fake_set_interrupted(session_id, user_id, reason="unknown", source="cancellation"):
        marker_calls.append({"reason": reason})

    agent = _InterruptingAgent()
    agent.messages = [
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [{"text": "truncated partial"}]},
    ]
    coordinator = StreamCoordinator()
    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=persist_sm,
    ), patch("apis.shared.sessions.metadata.set_interrupted_turn", _fake_set_interrupted):
        await coordinator._persist_interruption(
            agent=agent,
            session_id="sess-interrupt",
            user_id="user-1",
            partial_text="",
        )

    assert persist_sm.calls == []
    assert marker_calls == [{"reason": "connection_lost"}]
