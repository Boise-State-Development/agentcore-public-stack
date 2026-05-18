"""Regression: the `compaction` SSE event emits exactly once per compaction event.

The `compaction` SSE event (frontend inline "earlier messages summarized"
divider, landed in PR #243) is emitted by ``StreamCoordinator.stream_response``
from inside the single terminal ``done`` handler, gated solely on
``TurnBasedSessionManager.update_after_turn`` returning a ``CompactionResult``.

``process_agent_stream`` yields exactly one ``done`` event per turn (STEP 9,
after the raw agent stream is exhausted), so ``update_after_turn`` is awaited
exactly once and the SSE frame is yielded at most once.

This module locks that once-per-turn invariant against the *real* pipeline
(``stream_response`` → ``process_agent_stream`` → coordinator emit code),
stubbing only two narrow seams: ``agent.stream_async`` (raw Strands events)
and ``session_manager.update_after_turn`` (the compaction decision).

It is also the explicit non-regression guard for the Strands 1.40 bump.
Strands 1.40 ships proactive context compression (strands PR #2239) and feeds
``EventLoopMetrics.accumulated_usage`` on the ``AgentResult`` event — which
``_handle_metadata_events`` surfaces on the ``metadata_summary`` track. Neither
is a second emit path: proactive compression is opt-in via a
``ConversationManager``'s ``proactive_compression`` (default ``None`` → the
``BeforeModelCallEvent`` handler early-returns), and our compaction lives in a
``SessionManager`` (``TurnBasedSessionManager.update_after_turn``), a different
abstraction. The third test drives the ``metadata_summary``/accumulated-usage
surface explicitly and asserts the emit count stays at one.
"""

from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from agents.main_agent.session.compaction_models import CompactionResult
from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


# Per-call metadata raw event: Bedrock's `metadata` chunk wrapped inside
# Strands' ModelStreamChunkEvent. Same shape as the cost-attribution suite.
def _raw_metadata_event(usage: Dict[str, int]) -> Dict[str, Any]:
    return {"event": {"metadata": {"usage": usage, "metrics": {"latencyMs": 100}}}}


# Strands AgentResult event. EventLoopMetrics.accumulated_usage is summed
# across all LLM calls in the turn; _handle_metadata_events extracts it onto
# the `metadata_summary` track. This is the surface Strands 1.40's proactive
# compression also reads from — included here to prove it is not a second
# compaction-emit path.
class _FakeEventLoopMetrics:
    def __init__(self, accumulated_usage: Dict[str, int]) -> None:
        self.accumulated_usage = accumulated_usage
        self.accumulated_metrics = {"latencyMs": 250}


class _FakeAgentResult:
    def __init__(self, accumulated_usage: Dict[str, int]) -> None:
        self.metrics = _FakeEventLoopMetrics(accumulated_usage)


def _raw_agent_result_event(accumulated_usage: Dict[str, int]) -> Dict[str, Any]:
    return {"result": _FakeAgentResult(accumulated_usage)}


class _FakeAgent:
    """Minimal agent: a message list and a controllable raw event stream.

    No ``_interrupt_state`` so the coordinator's paused-turn snapshot and
    OAuth / tool-approval extractors all early-return on the ``done`` event.
    """

    def __init__(self, raw_events: List[Dict[str, Any]]) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self._raw_events = raw_events

    def stream_async(self, prompt: Any) -> AsyncIterator[Dict[str, Any]]:
        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            for ev in self._raw_events:
                yield ev

        return _gen()


class _RecordingSessionManager:
    """Stub session manager that records ``update_after_turn`` invocations.

    Only the seam the coordinator depends on is implemented; the real
    threshold/checkpoint math is covered by the TurnBasedSessionManager
    suite. This isolates the coordinator-level once-per-turn invariant.
    """

    def __init__(self, result: Optional[CompactionResult]) -> None:
        self._result = result
        self.calls: List[int] = []

    async def update_after_turn(
        self,
        input_tokens: int,
        current_messages: Optional[List[Dict]] = None,
    ) -> Optional[CompactionResult]:
        self.calls.append(input_tokens)
        return self._result


async def _collect_sse(
    agent: _FakeAgent, session_manager: _RecordingSessionManager
) -> List[str]:
    coordinator = StreamCoordinator()
    frames: List[str] = []
    async for sse in coordinator.stream_response(
        agent=agent,
        prompt="hi",
        session_manager=session_manager,
        session_id="sess-1",
        user_id="user-1",
        main_agent_wrapper=None,
    ):
        frames.append(sse)
    return frames


def _compaction_frames(frames: List[str]) -> List[str]:
    return [f for f in frames if f.startswith("event: compaction\n")]


# A turn whose summed input buckets exceed any threshold — guarantees the
# coordinator's `total_input_tokens > 0` guard passes so update_after_turn
# is consulted.
_TURN_USAGE = {"inputTokens": 150_000, "outputTokens": 80, "totalTokens": 150_080}


@pytest.mark.asyncio
async def test_compaction_sse_emitted_exactly_once_when_checkpoint_advances():
    """Checkpoint advances → exactly one `event: compaction` frame."""
    result = CompactionResult(
        previous_checkpoint=0,
        new_checkpoint=4,
        summarized_turns=2,
        input_tokens=150_000,
    )
    agent = _FakeAgent([_raw_metadata_event(_TURN_USAGE)])
    sm = _RecordingSessionManager(result)

    frames = await _collect_sse(agent, sm)
    compaction = _compaction_frames(frames)

    # update_after_turn consulted exactly once (one terminal `done`).
    assert sm.calls == [150_000]
    assert len(compaction) == 1, (
        f"expected exactly one compaction SSE frame, got {len(compaction)}: "
        f"{compaction}"
    )

    import json

    payload = json.loads(compaction[0][len("event: compaction\ndata: ") :].strip())
    assert payload == {
        "type": "compaction",
        "previousCheckpoint": 0,
        "newCheckpoint": 4,
        "summarizedTurns": 2,
        "inputTokens": 150_000,
    }


@pytest.mark.asyncio
async def test_no_compaction_sse_when_checkpoint_does_not_advance():
    """update_after_turn returns None → zero compaction frames, still one call."""
    agent = _FakeAgent([_raw_metadata_event(_TURN_USAGE)])
    sm = _RecordingSessionManager(None)

    frames = await _collect_sse(agent, sm)

    assert sm.calls == [150_000]
    assert _compaction_frames(frames) == []


@pytest.mark.asyncio
async def test_strands_result_metadata_track_does_not_double_fire():
    """Strands 1.40 non-regression guard.

    Interleave per-call `metadata` events with a Strands ``AgentResult``
    (the ``EventLoopMetrics.accumulated_usage`` / ``metadata_summary``
    surface that 1.40's proactive compression also reads). There is still
    exactly one terminal ``done`` → update_after_turn is consulted exactly
    once → exactly one compaction frame. The accumulated-usage track is not
    a second emit path.
    """
    call_0 = {"inputTokens": 80_000, "outputTokens": 40, "totalTokens": 80_040}
    call_1 = {"inputTokens": 150_000, "outputTokens": 60, "totalTokens": 150_060}
    turn_cumulative = {
        "inputTokens": 230_000,  # Strands sums across calls — must not re-trigger
        "outputTokens": 100,
        "totalTokens": 230_100,
    }
    agent = _FakeAgent(
        [
            _raw_metadata_event(call_0),
            _raw_metadata_event(call_1),
            _raw_agent_result_event(turn_cumulative),
        ]
    )
    result = CompactionResult(
        previous_checkpoint=4,
        new_checkpoint=8,
        summarized_turns=2,
        input_tokens=150_000,
    )
    sm = _RecordingSessionManager(result)

    frames = await _collect_sse(agent, sm)

    # Consulted exactly once. The compaction trigger reads "current context"
    # (last per-call usage via last-write-wins), NOT Strands' summed
    # accumulated_usage — so the input is call_1's 150_000, not 230_000.
    assert sm.calls == [150_000]
    assert len(_compaction_frames(frames)) == 1
