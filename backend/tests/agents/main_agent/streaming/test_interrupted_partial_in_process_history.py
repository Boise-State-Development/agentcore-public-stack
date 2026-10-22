"""Regression: a stopped turn's partial reaches the model on the next turn.

``_persist_interruption`` writes the partial to Memory through a throwaway
session manager. Before the fix nothing appended it to the cached agent's
``agent.messages``, and the next turn hits the agent cache, where
``initialize()`` never re-runs. On dev (2026-10-22) the follow-up call's
``messageCount`` was 2: the model saw ``[question, follow-up]`` while Memory,
and every later restore, held ``[question, partial, follow-up]``.

The live list is aliased across every agent serving one ``(session, user)``
(``_adopt_session_conversation``), so the reply must be appended in place to
that list, never kept on one instance.
"""

from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


def _raw_message_start() -> Dict[str, Any]:
    return {"event": {"messageStart": {"role": "assistant"}}}


def _raw_text_delta(text: str) -> Dict[str, Any]:
    return {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}}}


def _raw_message_stop() -> Dict[str, Any]:
    return {"event": {"messageStop": {"stopReason": "end_turn"}}}


class _SessionManager:
    def __init__(self) -> None:
        self.cancelled = False

    async def update_after_turn(self, input_tokens: int, current_messages=None):
        return None


class _RecordingPersistSessionManager:
    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []

    def create_message(self, session_id: str, agent_id: str, session_message: Any) -> None:
        self.messages.append(session_message.to_message())


class _StrandsLikeAgent:
    """Appends the prompt as a user message, records what a model call would be
    sent, then plays the scripted events — the order Strands' event loop uses."""

    def __init__(self, messages: Optional[List[Dict[str, Any]]] = None) -> None:
        self.messages: List[Dict[str, Any]] = messages if messages is not None else []
        self.sent: List[List[Dict[str, Any]]] = []
        self.script: List[Any] = []

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            self.messages.append({"role": "user", "content": [{"text": prompt}]})
            self.sent.append([dict(m) for m in self.messages])
            reply = ""
            for step in self.script:
                if callable(step):
                    step()
                    continue
                delta = step.get("event", {}).get("contentBlockDelta")
                if delta:
                    reply += delta["delta"]["text"]
                if "messageStop" in step.get("event", {}):
                    self.messages.append({"role": "assistant", "content": [{"text": reply}]})
                yield step

        return _gen()


async def _run_turn(agent: _StrandsLikeAgent, sm: _SessionManager, prompt: str) -> List[str]:
    frames: List[str] = []
    async for frame in StreamCoordinator().stream_response(
        agent=agent,
        prompt=prompt,
        session_manager=sm,
        session_id="sess-stop",
        user_id="user-1",
        main_agent_wrapper=None,
    ):
        frames.append(frame)
    return frames


async def _noop_marker(session_id, user_id, reason="unknown", source="cancellation"):
    return None


def _stop_after(sm: _SessionManager, *texts: str) -> List[Any]:
    """A scripted answer the user stops after ``texts`` have streamed."""
    return [
        _raw_message_start(),
        *(_raw_text_delta(t) for t in texts),
        lambda: setattr(sm, "cancelled", True),
        _raw_text_delta(" (never streamed)"),
        _raw_message_stop(),
    ]


@pytest.mark.asyncio
async def test_next_turn_on_the_cached_agent_sees_the_stopped_partial():
    sm = _SessionManager()
    memory = _RecordingPersistSessionManager()
    agent = _StrandsLikeAgent()

    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=memory,
    ), patch("apis.shared.sessions.metadata.set_interrupted_turn", _noop_marker):
        agent.script = _stop_after(sm, "Chapter one begins", " with a storm")
        await _run_turn(agent, sm, "write me a long story")

        sm.cancelled = False
        agent.script = [_raw_message_start(), _raw_text_delta("Sure."), _raw_message_stop()]
        await _run_turn(agent, sm, "make it shorter")

    partial = {"role": "assistant", "content": [{"text": "Chapter one begins with a storm"}]}
    assert agent.sent[-1] == [
        {"role": "user", "content": [{"text": "write me a long story"}]},
        partial,
        {"role": "user", "content": [{"text": "make it shorter"}]},
    ]
    # In process and in Memory it is the same message, byte for byte, so a
    # restore after a container recycle rebuilds the same prefix.
    assert memory.messages == [partial]


@pytest.mark.asyncio
async def test_partial_reaches_every_agent_aliased_to_the_conversation():
    """An @-mention agent shares the list by reference; the next plain turn
    cache-hits the original agent, which must see the partial too."""
    sm = _SessionManager()
    shared: List[Dict[str, Any]] = []
    mention_agent = _StrandsLikeAgent(shared)
    plain_agent = _StrandsLikeAgent(shared)

    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=_RecordingPersistSessionManager(),
    ), patch("apis.shared.sessions.metadata.set_interrupted_turn", _noop_marker):
        mention_agent.script = _stop_after(sm, "Partial from the mention")
        await _run_turn(mention_agent, sm, "@helper explain")

        sm.cancelled = False
        plain_agent.script = [_raw_message_start(), _raw_text_delta("ok"), _raw_message_stop()]
        await _run_turn(plain_agent, sm, "go on")

    assert [m["role"] for m in plain_agent.sent[-1]] == ["user", "assistant", "user"]
    assert plain_agent.sent[-1][1]["content"] == [{"text": "Partial from the mention"}]
    assert plain_agent.messages is shared


@pytest.mark.asyncio
async def test_placeholder_answers_the_dangling_user_turn_in_process_too():
    """A stop before any token persists the placeholder; the live list gets the
    same placeholder so it never ends on two user turns."""
    sm = _SessionManager()
    memory = _RecordingPersistSessionManager()
    agent = _StrandsLikeAgent()

    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=memory,
    ), patch("apis.shared.sessions.metadata.set_interrupted_turn", _noop_marker):
        agent.script = _stop_after(sm)
        await _run_turn(agent, sm, "hello?")

    assert [m["role"] for m in agent.messages] == ["user", "assistant"]
    assert agent.messages[-1] == memory.messages[-1]
    assert "interrupted" in agent.messages[-1]["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_assistant_tail_stays_marker_only_in_process():
    """A continuation/resume teardown (tail already assistant) writes nothing
    to Memory and must append nothing in process either."""
    agent = _StrandsLikeAgent(
        [
            {"role": "user", "content": [{"text": "hi"}]},
            {"role": "assistant", "content": [{"text": "being extended"}]},
        ]
    )
    memory = _RecordingPersistSessionManager()
    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=memory,
    ), patch("apis.shared.sessions.metadata.set_interrupted_turn", _noop_marker):
        await StreamCoordinator()._persist_interruption(
            agent=agent, session_id="sess-stop", user_id="user-1", partial_text="more text"
        )

    assert memory.messages == []
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_failed_memory_write_appends_nothing_in_process():
    """The live list mirrors Memory: a reply Memory never got stays out of it."""

    class _FailingSessionManager:
        def create_message(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("memory unavailable")

    agent = _StrandsLikeAgent([{"role": "user", "content": [{"text": "hi"}]}])
    with patch(
        "agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
        return_value=_FailingSessionManager(),
    ), patch("apis.shared.sessions.metadata.set_interrupted_turn", _noop_marker):
        await StreamCoordinator()._persist_interruption(
            agent=agent, session_id="sess-stop", user_id="user-1", partial_text="partial"
        )

    assert agent.messages == [{"role": "user", "content": [{"text": "hi"}]}]


def test_reply_is_not_appended_once_the_conversation_moved():
    """If a new turn already appended its prompt, appending now would answer the
    wrong user turn; skip it."""
    tail = {"role": "user", "content": [{"text": "first"}]}
    agent = _StrandsLikeAgent([tail])
    agent.messages.append({"role": "user", "content": [{"text": "second"}]})

    appended = StreamCoordinator._append_interrupted_reply(
        agent, "sess-stop", "partial", tail=tail, history_len=1
    )

    assert appended is False
    assert [m["content"][0]["text"] for m in agent.messages] == ["first", "second"]
