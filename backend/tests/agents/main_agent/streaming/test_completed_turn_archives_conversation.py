"""A finished turn is queued for the conversation archive, keyed at its user message.

The archive write is the only thing conversation search adds to the turn path,
and it must sit after `done` and never be awaited (docs/specs/conversation-search.md
§1, §4). These tests drive the real coordinator and stand in for the scheduler
or the S3 write.

The key comes from counting what the request appended, not from `message_id`:
a resume the model closes without prose produces no assistant message, and
`message_id` then falls back to the session manager's stale `flush()` count.
On dev that wrote a resumed turn over the previous turn's object (1.27.0 smoke
pass, 2026-10-09); `test_a_resume_that_ends_on_a_tool_result_*` replays it.
"""

import asyncio
from typing import Any, AsyncIterator, Dict, List, Tuple
from unittest.mock import patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator
from apis.shared.conversation_archive import ArchivedTurn, drain_pending
from tests.agents.main_agent.streaming.test_completed_turn_clears_interrupt import (
    _CompletingAgent,
    _drive,
    _InterruptedAgent,
)


def _user(text):
    return {"role": "user", "content": [{"text": text}]}


def _assistant(text):
    return {"role": "assistant", "content": [{"text": text}]}


def _tool_call(tool_id, text=None):
    content = [{"text": text}] if text else []
    return {"role": "assistant", "content": content + [{"toolUse": {"toolUseId": tool_id, "name": "x", "input": {}}}]}


def _tool_result(tool_id):
    return {"role": "user", "content": [{"toolResult": {"toolUseId": tool_id, "content": [{"text": "raw"}]}}]}


def _start():
    return {"event": {"messageStart": {"role": "assistant"}}}


def _delta(text):
    return {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}}}


def _stop(reason="end_turn"):
    return {"event": {"messageStop": {"stopReason": reason}}}


class _ScriptedAgent:
    """Holds a history and, as its stream runs, appends messages the way Strands
    does: each step is a raw event to yield or a message to append."""

    def __init__(self, history: List[Dict[str, Any]], script: List[Any], exc: BaseException = None) -> None:
        self.messages = list(history)
        self._script = script
        self._exc = exc

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            for step in self._script:
                if "role" in step:
                    self.messages.append(step)
                else:
                    yield step
            if self._exc is not None:
                raise self._exc

        return _gen()


class _StoredHistory:
    """A session manager whose stored history holds `stored` messages, and whose
    `flush()` reports a stale per-instance count, as `TurnBasedSessionManager`'s
    does on a cached agent."""

    cancelled = False

    def __init__(self, stored: int, flush_returns: int) -> None:
        self.message_count = stored
        self._flush_returns = flush_returns

    def flush(self) -> int:
        return self._flush_returns

    async def update_after_turn(self, input_tokens: int, current_messages=None):
        return None


@pytest.fixture()
def archived(monkeypatch):
    """Each archive write as ``(turn, expect_existing)``, after the real placement."""
    monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "true")
    writes: List[Tuple[ArchivedTurn, bool]] = []

    async def _fake_write(turn, *, expect_existing):
        writes.append((turn, expect_existing))
        return True

    async def _noop(*args, **kwargs):
        return None

    with patch("apis.shared.conversation_archive.live.write_turn_guarded", _fake_write), \
            patch("apis.shared.sessions.metadata.clear_interrupted_turn", _noop), \
            patch("apis.shared.sessions.metadata.clear_pending_attachments", _noop), \
            patch("apis.shared.sessions.metadata.get_user_display_text", lambda *a: None):
        yield writes


async def _run(agent: Any, session_manager: Any, expected_exc: type = None, prompt: Any = "hello") -> None:
    async def _consume():
        async for _sse in StreamCoordinator().stream_response(
            agent=agent,
            prompt=prompt,
            session_manager=session_manager,
            session_id="sess-archive",
            user_id="user-1",
            main_agent_wrapper=None,
        ):
            pass

    if expected_exc is not None:
        with pytest.raises(expected_exc):
            await _consume()
    else:
        await _consume()
    await drain_pending()


def _history(n: int) -> List[Dict[str, Any]]:
    return [_user(f"q{i}") if i % 2 == 0 else _assistant(f"a{i}") for i in range(n)]


@pytest.mark.asyncio
async def test_completed_turn_is_queued_after_done():
    calls: List[Dict[str, Any]] = []
    order: List[str] = []

    def _fake_schedule(**kwargs):
        calls.append(kwargs)
        order.append("archive")
        return True

    async def _fake_clear(session_id, user_id):
        order.append("clear_interrupted")

    with patch("apis.shared.conversation_archive.live.schedule_turn_archive", _fake_schedule), \
            patch("apis.shared.sessions.metadata.clear_interrupted_turn", _fake_clear):
        await _drive(_CompletingAgent())

    assert len(calls) == 1
    kwargs = calls[0]
    assert (kwargs["session_id"], kwargs["user_id"]) == ("sess-complete", "user-1")
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": "hi"}]}]
    assert kwargs["appended_count"] == 0, "this fake agent's stream appends nothing to its history"
    assert order[-1] == "archive", "the archive write is the last thing the turn does"


@pytest.mark.asyncio
async def test_a_normal_multi_tool_turn_is_keyed_at_its_user_message(archived):
    agent = _ScriptedAgent(_history(4), [
        _user("find both"),
        _start(), _delta("Looking."), _stop("tool_use"), _tool_call("a", "Looking."),
        _tool_result("a"),
        _start(), _stop("tool_use"), _tool_call("b"),
        _tool_result("b"),
        _start(), _delta("Found both."), _stop(), _assistant("Found both."),
    ])
    await _run(agent, _StoredHistory(stored=4, flush_returns=3))

    [(turn, expect_existing)] = archived
    assert (turn.message_index, turn.user_text, turn.assistant_text) == (4, "find both", "Looking.\n\nFound both.")
    assert expect_existing is False


@pytest.mark.asyncio
async def test_a_resume_that_ends_on_a_tool_result_keeps_the_paused_turns_key(archived):
    """Messages 0..23 completed; user 24 made the model call ask_user_question
    (25) and the turn paused. On resume the request appended only the tool
    result (26) and the model ended the turn with no text. The old placement
    took `flush()`'s stale 24 as the last assistant index and wrote key 22."""
    history = _history(24) + [_user("plan my week"), _tool_call("q", "One question first.")]
    agent = _ScriptedAgent(history, [_tool_result("q")])

    await _run(agent, _StoredHistory(stored=26, flush_returns=24),
               prompt=[{"interruptResponse": {"interruptId": "i", "response": {"answers": {}}}}])

    [(turn, expect_existing)] = archived
    assert turn.message_index == 24
    assert (turn.user_text, turn.assistant_text) == ("plan my week", "One question first.")
    assert expect_existing is True, "a resumed turn may only replace its own paused copy"


@pytest.mark.asyncio
async def test_a_resume_with_text_keeps_the_paused_turns_key(archived):
    history = _history(24) + [_user("plan my week"), _tool_call("q", "One question first.")]
    agent = _ScriptedAgent(history, [
        _tool_result("q"),
        _start(), _delta("Here is the plan."), _stop(), _assistant("Here is the plan."),
    ])

    await _run(agent, _StoredHistory(stored=26, flush_returns=24))

    [(turn, expect_existing)] = archived
    assert (turn.message_index, turn.assistant_text) == (24, "One question first.\n\nHere is the plan.")
    assert expect_existing is True


@pytest.mark.asyncio
async def test_a_stopped_turn_archives_the_partial_it_persisted(archived):
    """Stop / disconnect mid-answer: the partial is persisted to Memory, so it
    is archived too, under the turn's user message."""
    agent = _ScriptedAgent(_history(2), [_user("write an essay"), _start(), _delta("Once upon")],
                           exc=asyncio.CancelledError())
    persisted: List[Any] = []

    class _Recording:
        def create_message(self, session_id, agent_id, session_message):
            persisted.append(session_message)

    async def _noop(*args, **kwargs):
        return None

    with patch("agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
               lambda **kw: _Recording()), \
            patch("apis.shared.sessions.metadata.set_interrupted_turn", _noop):
        await _run(agent, _StoredHistory(stored=2, flush_returns=1), expected_exc=asyncio.CancelledError)

    assert len(persisted) == 1
    [(turn, expect_existing)] = archived
    assert (turn.message_index, turn.user_text, turn.assistant_text) == (2, "write an essay", "Once upon")
    assert expect_existing is False


@pytest.mark.asyncio
async def test_a_turn_stopped_before_any_text_archives_the_question_without_the_placeholder(archived):
    agent = _ScriptedAgent(_history(2), [_user("write an essay")], exc=asyncio.CancelledError())

    class _Recording:
        def create_message(self, session_id, agent_id, session_message):
            pass

    async def _noop(*args, **kwargs):
        return None

    with patch("agents.main_agent.session.session_factory.SessionFactory.create_session_manager",
               lambda **kw: _Recording()), \
            patch("apis.shared.sessions.metadata.set_interrupted_turn", _noop):
        await _run(agent, _StoredHistory(stored=2, flush_returns=1), expected_exc=asyncio.CancelledError)

    [(turn, _)] = archived
    assert (turn.message_index, turn.user_text, turn.assistant_text) == (2, "write an essay", "")


@pytest.mark.asyncio
async def test_an_interrupted_turn_is_not_archived_by_the_success_path():
    calls: List[Any] = []

    async def _fake_persist(self, **kwargs):
        calls.append(("persist", kwargs["archive_appended_count"]))

    with patch("apis.shared.conversation_archive.live.schedule_turn_archive",
               lambda **kw: calls.append(("success", kw))), \
            patch.object(StreamCoordinator, "_persist_interruption", _fake_persist):
        await _drive(_InterruptedAgent(), asyncio.CancelledError)

    assert calls == [("persist", 0)], "the interruption arm owns the archive of a cut turn"


@pytest.mark.asyncio
async def test_a_scheduling_failure_never_breaks_the_stream():
    def _boom(**kwargs):
        raise RuntimeError("s3 down")

    with patch("apis.shared.conversation_archive.live.schedule_turn_archive", _boom):
        await _drive(_CompletingAgent())
