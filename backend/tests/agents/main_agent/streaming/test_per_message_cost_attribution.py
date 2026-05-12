"""Regression test for per-message cost attribution on multi-LLM-call turns.

Strands emits two sources of usage during a tool-use turn:
  1. Per-LLM-call metadata via ``ModelStreamChunkEvent`` (one per assistant
     message), carrying just that call's tokens.
  2. A final ``AgentResultEvent`` whose ``AgentResult.metrics`` is an
     ``EventLoopMetrics`` with ``accumulated_usage`` summed across every call
     in the turn.

``stream_processor._handle_metadata_events`` extracts both. The stream
coordinator routes any ``metadata`` event into
``per_message_metadata[current_assistant_message_index]["usage"].update(...)``.
Because the AgentResult event arrives *after* every ``message_stop`` (so the
index still points at the last assistant message), a naive ``.update()`` on
the same key overwrites the last message's per-call usage with the
turn-cumulative usage. Pricing each per-message entry and summing then
double-counts every earlier message's input tokens.

This module locks the contract:
  - The per-call metadata events stay typed ``metadata`` (per-message track).
  - The result-extracted cumulative metadata is typed ``metadata_summary``
    (turn-summary track), so it never lands in per_message_metadata.

If the contract regresses, simulating the dispatch loop will reproduce the
double-count and these assertions will fail.
"""

from typing import Any, Dict, List

from agents.main_agent.streaming.stream_processor import _handle_metadata_events


# Realistic per-call metadata chunk shape: Bedrock's `metadata` chunk wrapped
# inside Strands' ModelStreamChunkEvent (`{"event": chunk}`).
def _per_call_metadata_event(usage: Dict[str, int]) -> Dict[str, Any]:
    return {"event": {"metadata": {"usage": usage, "metrics": {"latencyMs": 100}}}}


# Realistic AgentResultEvent shape. EventLoopMetrics has accumulated_usage
# summed across all calls; _handle_metadata_events extracts it via __dict__.
class _FakeEventLoopMetrics:
    def __init__(self, accumulated_usage: Dict[str, int]) -> None:
        self.accumulated_usage = accumulated_usage
        self.accumulated_metrics = {"latencyMs": 250}


class _FakeAgentResult:
    def __init__(self, accumulated_usage: Dict[str, int]) -> None:
        self.metrics = _FakeEventLoopMetrics(accumulated_usage)


def _agent_result_event(accumulated_usage: Dict[str, int]) -> Dict[str, Any]:
    return {"result": _FakeAgentResult(accumulated_usage)}


def _dispatch_to_per_message(
    processed_events: List[Dict[str, Any]],
    per_message_metadata: List[Dict[str, Any]],
    current_index: int,
) -> None:
    """Mimic stream_coordinator's per-message routing for a single source event.

    Only ``metadata`` events flow into ``per_message_metadata`` — the
    ``metadata_summary`` track is for the turn-level accumulator and is
    intentionally not routed here.
    """
    for processed in processed_events:
        if processed.get("type") != "metadata":
            continue
        usage = processed.get("data", {}).get("usage")
        if not usage:
            continue
        per_message_metadata[current_index]["usage"].update(usage)


class TestPerMessageAttributionTwoCallTurn:
    """Reproduce the dispatch sequence of a 2-call tool-use turn."""

    CALL_0_USAGE = {"inputTokens": 1000, "outputTokens": 50, "totalTokens": 1050}
    CALL_1_USAGE = {"inputTokens": 1300, "outputTokens": 80, "totalTokens": 1380}
    TURN_CUMULATIVE = {
        "inputTokens": CALL_0_USAGE["inputTokens"] + CALL_1_USAGE["inputTokens"],
        "outputTokens": CALL_0_USAGE["outputTokens"] + CALL_1_USAGE["outputTokens"],
        "totalTokens": CALL_0_USAGE["totalTokens"] + CALL_1_USAGE["totalTokens"],
    }

    def test_per_call_metadata_routes_to_per_message_track(self):
        """Each per-call metadata event carries one message's tokens, no more."""
        events = _handle_metadata_events(_per_call_metadata_event(self.CALL_0_USAGE))
        metadata_events = [e for e in events if e["type"] == "metadata"]
        assert len(metadata_events) == 1
        assert metadata_events[0]["data"]["usage"] == self.CALL_0_USAGE

    def test_result_cumulative_does_not_route_to_per_message_track(self):
        """The AgentResult cumulative must not be a `metadata` event.

        If it is, the dispatch loop overwrites the last per-message entry
        with cumulative usage, double-counting earlier messages' input
        tokens at pricing time.
        """
        events = _handle_metadata_events(_agent_result_event(self.TURN_CUMULATIVE))
        per_message_typed = [e for e in events if e["type"] == "metadata"]
        assert per_message_typed == [], (
            "AgentResult cumulative usage was emitted as a `metadata` event; "
            "it would clobber the last per-message entry. Expected "
            "`metadata_summary` so it stays on the turn-summary track only."
        )

    def test_result_cumulative_emitted_on_summary_track(self):
        """Result-extracted cumulative is still emitted — just on metadata_summary."""
        events = _handle_metadata_events(_agent_result_event(self.TURN_CUMULATIVE))
        summary_events = [e for e in events if e["type"] == "metadata_summary"]
        assert len(summary_events) == 1
        assert summary_events[0]["data"]["usage"] == self.TURN_CUMULATIVE

    def test_full_turn_dispatch_preserves_per_call_attribution(self):
        """Drive the full event sequence and assert no double-counting."""
        per_message_metadata = [
            {"usage": {}, "metrics": {}},
            {"usage": {}, "metrics": {}},
        ]

        # Message 0's per-call metadata fires while index = 0.
        _dispatch_to_per_message(
            _handle_metadata_events(_per_call_metadata_event(self.CALL_0_USAGE)),
            per_message_metadata,
            current_index=0,
        )
        # Message 1's per-call metadata fires while index = 1.
        _dispatch_to_per_message(
            _handle_metadata_events(_per_call_metadata_event(self.CALL_1_USAGE)),
            per_message_metadata,
            current_index=1,
        )
        # AgentResult cumulative fires last, with index still at 1. If this
        # leaks onto the `metadata` track, msg 1's usage gets clobbered with
        # the turn cumulative — input tokens for msg 0 would be summed twice
        # when pricing each entry independently.
        _dispatch_to_per_message(
            _handle_metadata_events(_agent_result_event(self.TURN_CUMULATIVE)),
            per_message_metadata,
            current_index=1,
        )

        assert per_message_metadata[0]["usage"] == self.CALL_0_USAGE
        assert per_message_metadata[1]["usage"] == self.CALL_1_USAGE

        # Pricing each entry independently must equal the cumulative input,
        # not 2× msg 0's input + msg 1's input.
        summed_input = (
            per_message_metadata[0]["usage"]["inputTokens"]
            + per_message_metadata[1]["usage"]["inputTokens"]
        )
        assert summed_input == self.TURN_CUMULATIVE["inputTokens"]


class TestSummaryAccumulatorAcceptsBothTracks:
    """The stream_processor main loop must keep `accumulated_metadata` cumulative.

    Per-call events accumulate via ``.update()`` (last-write-wins), so before
    the cumulative arrives the accumulator only holds the last call's usage —
    which is *not* cumulative. The accumulator must therefore consume both
    `metadata` and `metadata_summary` events for the final summary emission
    to carry true turn totals.
    """

    def test_accumulator_processes_both_tracks(self):
        """Walk the same sequence the main loop does and check the final state."""
        accumulated: Dict[str, Any] = {"usage": {}, "metrics": {}}

        sequence = [
            _per_call_metadata_event(TestPerMessageAttributionTwoCallTurn.CALL_0_USAGE),
            _per_call_metadata_event(TestPerMessageAttributionTwoCallTurn.CALL_1_USAGE),
            _agent_result_event(TestPerMessageAttributionTwoCallTurn.TURN_CUMULATIVE),
        ]

        for raw in sequence:
            for processed in _handle_metadata_events(raw):
                if processed.get("type") in ("metadata", "metadata_summary"):
                    data = processed.get("data", {})
                    if "usage" in data:
                        accumulated["usage"].update(data["usage"])
                    if "metrics" in data:
                        accumulated["metrics"].update(data["metrics"])

        assert accumulated["usage"] == TestPerMessageAttributionTwoCallTurn.TURN_CUMULATIVE


class TestStreamCoordinatorContextOccupancy:
    """The final SSE `usage` field must reflect current context, not sums.

    Bedrock reports each LLM call's `inputTokens` as the FULL context size
    sent on that call. For a 2-call tool turn:
        call_1.input  = 1000  (system + user_msg)
        call_2.input  = 2500  (system + user_msg + tool_use + tool_result)

    Strands' EventLoopMetrics.accumulated_usage sums these into 3500 — but
    the actual context occupancy is 2500, the size of the most recent call.
    The frontend uses the SSE metadata `usage` to drive the context-%
    badge, and the backend uses it to decide whether to trigger
    compaction; both need "current context size", not the cross-call sum.

    This locks in the contract that stream_coordinator's accumulated_metadata
    (which feeds the final SSE metadata) takes per-call values via
    last-write-wins from `metadata` events and IGNORES the cross-call
    cumulative carried on `metadata_summary`.
    """

    CALL_0_USAGE = {"inputTokens": 1000, "outputTokens": 50, "totalTokens": 1050}
    CALL_1_USAGE = {"inputTokens": 2500, "outputTokens": 100, "totalTokens": 2600}
    TURN_CUMULATIVE = {
        "inputTokens": 3500,   # 1000 + 2500 — Strands' accumulated_usage
        "outputTokens": 150,
        "totalTokens": 3650,
    }

    def _simulate_stream_coordinator_accumulator(
        self, events: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Mirror stream_coordinator's accumulator branches for a sequence of
        already-processed events. Returns the resulting accumulated_metadata.

        - `metadata` events → update accumulated_metadata.usage/metrics.
        - `metadata_summary` events → first_token_time only; usage/metrics ignored.
        """
        accumulated: Dict[str, Any] = {"usage": {}, "metrics": {}}
        for processed in events:
            event_type = processed.get("type")
            event_data = processed.get("data", {})
            if event_type == "metadata":
                if "usage" in event_data:
                    accumulated["usage"].update(event_data["usage"])
                if "metrics" in event_data:
                    accumulated["metrics"].update(event_data["metrics"])
            # metadata_summary intentionally does NOT touch usage/metrics here
        return accumulated

    def test_final_usage_reflects_last_call_not_sum(self):
        """End of a 2-call tool turn — usage should be call_2's, not the sum."""
        # Drive the realistic event order through _handle_metadata_events
        # exactly as stream_processor would, then through the coordinator's
        # accumulator branches.
        raw_events = [
            _per_call_metadata_event(self.CALL_0_USAGE),
            _per_call_metadata_event(self.CALL_1_USAGE),
            _agent_result_event(self.TURN_CUMULATIVE),
        ]
        processed: List[Dict[str, Any]] = []
        for raw in raw_events:
            processed.extend(_handle_metadata_events(raw))

        result = self._simulate_stream_coordinator_accumulator(processed)

        assert result["usage"] == self.CALL_1_USAGE, (
            "Final accumulated usage must equal the last per-call's full input "
            "(current context size), not Strands' summed-across-calls value. "
            "If this regresses, the context-% badge and compaction trigger "
            "will inflate by ~the size of every prior call in the turn."
        )

    def test_compaction_input_tokens_match_current_context(self):
        """The trigger threshold computation in stream_coordinator uses
        `usage.inputTokens + cacheReadInputTokens + cacheWriteInputTokens`."""
        call_with_cache = {
            "inputTokens": 200,
            "outputTokens": 80,
            "totalTokens": 280,
            "cacheReadInputTokens": 2000,
            "cacheWriteInputTokens": 300,
        }
        prior_call = {
            "inputTokens": 100,
            "outputTokens": 40,
            "totalTokens": 140,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 800,
        }
        cumulative_after_two_calls = {
            "inputTokens": 300,            # would be summed by Strands
            "outputTokens": 120,
            "totalTokens": 420,
            "cacheReadInputTokens": 2000,
            "cacheWriteInputTokens": 1100, # would be summed by Strands
        }

        raw_events = [
            _per_call_metadata_event(prior_call),
            _per_call_metadata_event(call_with_cache),
            _agent_result_event(cumulative_after_two_calls),
        ]
        processed: List[Dict[str, Any]] = []
        for raw in raw_events:
            processed.extend(_handle_metadata_events(raw))

        result = self._simulate_stream_coordinator_accumulator(processed)
        usage = result["usage"]

        # Compaction sums all three input buckets — must equal call_with_cache's
        # totals (current context), not the summed-across-calls totals.
        compaction_input = (
            usage.get("inputTokens", 0)
            + usage.get("cacheReadInputTokens", 0)
            + usage.get("cacheWriteInputTokens", 0)
        )
        expected_current_context = (
            call_with_cache["inputTokens"]
            + call_with_cache["cacheReadInputTokens"]
            + call_with_cache["cacheWriteInputTokens"]
        )
        assert compaction_input == expected_current_context
