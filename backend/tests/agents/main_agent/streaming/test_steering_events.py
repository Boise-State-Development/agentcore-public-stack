"""The `steering_applied` SSE surface and the per-turn lease stamp.

Mid-turn steering (docs/specs/mid-turn-steering.md) has two coordinator-level
jobs, and both are per-turn state on objects the agent cache reuses:

1. **Stamp this turn's lease** onto the session manager, so ``SteeringHook``
   reads the right inbox at each tool boundary — and stamp it *unconditionally*,
   including to None, so a lease left by a previous turn on a cached agent is
   never read against a row a later turn now owns. Same discipline, same
   reason, as ``reset_cancellation_state``.
2. **Drain the hook's confirmed injections** and emit one `steering_applied`
   frame each, always ahead of `done` — the drain runs *before* each event is
   yielded, so an injection confirmed on the turn's final tool batch is not
   stranded behind the terminal frame.

Driven through the real ``stream_response`` (as the compaction-emit suite
does), stubbing only ``agent.stream_async`` and the session manager.
"""

import json
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator
from apis.shared.sessions.session_lease import SessionLease


class _FakeAgent:
    def __init__(self, raw_events: Optional[List[Dict[str, Any]]] = None) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self._raw_events = raw_events or []

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            for ev in self._raw_events:
                yield ev

        return _gen()


class _SessionManager:
    """Only the seams stream_response touches; `turn_lease` starts stale."""

    def __init__(self) -> None:
        self.cancelled = False
        self.turn_lease = SessionLease(
            session_id="s1", user_id="u1", owner="a-previous-turn"
        )

    async def update_after_turn(self, input_tokens, current_messages=None):
        return None


class _Hook:
    def __init__(self, applied: Optional[List[dict]] = None) -> None:
        self._applied = list(applied or [])
        self.drains = 0

    def drain_applied(self) -> List[dict]:
        self.drains += 1
        applied, self._applied = self._applied, []
        return applied


class _Wrapper:
    def __init__(self, hook) -> None:
        self.steering_hook = hook


async def _collect(agent, session_manager, wrapper=None, turn_lease=None) -> List[str]:
    coordinator = StreamCoordinator()
    frames: List[str] = []
    async for sse in coordinator.stream_response(
        agent=agent,
        prompt="hi",
        session_manager=session_manager,
        session_id="sess-1",
        user_id="user-1",
        main_agent_wrapper=wrapper,
        turn_lease=turn_lease,
    ):
        frames.append(sse)
    return frames


def _steering_frames(frames: List[str]) -> List[dict]:
    prefix = "event: steering_applied\ndata: "
    return [
        json.loads(f[len(prefix) :].strip())
        for f in frames
        if f.startswith(prefix)
    ]


class TestLeaseStamp:
    @pytest.mark.asyncio
    async def test_this_turns_lease_replaces_the_previous_one(self):
        lease = SessionLease(session_id="s1", user_id="u1", owner="this-turn")
        sm = _SessionManager()

        await _collect(_FakeAgent(), sm, turn_lease=lease)

        assert sm.turn_lease is lease

    @pytest.mark.asyncio
    async def test_a_turn_without_a_lease_clears_the_stale_one(self):
        """Preview sessions and local dev run with no lease.

        Leaving the previous turn's handle in place would point the steering
        hook at a row a later turn owns — the sticky-state shape #741/#751
        keep producing.
        """
        sm = _SessionManager()

        await _collect(_FakeAgent(), sm, turn_lease=None)

        assert sm.turn_lease is None


class TestSteeringApplied:
    @pytest.mark.asyncio
    async def test_emits_one_frame_per_confirmed_injection(self):
        hook = _Hook([{"id": "e1", "text": "use the other file"}])
        frames = await _collect(_FakeAgent(), _SessionManager(), _Wrapper(hook))

        payloads = _steering_frames(frames)
        assert payloads == [
            {
                "type": "steering_applied",
                "sessionId": "sess-1",
                "entryId": "e1",
                "text": "use the other file",
            }
        ]

    @pytest.mark.asyncio
    async def test_emits_nothing_when_nothing_was_injected(self):
        """The overwhelming majority of turns. The drain must stay silent."""
        hook = _Hook([])
        frames = await _collect(_FakeAgent(), _SessionManager(), _Wrapper(hook))

        assert _steering_frames(frames) == []
        assert hook.drains > 0  # drained, just empty

    @pytest.mark.asyncio
    async def test_frame_lands_before_done(self):
        """The stranding case: nothing follows the injection but `done`.

        The agent here yields no events at all, so `done` is the only frame
        the drain can precede. Draining after the yield instead would put the
        ack past the terminal event, where the SPA's state gating drops it and
        the user's follow-up stays queued forever.
        """
        hook = _Hook([{"id": "e1", "text": "hi"}])
        frames = await _collect(_FakeAgent(), _SessionManager(), _Wrapper(hook))

        steer_at = next(
            i for i, f in enumerate(frames) if f.startswith("event: steering_applied\n")
        )
        done_at = next(i for i, f in enumerate(frames) if f.startswith("event: done\n"))
        # The SPA gates events on the stream state; anything after `done` is
        # dropped unless explicitly allowlisted, and this event is not.
        assert steer_at < done_at

    @pytest.mark.asyncio
    async def test_a_wrapper_without_a_hook_is_inert(self):
        """Voice and every test double take this path."""
        frames = await _collect(_FakeAgent(), _SessionManager(), object())
        assert _steering_frames(frames) == []

    @pytest.mark.asyncio
    async def test_no_wrapper_at_all_is_inert(self):
        frames = await _collect(_FakeAgent(), _SessionManager(), None)
        assert _steering_frames(frames) == []

    @pytest.mark.asyncio
    async def test_a_failing_drain_never_breaks_the_stream(self):
        class _Exploding:
            def drain_applied(self):
                raise RuntimeError("boom")

        frames = await _collect(_FakeAgent(), _SessionManager(), _Wrapper(_Exploding()))

        assert _steering_frames(frames) == []
        assert any(f.startswith("event: done\n") for f in frames)
