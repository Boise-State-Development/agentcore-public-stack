"""A turn that completes must not leave an interrupted-turn marker behind.

The client's Stop writes `lastTurnInterrupted` immediately (app-api,
source=client_signal), but the server only observes the armed cancel on the
lease heartbeat, which sleeps LEASE_HEARTBEAT_SECONDS (10s) BEFORE its first
check. A turn that finishes inside that window races the first tick and wins:
the stream completes normally, the cooperative-stop arm never runs, and a
marker is left describing a turn that was never cut short.

Left in place, the NEXT turn pops it and prepends
`_build_interruption_note("user_stopped")` — telling the model its own
COMPLETE reply "was the partial that was delivered" and to treat it as
rejected feedback. Observed in dev 2026-09-02: Stop at 2.6s on a 10.4s turn,
full answer persisted, follow-up turn logged
"Cleared interrupted_turn ... (reason=user_stopped)".

Narrowing the heartbeat interval cannot fix this — a turn shorter than one
tick is unstoppable however the ticks are spaced — so the marker has to be
reconciled against what actually happened.
"""

import asyncio
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


class _CompletingAgent:
    """Agent whose stream yields a full message and then ends cleanly."""

    def __init__(self, events: List[Dict[str, Any]] = None) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self._events = events if events is not None else [
            {"event": {"messageStart": {"role": "assistant"}}},
            {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "All done."}}}},
            {"event": {"messageStop": {"stopReason": "end_turn"}}},
        ]

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            for event in self._events:
                yield event

        return _gen()


class _InterruptedAgent:
    """Agent torn down mid-stream, as a client disconnect does."""

    def __init__(self) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            yield {"event": {"messageStart": {"role": "assistant"}}}
            yield {"event": {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "half"}}}}
            raise asyncio.CancelledError()

        return _gen()


class _NoopSessionManager:
    cancelled = False

    async def update_after_turn(self, input_tokens: int, current_messages=None):
        return None


async def _drive(agent: Any, expected_exc: type = None) -> None:
    coordinator = StreamCoordinator()

    async def _run():
        async for _sse in coordinator.stream_response(
            agent=agent,
            prompt="hello",
            session_manager=_NoopSessionManager(),
            session_id="sess-complete",
            user_id="user-1",
            main_agent_wrapper=None,
        ):
            pass

    if expected_exc is not None:
        with pytest.raises(expected_exc):
            await _run()
    else:
        await _run()


@pytest.mark.asyncio
async def test_completed_turn_clears_a_stale_interrupt_marker():
    cleared: List[Any] = []

    async def _fake_clear(session_id, user_id):
        cleared.append((session_id, user_id))
        return None

    with patch("apis.shared.sessions.metadata.clear_interrupted_turn", _fake_clear):
        await _drive(_CompletingAgent())

    assert cleared == [("sess-complete", "user-1")], (
        "a turn that produced a complete answer must reconcile away the "
        "client-signalled interrupted marker"
    )


@pytest.mark.asyncio
async def test_interrupted_turn_does_not_clear_the_marker():
    """The failure arms own the marker — clearing there would erase the real
    interruption `_persist_interruption` is about to record."""
    cleared: List[Any] = []

    async def _fake_clear(session_id, user_id):
        cleared.append((session_id, user_id))
        return None

    async def _fake_persist(self, **kwargs):
        return None

    with patch("apis.shared.sessions.metadata.clear_interrupted_turn", _fake_clear), \
            patch.object(StreamCoordinator, "_persist_interruption", _fake_persist):
        await _drive(_InterruptedAgent(), asyncio.CancelledError)

    assert cleared == [], "an interrupted turn must keep its marker"


@pytest.mark.asyncio
async def test_clear_failure_never_breaks_the_stream():
    """Best-effort, like every sibling marker write."""

    async def _boom(session_id, user_id):
        raise RuntimeError("dynamo down")

    with patch("apis.shared.sessions.metadata.clear_interrupted_turn", _boom):
        await _drive(_CompletingAgent())
