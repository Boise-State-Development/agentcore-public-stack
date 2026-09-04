"""Retry visibility and service-unavailable copy.

Both close gaps found in prod session `5f34d2b0` (2026-08-31): a retried model
call was indistinguishable from a hang, and a Bedrock 503 fell through to the
generic "I ran into a problem with the AI model" text that gave the user no
signal the failure was temporary.
"""

import pytest

from agents.main_agent.streaming.stream_processor import (
    _format_force_stop_message,
    _handle_retry_events,
    process_agent_stream,
)
from apis.shared.errors import (
    ErrorCode,
    build_conversational_error_event,
    is_service_unavailable_error,
)


class TestHandleRetryEvents:
    def test_emits_model_retry_with_attempt_and_delay(self):
        state = {"attempt": 0}
        events = _handle_retry_events({"event_loop_throttled_delay": 4}, state)
        assert events == [
            {"type": "model_retry", "data": {"type": "model_retry", "attempt": 1, "delaySeconds": 4}}
        ]

    def test_attempt_increments_within_a_turn(self):
        state = {"attempt": 0}
        _handle_retry_events({"event_loop_throttled_delay": 2}, state)
        second = _handle_retry_events({"event_loop_throttled_delay": 4}, state)
        assert second[0]["data"]["attempt"] == 2

    def test_ignores_unrelated_events(self):
        assert _handle_retry_events({"start_event_loop": True}, {"attempt": 0}) == []

    def test_unparseable_delay_still_reports_the_retry(self):
        """Losing the delay must not lose the signal that a retry happened."""
        events = _handle_retry_events({"event_loop_throttled_delay": None}, {"attempt": 0})
        assert events[0]["data"]["delaySeconds"] == 0

    def test_payload_carries_type_for_the_spa_validator(self):
        """Every SPA validator discriminates on a `type` inside the payload,
        not on the SSE `event:` line."""
        events = _handle_retry_events({"event_loop_throttled_delay": 1}, {"attempt": 0})
        assert events[0]["data"]["type"] == "model_retry"


class TestRetryEventReachesTheStream:
    @pytest.mark.asyncio
    async def test_model_retry_is_yielded_before_content(self):
        async def stream():
            yield {"event_loop_throttled_delay": 2}
            yield {"event": {"contentBlockDelta": {"delta": {"text": "hi"}}}}

        types = [e["type"] async for e in process_agent_stream(stream())]
        assert "model_retry" in types
        assert types.index("model_retry") == 0

    @pytest.mark.asyncio
    async def test_counter_resets_between_turns(self):
        """retry_state is per-invocation; the agent instance is cached (#741/#751)."""
        async def stream():
            yield {"event_loop_throttled_delay": 2}

        for _ in range(2):
            events = [e async for e in process_agent_stream(stream()) if e["type"] == "model_retry"]
            assert events[0]["data"]["attempt"] == 1


class TestIsServiceUnavailableError:
    @pytest.mark.parametrize(
        "text",
        [
            "an error occurred (serviceunavailableexception) when calling converse",
            "service unavailable",
            "internalserverexception: something broke",
            "modelnotreadyexception",
            "http 503 from bedrock",
        ],
    )
    def test_recognizes_transient_provider_faults(self, text):
        assert is_service_unavailable_error(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "validationexception: bad input",
            "accessdeniedexception",
            "the model or feature you're trying to use isn't available",
            "throttlingexception",
        ],
    )
    def test_does_not_swallow_unrelated_errors(self, text):
        """'unavailable' as a bare word appears in unrelated copy — the
        predicate must stay narrow enough not to claim those."""
        assert is_service_unavailable_error(text) is False


class TestServiceUnavailableCopy:
    def test_force_stop_classifier_explains_the_outage(self):
        message, recoverable = _format_force_stop_message(
            "An error occurred (ServiceUnavailableException) when calling ConverseStream"
        )
        assert "temporarily unavailable" in message
        assert "already retried" in message
        assert recoverable is True

    def test_force_stop_no_longer_falls_through_to_the_generic_text(self):
        message, _ = _format_force_stop_message("ServiceUnavailableException")
        assert "Agent force-stopped" not in message

    @pytest.mark.parametrize(
        "code", [ErrorCode.MODEL_ERROR, ErrorCode.STREAM_ERROR, ErrorCode.AGENT_ERROR]
    )
    def test_conversational_event_is_specific_under_every_code(self, code):
        """A 503 surfaces under different codes depending on where it is
        caught, so the override lives outside the per-code branches."""
        event = build_conversational_error_event(
            code=code,
            error=Exception("ServiceUnavailableException: Bedrock is busy"),
        )
        assert "temporarily unavailable" in event.message
        assert "I ran into a problem with the AI model" not in event.message
        assert event.recoverable is True

    def test_throttling_keeps_its_own_wording(self):
        message, _ = _format_force_stop_message("ThrottlingException: too many requests")
        assert "too many requests" in message
        assert "temporarily unavailable" not in message
