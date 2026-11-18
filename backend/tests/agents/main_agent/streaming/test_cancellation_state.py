"""Cancellation must stop the running turn — and only the running turn.

Two defects live here, and they pull in opposite directions.

**Cancel was too weak.** A Stop flips ``session_manager.cancelled``, which
StopHook reads at *tool boundaries* and the stream coordinator reads *between
stream events*. Neither regains control while an MCP ``tools/call`` is in
flight, so a Stop pressed during a slow MCP call ran that call to completion.
Strands forwards its cancel signal into the in-flight MCP call and skips the
tools queued behind it. The first attempt reached it through
``agent.cancel()``, but the route passes our ``ChatAgent`` wrapper, which has no
``cancel``, so the guarded call never ran. The turn now passes its own
``cancel_signal`` to ``stream_async``, and the heartbeat sets that event.

**Cancel was too sticky.** The agent cache hands the *same* ``Agent`` and the
same ``TurnBasedSessionManager`` to the next turn (#741/#751), so a flag left
set by one Stop cancelled every later turn. ``session_manager.cancelled`` is
reset at the head of each turn. The Strands half is per turn by construction:
an event created for one invocation cannot be seen by the next.

The SDK-contract tests at the bottom bind against the installed SDK, so a pin
that drops the ``cancel_signal`` plumbing fails here rather than silently
returning Stop to running MCP calls to completion.
"""

import asyncio
import inspect
import threading
from typing import Any, AsyncIterable

from strands import Agent, tool
from strands.models.model import Model
from strands.types.tools import ToolContext

from agents.main_agent.streaming.stream_coordinator import reset_cancellation_state
from apis.inference_api.chat.routes import _mark_session_cancelled


class _SessionManager:
    def __init__(self, cancelled=False):
        self.cancelled = cancelled


class _Wrapper:
    """Shaped like ``ChatAgent``: a session manager, and deliberately no ``cancel``."""

    def __init__(self, session_manager=None):
        self.session_manager = session_manager


class TestMarkSessionCancelled:
    """A Stop must arm the flag and this turn's Strands signal."""

    def test_arms_both_the_flag_and_the_turn_signal(self):
        session_manager = _SessionManager()
        signal = threading.Event()

        _mark_session_cancelled(_Wrapper(session_manager), signal)

        assert session_manager.cancelled is True
        # The half that reaches an in-flight MCP call.
        assert signal.is_set()

    def test_reaches_strands_through_a_wrapper_without_cancel(self):
        """The regression: the route passes ``ChatAgent``, which has no ``cancel``.

        Calling ``getattr(agent, "cancel", None)`` on it returned None, so the
        Strands half of every Stop was silently skipped in production.
        """
        from agents.main_agent.chat_agent import ChatAgent

        assert not hasattr(ChatAgent, "cancel")
        signal = threading.Event()

        _mark_session_cancelled(_Wrapper(_SessionManager()), signal)

        assert signal.is_set()

    def test_no_session_manager_still_sets_the_signal(self):
        signal = threading.Event()

        _mark_session_cancelled(_Wrapper(session_manager=None), signal)

        assert signal.is_set()

    def test_no_signal_still_sets_the_flag(self):
        """Callers without a turn signal (none today) keep the flag half."""
        wrapper = _Wrapper(_SessionManager())

        _mark_session_cancelled(wrapper)

        assert wrapper.session_manager.cancelled is True

    def test_is_idempotent(self):
        wrapper = _Wrapper(_SessionManager())
        signal = threading.Event()

        _mark_session_cancelled(wrapper, signal)
        _mark_session_cancelled(wrapper, signal)

        assert wrapper.session_manager.cancelled is True
        assert signal.is_set()


class TestResetCancellationState:
    """The next turn on a cached agent must start uncancelled."""

    def test_clears_a_sticky_session_manager_flag(self):
        """The bug: one Stop bricked every later turn in the session."""
        session_manager = _SessionManager(cancelled=True)

        reset_cancellation_state(object(), session_manager)

        assert session_manager.cancelled is False

    def test_leaves_an_uncancelled_turn_alone(self):
        session_manager = _SessionManager(cancelled=False)

        reset_cancellation_state(object(), session_manager)

        assert session_manager.cancelled is False

    def test_never_touches_the_strands_signal(self):
        """Strands' internal event is no longer ours to clear."""

        class _Agent:
            def __init__(self):
                self._cancel_signal = threading.Event()
                self._cancel_signal.set()

        agent = _Agent()
        reset_cancellation_state(agent, _SessionManager(cancelled=True))

        assert agent._cancel_signal.is_set()

    def test_tolerates_missing_surfaces(self):
        """Voice/preview paths pass differently-shaped objects; never raise."""
        reset_cancellation_state(object(), None)


# ------------------------------------------------- through a real Strands agent


class _SlowToolModel(Model):
    """Calls ``slow_lookup`` once, then answers in text."""

    def update_config(self, **model_config: Any) -> None:  # pragma: no cover
        pass

    def get_config(self) -> Any:  # pragma: no cover
        return {}

    def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, *args: Any, **kwargs: Any) -> AsyncIterable[dict]:
        answered = any("toolResult" in block for block in messages[-1]["content"])
        yield {"messageStart": {"role": "assistant"}}
        if answered:
            yield {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}}
            yield {"contentBlockDelta": {"delta": {"text": "Done."}, "contentBlockIndex": 0}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        yield {
            "contentBlockStart": {
                "start": {"toolUse": {"name": "slow_lookup", "toolUseId": "tu-1"}},
                "contentBlockIndex": 0,
            }
        }
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}, "contentBlockIndex": 0}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def _agent_with_slow_tool(observed: dict) -> Agent:
    @tool(context=True)
    async def slow_lookup(tool_context: ToolContext) -> str:
        """Wait the way an in-flight MCP call does, watching the cancel signal."""
        observed["started"].set()
        if observed.get("fast"):
            return "found it"
        for _ in range(200):  # 10 s ceiling, far past the test's wait
            if tool_context.cancel_signal.is_set():
                observed["saw_cancel"] = True
                return "cancelled mid-call"
            await asyncio.sleep(0.05)
        return "ran to completion"

    return Agent(model=_SlowToolModel(), tools=[slow_lookup], callback_handler=None)


async def _drain(stream) -> Any:
    result = None
    async for event in stream:
        if "result" in event:
            result = event["result"]
    return result


class TestPerTurnSignalThroughStrands:
    """The signal the route sets must reach a running tool, and only that turn."""

    def test_a_set_turn_signal_reaches_the_in_flight_tool(self):
        observed = {"started": None, "saw_cancel": False}

        async def scenario():
            observed["started"] = asyncio.Event()
            agent = _agent_with_slow_tool(observed)
            signal = threading.Event()
            run = asyncio.create_task(_drain(agent.stream_async("look it up", cancel_signal=signal)))
            await asyncio.wait_for(observed["started"].wait(), timeout=5)
            _mark_session_cancelled(_Wrapper(_SessionManager()), signal)
            return await asyncio.wait_for(run, timeout=5), agent

        result, agent = asyncio.run(scenario())

        assert observed["saw_cancel"] is True
        assert result.stop_reason == "cancelled"
        # Strands cleared its internal event when the invocation ended.
        assert not agent.cancel_signal.is_set()

    def test_a_late_cancel_on_a_finished_turn_cannot_cancel_the_next_one(self):
        """The sticky-Stop race: the heartbeat observes a Stop just as a turn ends."""
        observed = {"started": None, "saw_cancel": False, "fast": True}

        async def scenario():
            observed["started"] = asyncio.Event()
            agent = _agent_with_slow_tool(observed)
            first = threading.Event()
            await _drain(agent.stream_async("look it up", cancel_signal=first))
            # The heartbeat sets the finished turn's event, with nothing running.
            first.set()
            return await _drain(agent.stream_async("again", cancel_signal=threading.Event())), agent

        second_result, agent = asyncio.run(scenario())

        assert second_result.stop_reason == "end_turn"
        assert not agent.cancel_signal.is_set()

    def test_agent_cancel_between_turns_is_why_we_do_not_use_it(self):
        """Pins the hazard the per-turn event avoids.

        ``agent.cancel()`` sets Strands' internal event, which ``stream_async``
        clears only when an invocation ends. Armed between turns, it cancels the
        next one, which is why the old code needed a private ``_cancel_signal``
        reset at the head of every turn. If upstream ever clears it at the start
        of an invocation, this test fails and the note in
        ``reset_cancellation_state`` can go.
        """
        observed = {"started": None, "saw_cancel": False, "fast": True}

        async def scenario():
            observed["started"] = asyncio.Event()
            agent = _agent_with_slow_tool(observed)
            await _drain(agent.stream_async("look it up"))
            agent.cancel()
            return await _drain(agent.stream_async("again"))

        assert asyncio.run(scenario()).stop_reason == "cancelled"


class TestStrandsCancellationContract:
    """Bind against the installed SDK, not a stub.

    Everything that makes the turn signal *useful* is inside Strands: the
    invocation mirrors a caller-owned event onto its internal one, the executor
    checks that before each queued tool, and the MCP tool forwards it into the
    in-flight call. If a future pin drops any of these, Stop silently goes back
    to running MCP calls to completion, with every test above that uses a stub
    still green.
    """

    def test_stream_async_accepts_a_cancel_signal(self):
        from strands import Agent

        assert "cancel_signal" in inspect.signature(Agent.stream_async).parameters

    def test_observe_cancellation_mirrors_the_external_signal(self):
        from strands import Agent

        source = inspect.getsource(Agent._observe_cancellation)
        assert "_external_cancel_signal" in source

    def test_sequential_executor_honors_the_cancel_signal(self):
        """Queued tools must be skipped once cancel is armed."""
        from strands.tools.executors import sequential

        source = inspect.getsource(sequential)
        assert "_cancel_signal" in source or "_observe_cancellation" in source

    def test_mcp_tool_forwards_the_cancel_signal(self):
        """The in-flight MCP call must see the signal."""
        from strands.tools.mcp import mcp_agent_tool

        assert "cancel_signal" in inspect.getsource(mcp_agent_tool)

    def test_mcp_client_accepts_a_per_call_cancel_signal(self):
        from strands.tools.mcp import mcp_client

        assert "cancel_signal" in inspect.getsource(mcp_client)
