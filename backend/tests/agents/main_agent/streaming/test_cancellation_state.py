"""Cancellation must stop the running turn — and only the running turn.

Two defects live here, and they pull in opposite directions.

**Cancel was too weak.** ``_mark_session_cancelled`` only flipped
``session_manager.cancelled``, which StopHook reads at *tool boundaries* and the
stream coordinator reads *between stream events*. Neither regains control while
an MCP ``tools/call`` is in flight, so a Stop pressed during a slow MCP call ran
that call to completion. strands-agents 1.51.0 forwards the agent's cancel signal
into the in-flight MCP call and makes the sequential executor skip the tools
queued behind it, so ``agent.cancel()`` now closes that gap.

**Cancel was too sticky.** Neither flag was ever reset. The agent cache keys on
session id and hands the *same* ``Agent`` — and the same
``TurnBasedSessionManager`` — to the next turn (#741/#751), so one Stop left the
session permanently cancelled: every subsequent tool cancelled at its boundary,
every message dropped by ``append_message``, and the coordinator raising
``_CooperativeStopSignal`` on the first event of a turn the user never stopped.
Strengthening the first defect without fixing this one would have widened it.

The SDK-contract tests at the bottom exist because both fixes depend on private
Strands internals that a future pin could drop silently. A stub would keep
passing; these bind against the installed SDK so the regression surfaces here.
"""

import inspect
import threading

from agents.main_agent.streaming.stream_coordinator import reset_cancellation_state
from apis.inference_api.chat.routes import _mark_session_cancelled


class _SessionManager:
    def __init__(self, cancelled=False):
        self.cancelled = cancelled


class _Agent:
    """Agent shaped like the real one: a session manager plus a cancel signal."""

    def __init__(self, session_manager=None, cancelled_signal=False):
        self.session_manager = session_manager
        self._cancel_signal = threading.Event()
        if cancelled_signal:
            self._cancel_signal.set()

    def cancel(self):
        self._cancel_signal.set()


class TestMarkSessionCancelled:
    """A Stop must arm both signals."""

    def test_arms_both_the_flag_and_the_strands_signal(self):
        session_manager = _SessionManager()
        agent = _Agent(session_manager)

        _mark_session_cancelled(agent)

        assert session_manager.cancelled is True
        # The half that reaches an in-flight MCP call.
        assert agent._cancel_signal.is_set()

    def test_no_session_manager_still_cancels_the_agent(self):
        agent = _Agent(session_manager=None)

        _mark_session_cancelled(agent)

        assert agent._cancel_signal.is_set()

    def test_agent_without_cancel_is_a_no_op(self):
        """A nonstandard agent must not break the heartbeat that calls this."""

        class _Bare:
            session_manager = _SessionManager()

        agent = _Bare()
        _mark_session_cancelled(agent)

        assert agent.session_manager.cancelled is True

    def test_is_idempotent(self):
        agent = _Agent(_SessionManager())

        _mark_session_cancelled(agent)
        _mark_session_cancelled(agent)

        assert agent._cancel_signal.is_set()


class TestResetCancellationState:
    """The next turn on a cached agent must start uncancelled."""

    def test_clears_a_sticky_session_manager_flag(self):
        """The bug: one Stop bricked every later turn in the session."""
        session_manager = _SessionManager(cancelled=True)
        agent = _Agent(session_manager)

        reset_cancellation_state(agent, session_manager)

        assert session_manager.cancelled is False

    def test_clears_a_stale_strands_signal(self):
        """``stream_async``'s finally only clears the signal if a turn was running.

        The lease heartbeat can observe a cancel just as a turn finishes, leaving
        the signal set with no invocation left to clear it.
        """
        agent = _Agent(_SessionManager(), cancelled_signal=True)

        reset_cancellation_state(agent, agent.session_manager)

        assert not agent._cancel_signal.is_set()

    def test_leaves_an_uncancelled_turn_alone(self):
        session_manager = _SessionManager(cancelled=False)
        agent = _Agent(session_manager)

        reset_cancellation_state(agent, session_manager)

        assert session_manager.cancelled is False
        assert not agent._cancel_signal.is_set()

    def test_tolerates_missing_surfaces(self):
        """Voice/preview paths pass differently-shaped objects; never raise."""
        reset_cancellation_state(object(), None)

    def test_a_stop_followed_by_a_reset_leaves_the_session_usable(self):
        """End to end: Stop arms both signals, the next turn clears both."""
        session_manager = _SessionManager()
        agent = _Agent(session_manager)

        _mark_session_cancelled(agent)
        reset_cancellation_state(agent, session_manager)

        assert session_manager.cancelled is False
        assert not agent._cancel_signal.is_set()


class TestStrandsCancellationContract:
    """Bind against the installed SDK, not a stub.

    ``agent.cancel()`` is public, but everything that makes it *useful* is
    private: the executor and the MCP tool both read ``_cancel_signal`` by name.
    If a future strands pin renames or drops either, our Stop silently goes back
    to running MCP calls to completion — with every test above still green.
    """

    def test_agent_exposes_cancel(self):
        from strands import Agent

        assert callable(getattr(Agent, "cancel", None))

    def test_sequential_executor_honors_the_cancel_signal(self):
        """Queued tools must be skipped once cancel is armed (new in 1.51.0)."""
        from strands.tools.executors import sequential

        assert "_cancel_signal" in inspect.getsource(sequential)

    def test_mcp_tool_forwards_the_cancel_signal(self):
        """The in-flight MCP call must see the signal (new in 1.51.0)."""
        from strands.tools.mcp import mcp_agent_tool

        assert "cancel_signal" in inspect.getsource(mcp_agent_tool)

    def test_mcp_client_accepts_a_per_call_cancel_signal(self):
        from strands.tools.mcp import mcp_client

        source = inspect.getsource(mcp_client)
        assert "cancel_signal" in source
