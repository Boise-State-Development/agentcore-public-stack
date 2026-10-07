"""A finished turn is queued for the conversation archive; a failed one is not.

The archive write is the only thing conversation search adds to the turn path,
and it must sit after `done` and never be awaited (docs/specs/conversation-search.md
§1, §4). These tests drive the real coordinator and stand in for the scheduler.
"""

import asyncio
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator
from tests.agents.main_agent.streaming.test_completed_turn_clears_interrupt import (
    _CompletingAgent,
    _drive,
    _InterruptedAgent,
)


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
    assert kwargs["last_message_index"] == kwargs["turn_first_index"] + 1
    assert order[-1] == "archive", "the archive write is the last thing the turn does"


@pytest.mark.asyncio
async def test_interrupted_turn_is_not_archived():
    calls: List[Any] = []

    async def _fake_persist(self, **kwargs):
        return None

    with patch("apis.shared.conversation_archive.live.schedule_turn_archive",
               lambda **kw: calls.append(kw)), \
            patch.object(StreamCoordinator, "_persist_interruption", _fake_persist):
        await _drive(_InterruptedAgent(), asyncio.CancelledError)

    assert calls == []


@pytest.mark.asyncio
async def test_a_scheduling_failure_never_breaks_the_stream():
    def _boom(**kwargs):
        raise RuntimeError("s3 down")

    with patch("apis.shared.conversation_archive.live.schedule_turn_archive", _boom):
        await _drive(_CompletingAgent())
