"""The deferred, narrated agent build (docs/specs/agent-state-feedback.md PR-3).

Measured on dev: a cold agent-cache miss spends ~1500ms inside `get_agent`, and
because FastAPI flushes response headers when the handler returns its
`StreamingResponse`, every millisecond of that is dead air. PR-3 defers the
build into the stream generator so the response opens first and the wait can be
narrated.

**The timing decision is NOT made here.** It used to be: the generator raced the
build against a 250ms timer and emitted the frame only if the build was still
running, so a warm build never flashed a phase nobody can read. That cannot
work — `create_agent` is synchronous, so a cold build occupies the event loop
for its whole duration and `asyncio.wait` never fires its timeout. Verified on
dev: a 1548ms build, six times the threshold, emitted nothing. The frame is now
unconditional and the SPA holds it for 250ms before rendering.

So what is pinned here is what the server still owns: the frame goes out BEFORE
the build, a failed build surfaces instead of hanging, and the lease is released
either way.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator, List, Optional

import pytest

# Module level on purpose. `backend/tests/apis/__init__.py` makes `tests/apis`
# a package ALSO named `apis`, and pytest puts `backend/tests` on sys.path, so
# a file whose first `apis.` import happens later — inside a fixture, say —
# can bind `apis` to the TEST package and fail on `apis.shared.caching`, which
# only exists under `src`. Importing here binds it to the real one first, which
# is why every other test file in this tree does the same.
import apis.inference_api.chat.routes as routes_module


def _frames_of(kind: str, frames: List[str]) -> List[dict]:
    prefix = f"event: {kind}\ndata: "
    return [
        json.loads(f[len(prefix) :].strip())
        for f in frames
        if f.startswith(prefix)
    ]


class _Harness:
    """A faithful copy of the generator's build-and-narrate preamble.

    The real `_guarded_stream` is a closure over ~40 locals inside a
    1700-line handler; reproducing its *decision* here keeps the test on the
    behaviour under change instead of on FastAPI wiring. Kept in step with
    `routes.py` by `TestRouteContract`.
    """

    def __init__(self, build_seconds: float, fails: bool = False) -> None:
        self.build_seconds = build_seconds
        self.fails = fails
        self.released = False
        self.built = False

    async def _build(self) -> Any:
        await asyncio.sleep(self.build_seconds)
        if self.fails:
            raise RuntimeError("tool registry exploded")
        self.built = True
        return object()

    async def stream(self) -> AsyncGenerator[str, None]:
        agent: Optional[Any] = None
        try:
            yield (
                "event: agent_status\ndata: "
                + json.dumps(
                    {
                        "type": "agent_status",
                        "sessionId": "sess-1",
                        "phase": "preparing",
                    }
                )
                + "\n\n"
            )
            try:
                agent = await self._build()
            except Exception:
                yield 'event: stream_error\ndata: {"code": "AGENT_ERROR"}\n\n'
                yield "event: done\ndata: {}\n\n"
                return
            assert agent is not None
            yield (
                "event: agent_status\ndata: "
                + json.dumps(
                    {
                        "type": "agent_status",
                        "sessionId": "sess-1",
                        "phase": "prepared",
                        "durationMs": int(self.build_seconds * 1000),
                    }
                )
                + "\n\n"
            )
            yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
            yield "event: done\ndata: {}\n\n"
        finally:
            self.released = True


class TestTheFrame:
    @pytest.mark.asyncio
    async def test_is_emitted_for_every_deferred_build(self):
        """Unconditional by design — the server cannot time its own build.

        It is always paired with `prepared`: the SPA needs the start to show
        the label and the end to suppress it on a fast build.
        """
        harness = _Harness(build_seconds=0.01)

        frames = [f async for f in harness.stream()]

        assert [s["phase"] for s in _frames_of("agent_status", frames)] == [
            "preparing",
            "prepared",
        ]
        assert harness.built

    @pytest.mark.asyncio
    async def test_precedes_the_build_it_explains(self):
        """After the build it would describe a wait that had already ended."""
        harness = _Harness(build_seconds=0.3)

        frames = [f async for f in harness.stream()]

        preparing = next(i for i, f in enumerate(frames) if "preparing" in f)
        message_start = next(
            i for i, f in enumerate(frames) if f.startswith("event: message_start")
        )
        assert preparing < message_start

    @pytest.mark.asyncio
    async def test_carries_no_cycle(self):
        """`preparing` precedes the event loop, so there is no cycle to number.

        The SPA validator accepts it on that basis; a fabricated cycle would
        make the two disagree about what the phase means.
        """
        harness = _Harness(build_seconds=0.01)

        frames = [f async for f in harness.stream()]

        assert "cycle" not in _frames_of("agent_status", frames)[0]


class TestTheEndFrame:
    """`prepared` is what lets the SPA suppress a fast build.

    Without it the client can only infer the build ended from `thinking`,
    which does not arrive until the head-of-turn work and the event loop's
    startup have also run — well past the 250ms the SPA waits. Measured on
    dev, builds of 0ms, 1ms and 40ms all rendered "Getting ready…" because of
    exactly that gap.
    """

    @pytest.mark.asyncio
    async def test_follows_the_build(self):
        harness = _Harness(build_seconds=0.05)

        frames = [f async for f in harness.stream()]

        assert [s["phase"] for s in _frames_of("agent_status", frames)] == [
            "preparing",
            "prepared",
        ]

    @pytest.mark.asyncio
    async def test_precedes_the_answer(self):
        """It must land before `message_start`, or the label it clears is
        already competing with streamed text."""
        harness = _Harness(build_seconds=0.05)

        frames = [f async for f in harness.stream()]

        prepared = next(i for i, f in enumerate(frames) if "prepared" in f)
        message_start = next(
            i for i, f in enumerate(frames) if f.startswith("event: message_start")
        )
        assert prepared < message_start

    @pytest.mark.asyncio
    async def test_carries_the_build_duration(self):
        """A bare marker would end the label; the duration also says how long
        the wait the user was told about actually took."""
        harness = _Harness(build_seconds=0.05)

        frames = [f async for f in harness.stream()]

        prepared = _frames_of("agent_status", frames)[1]
        assert prepared["durationMs"] >= 0

    @pytest.mark.asyncio
    async def test_is_not_emitted_when_the_build_fails(self):
        """Nothing was prepared. The error frame is what ends the label."""
        harness = _Harness(build_seconds=0.01, fails=True)

        frames = [f async for f in harness.stream()]

        assert [s["phase"] for s in _frames_of("agent_status", frames)] == [
            "preparing"
        ]
        assert any(f.startswith("event: stream_error") for f in frames)


class TestFailure:
    @pytest.mark.asyncio
    async def test_a_failed_build_surfaces_as_a_conversational_error(self):
        """The handler has already returned, so its `except` arms cannot see
        this. Without the in-generator catch it is a silent hung stream."""
        harness = _Harness(build_seconds=0.01, fails=True)

        frames = [f async for f in harness.stream()]

        assert any(f.startswith("event: stream_error") for f in frames)
        assert any(f.startswith("event: done") for f in frames)
        assert not any(f.startswith("event: message_start") for f in frames)

    @pytest.mark.asyncio
    async def test_a_failed_build_still_releases_the_lease(self):
        harness = _Harness(build_seconds=0.01, fails=True)

        [f async for f in harness.stream()]

        assert harness.released


class TestRouteContract:
    def test_route_still_matches_this_shape(self):
        """Guards the harness above against the route drifting away from it."""
        source = Path(routes_module.__file__).read_text()

        assert '"phase": "preparing"' in source
        # The end frame, without which a 1ms build still renders the label.
        assert '"phase": "prepared"' in source
        assert "Deferred agent build failed" in source

    def test_the_route_no_longer_races_its_own_build(self):
        """The regression this file exists to prevent a second time.

        A server-side timer around a synchronous build cannot fire, so any
        reappearance of one here means the frame has silently stopped being
        sent again.
        """
        source = Path(routes_module.__file__).read_text()

        assert "_PREPARING_NOTICE_SECONDS" not in source
