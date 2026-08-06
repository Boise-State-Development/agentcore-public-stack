"""Tests for SSE event parsing and the turn fold."""

from __future__ import annotations

import json

import pytest

from agentcore_tui.client.events import (
    ContentBlockStart,
    ContentBlockStop,
    Done,
    ErrorEvent,
    MessageStart,
    MessageStop,
    Metadata,
    ReasoningDelta,
    ReasoningStart,
    ReasoningStop,
    TextDelta,
    TurnAccumulator,
    UnknownEvent,
    Usage,
    parse_event,
)


class TestParseEvent:
    def test_message_start(self) -> None:
        event = parse_event("message_start", json.dumps({"role": "assistant"}))
        assert event == MessageStart(role="assistant")

    def test_message_start_defaults_role_when_absent(self) -> None:
        assert parse_event("message_start", "{}") == MessageStart(role="assistant")

    def test_text_content_block_start(self) -> None:
        event = parse_event("content_block_start", json.dumps({"contentBlockIndex": 2, "type": "text"}))
        assert event == ContentBlockStart(index=2, block_type="text", tool_use=None)

    def test_tool_use_content_block_start_keeps_payload(self) -> None:
        payload = {"contentBlockIndex": 1, "type": "tool_use", "toolUse": {"name": "search", "toolUseId": "abc"}}
        event = parse_event("content_block_start", json.dumps(payload))
        assert isinstance(event, ContentBlockStart)
        assert event.block_type == "tool_use"
        assert event.tool_use == {"name": "search", "toolUseId": "abc"}

    def test_text_delta(self) -> None:
        event = parse_event("content_block_delta", json.dumps({"contentBlockIndex": 0, "type": "text", "text": "hi"}))
        assert event == TextDelta(index=0, text="hi")

    def test_reasoning_lifecycle(self) -> None:
        assert parse_event("reasoning_start", json.dumps({"contentBlockIndex": 0})) == ReasoningStart(index=0)
        assert parse_event("reasoning_delta", json.dumps({"contentBlockIndex": 0, "text": "think"})) == ReasoningDelta(index=0, text="think")
        assert parse_event("reasoning_stop", json.dumps({"contentBlockIndex": 0})) == ReasoningStop(index=0)

    def test_content_block_stop(self) -> None:
        assert parse_event("content_block_stop", json.dumps({"contentBlockIndex": 3})) == ContentBlockStop(index=3)

    def test_message_stop(self) -> None:
        assert parse_event("message_stop", json.dumps({"stopReason": "max_tokens"})) == MessageStop(stop_reason="max_tokens")

    def test_metadata_parses_usage_and_metrics(self) -> None:
        payload = {
            "usage": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 5},
            "metrics": {"latencyMs": 1200},
        }
        event = parse_event("metadata", json.dumps(payload))
        assert isinstance(event, Metadata)
        assert event.usage == Usage(input_tokens=10, output_tokens=20, cache_read_input_tokens=5)
        assert event.usage.total_tokens == 30
        assert event.metrics == {"latencyMs": 1200}

    def test_error_event(self) -> None:
        event = parse_event("error", json.dumps({"error": "Model invocation failed"}))
        assert event == ErrorEvent(message="Model invocation failed")

    def test_error_event_without_message_still_reports(self) -> None:
        event = parse_event("error", "{}")
        assert isinstance(event, ErrorEvent)
        assert event.message

    def test_done(self) -> None:
        assert parse_event("done", "{}") == Done()

    def test_unknown_event_is_preserved_not_raised(self) -> None:
        """A newer server must not break an older client."""
        event = parse_event("tool_result", json.dumps({"anything": 1}))
        assert event == UnknownEvent(name="tool_result", payload={"anything": 1})

    def test_malformed_json_becomes_error_event(self) -> None:
        event = parse_event("content_block_delta", "{not json")
        assert isinstance(event, ErrorEvent)
        assert "malformed" in event.message.lower()

    def test_empty_data_is_tolerated(self) -> None:
        assert parse_event("done", "") == Done()

    def test_non_object_json_is_tolerated(self) -> None:
        event = parse_event("message_stop", json.dumps(["unexpected"]))
        assert event == MessageStop(stop_reason="end_turn")

    @pytest.mark.parametrize("bad_index", ["x", None, True, 1.5])
    def test_non_integer_index_falls_back_to_zero(self, bad_index: object) -> None:
        event = parse_event("content_block_delta", json.dumps({"contentBlockIndex": bad_index, "text": "a"}))
        assert isinstance(event, TextDelta)
        assert event.index == 0


class TestUsage:
    def test_missing_fields_default_to_zero(self) -> None:
        assert Usage.from_payload({}) == Usage(input_tokens=0, output_tokens=0)

    def test_booleans_are_not_treated_as_ints(self) -> None:
        """`isinstance(True, int)` is True in Python; the parser must reject it."""
        usage = Usage.from_payload({"inputTokens": True, "cacheReadInputTokens": False})
        assert usage.input_tokens == 0
        assert usage.cache_read_input_tokens is None


class TestTurnAccumulator:
    def test_concatenates_text_deltas_in_order(self) -> None:
        accumulator = TurnAccumulator()
        for chunk in ("Hello", ", ", "world"):
            accumulator.apply(TextDelta(index=0, text=chunk))
        assert accumulator.text == "Hello, world"

    def test_separates_reasoning_from_answer(self) -> None:
        accumulator = TurnAccumulator()
        accumulator.apply(ReasoningDelta(index=0, text="pondering"))
        accumulator.apply(TextDelta(index=1, text="answer"))
        assert accumulator.reasoning == "pondering"
        assert accumulator.text == "answer"

    def test_records_usage_stop_reason_and_completion(self) -> None:
        accumulator = TurnAccumulator()
        accumulator.apply(TextDelta(index=0, text="hi"))
        accumulator.apply(MessageStop(stop_reason="end_turn"))
        accumulator.apply(Metadata(usage=Usage(input_tokens=1, output_tokens=2)))
        accumulator.apply(Done())
        assert accumulator.stop_reason == "end_turn"
        assert accumulator.usage == Usage(input_tokens=1, output_tokens=2)
        assert accumulator.finished
        assert accumulator.ok

    def test_truncated_detects_token_ceiling(self) -> None:
        accumulator = TurnAccumulator()
        accumulator.apply(TextDelta(index=0, text="partial"))
        accumulator.apply(MessageStop(stop_reason="max_tokens"))
        assert accumulator.truncated

    def test_error_event_marks_turn_not_ok(self) -> None:
        accumulator = TurnAccumulator()
        accumulator.apply(TextDelta(index=0, text="partial"))
        accumulator.apply(ErrorEvent(message="upstream exploded"))
        assert accumulator.error == "upstream exploded"
        assert not accumulator.ok

    def test_empty_response_is_not_ok(self) -> None:
        accumulator = TurnAccumulator()
        accumulator.apply(MessageStop(stop_reason="end_turn"))
        accumulator.apply(Done())
        assert not accumulator.ok

    def test_unknown_events_are_ignored(self) -> None:
        accumulator = TurnAccumulator()
        accumulator.apply(UnknownEvent(name="future", payload={}))
        accumulator.apply(TextDelta(index=0, text="ok"))
        assert accumulator.text == "ok"
