"""The concurrent `agent_status` drain (docs/specs/agent-state-feedback.md PR-2).

The status hook records model-call and tool-call boundaries from inside
Strands' event loop, which has no route to the SSE stream — so the coordinator
drains it. Draining only from the emit loop meant a transition could not leave
the container until the agent stream produced its next event, and during tool
execution the agent stream produces NOTHING. A `tool_start` therefore waited
out exactly the silence it existed to explain.

That bug is invisible to an ordering assertion: the old drain ran just before
each event was yielded, so the frames came out in the right ORDER and hours
late. What these tests pin is TIMING — did the status frame reach the consumer
while the agent stream was still silent, or only once it spoke again — and they
do it without wall-clock thresholds, by asking the fake stream whether it had
yielded yet at the moment each frame arrived.
"""

import asyncio
import contextvars
import json
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


class _Hook:
    """The seams the coordinator uses, plus a way to record mid-silence."""

    def __init__(self) -> None:
        self._statuses: List[dict] = []
        self.drains = 0

    def record(self, status: dict) -> None:
        self._statuses.append(status)

    def drain_statuses(self) -> List[dict]:
        self.drains += 1
        statuses, self._statuses = self._statuses, []
        return statuses

    def drain_batches(self) -> List[dict]:
        return []


class _Wrapper:
    def __init__(self, hook: Optional[_Hook]) -> None:
        if hook is not None:
            self.agent_status_hook = hook


class _SessionManager:
    def __init__(self) -> None:
        self.cancelled = False

    async def update_after_turn(self, input_tokens, current_messages=None):
        return None


class _StallingAgent:
    """An agent stream that goes quiet exactly the way a tool call does.

    It yields one event, then records a `tool_start` and stalls — no events at
    all while the "tool runs" — then records `tool_end` and speaks again.
    `spoke_again` flips at the moment of that second yield, which is what lets
    the tests tell "arrived during the silence" from "arrived after it".
    """

    def __init__(self, hook: _Hook, silence: float = 0.4) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self._hook = hook
        self._silence = silence
        self.spoke_again = False

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            yield {"event": {"messageStart": {"role": "assistant"}}}

            self._hook.record(
                {"phase": "tool_start", "cycle": 1, "toolName": "list_assignments"}
            )
            await asyncio.sleep(self._silence)
            self._hook.record(
                {"phase": "tool_end", "cycle": 1, "toolName": "list_assignments"}
            )

            self.spoke_again = True
            yield {"event": {"messageStop": {"stopReason": "end_turn"}}}

        return _gen()


async def _collect_with_arrival(agent, wrapper) -> List[tuple]:
    """Every frame, paired with whether the stream had spoken again yet."""
    coordinator = StreamCoordinator()
    seen: List[tuple] = []
    async for sse in coordinator.stream_response(
        agent=agent,
        prompt="hi",
        session_manager=_SessionManager(),
        session_id="sess-1",
        user_id="user-1",
        main_agent_wrapper=wrapper,
    ):
        seen.append((sse, agent.spoke_again))
    return seen


def _status_payloads(frames) -> List[dict]:
    prefix = "event: agent_status\ndata: "
    return [
        json.loads(f[len(prefix) :].strip())
        for f, _ in frames
        if f.startswith(prefix)
    ]


def _arrival_of_phase(frames, phase: str) -> bool:
    """`spoke_again` at the moment the frame for `phase` arrived."""
    prefix = "event: agent_status\ndata: "
    for sse, spoke_again in frames:
        if not sse.startswith(prefix):
            continue
        if json.loads(sse[len(prefix) :].strip()).get("phase") == phase:
            return spoke_again
    raise AssertionError(f"no agent_status frame for phase {phase!r}")


class TestLiveDrain:
    @pytest.mark.asyncio
    async def test_tool_start_reaches_the_client_during_the_silence(self, monkeypatch):
        """The whole point: narrate the tool WHILE it runs."""
        monkeypatch.delenv("AGENT_STATUS_LIVE_DRAIN_ENABLED", raising=False)
        hook = _Hook()
        agent = _StallingAgent(hook)

        frames = await _collect_with_arrival(agent, _Wrapper(hook))

        assert _arrival_of_phase(frames, "tool_start") is False

    @pytest.mark.asyncio
    async def test_the_kill_switch_restores_the_between_yields_drain(self, monkeypatch):
        """Off, the frame waits for the stream to speak — the old behaviour."""
        monkeypatch.setenv("AGENT_STATUS_LIVE_DRAIN_ENABLED", "false")
        hook = _Hook()
        agent = _StallingAgent(hook)

        frames = await _collect_with_arrival(agent, _Wrapper(hook))

        # Still delivered, and still in order — just late, which is the bug.
        assert _arrival_of_phase(frames, "tool_start") is True
        phases = [p["phase"] for p in _status_payloads(frames)]
        assert phases == ["tool_start", "tool_end"]

    @pytest.mark.asyncio
    async def test_every_transition_is_delivered_exactly_once(self, monkeypatch):
        """A status line that stutters backwards is worse than none."""
        monkeypatch.delenv("AGENT_STATUS_LIVE_DRAIN_ENABLED", raising=False)
        hook = _Hook()

        frames = await _collect_with_arrival(_StallingAgent(hook), _Wrapper(hook))

        phases = [p["phase"] for p in _status_payloads(frames)]
        assert phases == ["tool_start", "tool_end"]

    @pytest.mark.asyncio
    async def test_sessionid_is_stamped_on_every_frame(self, monkeypatch):
        monkeypatch.delenv("AGENT_STATUS_LIVE_DRAIN_ENABLED", raising=False)
        hook = _Hook()

        frames = await _collect_with_arrival(_StallingAgent(hook), _Wrapper(hook))

        assert all(p["sessionId"] == "sess-1" for p in _status_payloads(frames))
        assert _status_payloads(frames)  # not vacuous


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_a_wrapper_without_the_hook_streams_normally(self, monkeypatch):
        """Voice and tests build wrappers with no status hook."""
        monkeypatch.delenv("AGENT_STATUS_LIVE_DRAIN_ENABLED", raising=False)
        hook = _Hook()  # recorded into, but never reachable by the coordinator
        agent = _StallingAgent(hook, silence=0.05)

        frames = await _collect_with_arrival(agent, _Wrapper(None))

        assert _status_payloads(frames) == []
        assert any(f.startswith("event: done") for f, _ in frames)

    @pytest.mark.asyncio
    async def test_a_failing_drain_does_not_break_the_turn(self, monkeypatch):
        """Narration is a nicety; it must never be able to end a turn."""
        monkeypatch.delenv("AGENT_STATUS_LIVE_DRAIN_ENABLED", raising=False)

        class _BrokenHook(_Hook):
            def drain_statuses(self):
                raise RuntimeError("hook exploded")

        hook = _BrokenHook()
        agent = _StallingAgent(hook, silence=0.05)

        frames = await _collect_with_arrival(agent, _Wrapper(hook))

        assert _status_payloads(frames) == []
        assert any(f.startswith("event: done") for f, _ in frames)


class TestEarlyExit:
    @pytest.mark.asyncio
    async def test_a_cooperative_stop_mid_silence_ends_cleanly(self, monkeypatch):
        """Stop abandons the merge, which must unwind the agent stream.

        The merge holds an in-flight `__anext__` across its poll timeouts, so
        this is the path where that task gets cancelled. A generator left
        half-closed here would surface as a hung turn or a leaked lease.
        """
        monkeypatch.delenv("AGENT_STATUS_LIVE_DRAIN_ENABLED", raising=False)
        hook = _Hook()
        agent = _StallingAgent(hook, silence=0.3)
        session_manager = _SessionManager()

        coordinator = StreamCoordinator()
        frames: List[str] = []
        async for sse in coordinator.stream_response(
            agent=agent,
            prompt="hi",
            session_manager=session_manager,
            session_id="sess-1",
            user_id="user-1",
            main_agent_wrapper=_Wrapper(hook),
        ):
            frames.append(sse)
            # Arm the stop as soon as the first status frame lands — i.e. while
            # the agent stream is still stalled inside its sleep.
            if sse.startswith("event: agent_status"):
                session_manager.cancelled = True

        assert any(f.startswith("event: done") for f in frames)


_ACTIVE_SPAN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_ACTIVE_SPAN", default=None
)


class _SpanHoldingAgent(_StallingAgent):
    """Holds a context value open across its yields, the way Strands' spans do.

    Strands wraps the invocation, each cycle and each model stream in
    ``use_span``, which sets an OpenTelemetry ContextVar on entry and resets it
    with the token on exit — with yields in between. The reset only succeeds in
    the Context that made the token, and anything opened from the ambient
    context during the silence (a Memory ``CreateEvent`` span) parents to
    whatever is current then.
    """

    def __init__(self, hook: _Hook) -> None:
        super().__init__(hook, silence=0.25)
        self.seen_during_silence: Optional[str] = None
        self.reset_error: Optional[BaseException] = None

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            token = _ACTIVE_SPAN.set("execute_event_loop_cycle")
            yield {"event": {"messageStart": {"role": "assistant"}}}

            self._hook.record({"phase": "tool_start", "cycle": 1, "toolName": "t"})
            await asyncio.sleep(self._silence)
            self.seen_during_silence = _ACTIVE_SPAN.get()

            self.spoke_again = True
            yield {"event": {"messageStop": {"stopReason": "end_turn"}}}
            try:
                _ACTIVE_SPAN.reset(token)
            except ValueError as exc:
                self.reset_error = exc

        return _gen()


class TestContextAcrossSteps:
    @pytest.mark.asyncio
    async def test_a_value_set_before_a_yield_resets_after_it(self, monkeypatch):
        """Each `__anext__` is its own task; they must share one Context.

        A fresh context copy per step made every span Strands held across a
        yield fail to detach ("was created in a different Context"), ~5 logged
        errors per tool turn in the runtime.
        """
        monkeypatch.delenv("AGENT_STATUS_LIVE_DRAIN_ENABLED", raising=False)
        hook = _Hook()
        agent = _SpanHoldingAgent(hook)

        frames = await _collect_with_arrival(agent, _Wrapper(hook))

        assert _arrival_of_phase(frames, "tool_start") is False  # merge engaged
        assert agent.reset_error is None

    @pytest.mark.asyncio
    async def test_the_ambient_value_survives_into_the_next_step(self, monkeypatch):
        """Otherwise spans opened mid-turn parent to the request span."""
        monkeypatch.delenv("AGENT_STATUS_LIVE_DRAIN_ENABLED", raising=False)
        hook = _Hook()
        agent = _SpanHoldingAgent(hook)

        await _collect_with_arrival(agent, _Wrapper(hook))

        assert agent.seen_during_silence == "execute_event_loop_cycle"
