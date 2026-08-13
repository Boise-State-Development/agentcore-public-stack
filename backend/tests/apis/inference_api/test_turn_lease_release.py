"""Turn-lease teardown survives the cancellation that triggers it.

`_release_turn_lease` runs from the SSE stream generator's `finally`, and the
case it exists for is the one where cancellation is what put it there: the
browser's connection drops and Starlette tears the response down. A bare
`await` in that `finally` never completes, so the release is abandoned — the
lease then survives its full 90s window and the user's resend is rejected as a
duplicate turn (409 → the Runtime's 424 rewrite → "Chat Request Failed"),
seconds after the dropped stream already showed them a network error.

The mechanism is specifically **anyio cancel scopes**, which is what Starlette
cancels a disconnected `StreamingResponse` with. Unlike a one-shot
`task.cancel()` — which lets a `finally` run its awaits to completion — an
anyio scope is level-triggered: while it is cancelled, *every* checkpoint
inside it raises `CancelledError`, including the ones in cleanup code. Tests
that cancel a bare asyncio task therefore pass with or without the fix and
prove nothing; these use a real cancel scope.

Observed in prod-ai 2026-08-13: session 938a1e68 acquired a lease at 14:28:44,
was interrupted (`reason=connection_lost`) at 14:30:15 with no release ever
logged, and its resend at 14:31:16 was rejected. Twice in twelve minutes.
"""

from __future__ import annotations

import asyncio

import anyio
import pytest

import apis.shared.sessions.session_lease as session_lease_module
from apis.inference_api.chat.routes import _release_turn_lease
from apis.shared.sessions.session_lease import SessionLease


def _lease() -> SessionLease:
    return SessionLease(session_id="s1", user_id="u1", owner="owner-token")


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record released leases, with a real suspension point in the release.

    The suspension is the whole point: DynamoDB's round-trip is where a
    cancelled `finally` abandons the work.
    """
    seen: list = []

    async def _release(lease) -> None:
        await asyncio.sleep(0.01)
        seen.append(lease)

    monkeypatch.setattr(session_lease_module, "release_session_lease", _release)
    return seen


async def _stream_dropped_mid_turn(lease, heartbeat_task=None) -> None:
    """Reproduce a client disconnect the way Starlette delivers one: cancel the
    surrounding anyio scope while the stream generator is suspended, so the
    generator's `finally` runs inside an already-cancelled scope."""
    with anyio.CancelScope() as scope:

        async def stream():
            try:
                while True:
                    yield "chunk"
                    scope.cancel()  # the browser goes away
                    await anyio.sleep(0.01)  # raises CancelledError here
            finally:
                await _release_turn_lease(heartbeat_task, lease)

        async for _ in stream():
            pass


class TestReleaseUnderClientDisconnect:
    @pytest.mark.asyncio
    async def test_lease_is_released_when_the_connection_drops(self, released):
        lease = _lease()
        await _stream_dropped_mid_turn(lease)

        # The shielded release runs on its own task, outside the cancelled
        # scope — give the loop a beat to finish it.
        await asyncio.sleep(0.05)
        assert released == [lease], (
            "lease was not released on a dropped stream — the user's resend "
            "will be rejected as a duplicate turn"
        )

    @pytest.mark.asyncio
    async def test_heartbeat_is_stopped_before_release(self, released):
        """A surviving heartbeat would keep renewing the lease we just freed."""
        started = asyncio.Event()

        async def _heartbeat() -> None:
            started.set()
            while True:
                await asyncio.sleep(0.01)

        heartbeat_task = asyncio.create_task(_heartbeat())
        await started.wait()

        lease = _lease()
        await _stream_dropped_mid_turn(lease, heartbeat_task)
        await asyncio.sleep(0.05)

        assert heartbeat_task.cancelled()
        assert released == [lease]


class TestReleaseUnderTaskCancellation:
    @pytest.mark.asyncio
    async def test_cancellation_still_propagates(self, released):
        """Swallowing CancelledError in the teardown must not swallow the
        cancellation that unwound the stream — the coordinator's interruption
        arm and Starlette's teardown both depend on it propagating."""
        lease = _lease()

        async def consume():
            async def stream():
                try:
                    while True:
                        yield "chunk"
                        await asyncio.sleep(0.01)
                finally:
                    await _release_turn_lease(None, lease)

            async for _ in stream():
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.03)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.sleep(0.05)
        assert released == [lease]


class TestReleaseOnNormalCompletion:
    @pytest.mark.asyncio
    async def test_lease_is_released_when_the_stream_ends_normally(self, released):
        lease = _lease()

        async def stream():
            try:
                yield "chunk"
            finally:
                await _release_turn_lease(None, lease)

        async for _ in stream():
            pass

        assert released == [lease]

    @pytest.mark.asyncio
    async def test_no_lease_is_a_noop(self, released):
        # Preview sessions and the local no-DynamoDB path never take a lease;
        # release_session_lease is itself a no-op on None.
        await _release_turn_lease(None, None)
        assert released == [None]
