"""Wire-contract tests for the browser-takeover interrupt.

What these protect is the two rules that are easy to state and easy to regress:
no URL may travel on this path, and a resume payload must never be able to
raise. Everything else about the flow is exercised in the tool and streaming
tests.
"""

from __future__ import annotations

import json

import pytest

from apis.shared.browser_takeover import (
    BrowserLoginRequiredEvent,
    BrowserSessionRef,
    assert_no_url,
    decode_ref,
    encode_ref,
    format_outcome,
    parse_outcome,
)


def _ref(**overrides) -> BrowserSessionRef:
    payload = {
        "browserSessionId": "bs-1",
        "browserId": "browser-abc",
        "viewport": {"width": 1280, "height": 800},
        "controlState": "user",
        "deadlineAt": "2026-09-18T12:00:00+00:00",
    }
    payload.update(overrides)
    return BrowserSessionRef.model_validate(payload)


def _event(**overrides) -> BrowserLoginRequiredEvent:
    payload = {
        "interruptId": "v1:tool_call:tu-1:abc",
        "toolUseId": "tu-1",
        "sessionId": "conv-1",
        "browserSessionId": "bs-1",
        "browserId": "browser-abc",
        "viewport": {"width": 1280, "height": 800},
    }
    payload.update(overrides)
    return BrowserLoginRequiredEvent.model_validate(payload)


# ---------------------------------------------------------------------------
# No URL, anywhere (PR #1101's rule, enforced rather than remembered)
# ---------------------------------------------------------------------------


class TestNoUrlReachesTheClient:
    def test_the_event_carries_identifiers_not_a_live_view_url(self) -> None:
        payload = _event().model_dump(by_alias=True, exclude_none=True)

        assert payload["browserSessionId"] == "bs-1"
        assert_no_url(payload)  # must not raise

    def test_a_presigned_url_smuggled_onto_the_payload_is_caught(self) -> None:
        payload = _event().model_dump(by_alias=True, exclude_none=True)
        payload["liveViewUrl"] = (
            "https://bedrock-agentcore.us-west-2.amazonaws.com/live?X-Amz-Signature=deadbeef"
        )

        with pytest.raises(ValueError, match="minted by app-api"):
            assert_no_url(payload)

    def test_a_url_nested_deeper_is_still_caught(self) -> None:
        with pytest.raises(ValueError):
            assert_no_url({"a": [{"b": "wss://example.com/stream"}]})

    def test_target_url_is_the_one_allowed_url(self) -> None:
        # The page the agent already browsed, shown so the user knows what they
        # are signing into. It is an ordinary navigational URL, not a signed
        # one, and the model put it there in the first place.
        assert_no_url(
            _event(targetUrl="https://www.jstor.org/action/showLogin").model_dump(
                by_alias=True, exclude_none=True
            )
        )

    def test_the_sandbox_origin_is_allowed_but_a_signed_url_is_not(self) -> None:
        # The origin is a deployment constant with no credential in it, and the
        # SPA needs it to know where to frame the viewer from. The guard exists
        # for presigned URLs, which are credentials in URL form — so widening
        # it for the origin must not widen it for those.
        payload = _event(sandboxOrigin="https://mcp-sandbox.example.edu").model_dump(
            by_alias=True, exclude_none=True
        )
        assert_no_url(payload)

        payload["someOtherField"] = "https://example.com/x?X-Amz-Signature=abc"
        with pytest.raises(ValueError):
            assert_no_url(payload)

    def test_an_absent_sandbox_origin_reads_as_no_viewer(self) -> None:
        # Not an error: an environment without the sandbox origin deployed
        # shows the prompt without a viewer rather than framing nothing.
        assert _event().sandbox_origin == ""

    def test_sse_frame_names_the_event_and_carries_json(self) -> None:
        frame = _event().to_sse_format()

        assert frame.startswith("event: browser_login_required\n")
        assert frame.endswith("\n\n")
        data = json.loads(frame.split("data: ", 1)[1])
        assert data["type"] == "browser_login_required"
        assert data["sessionId"] == "conv-1"

    def test_session_id_is_the_conversation_and_browser_is_separate(self) -> None:
        # The two are deliberately distinct fields: app-api scopes the
        # live-view route by conversation owner, and resolves the browser
        # session server-side. A single ambiguous `sessionId` would collide.
        data = json.loads(_event().to_sse_format().split("data: ", 1)[1])

        assert data["sessionId"] != data["browserSessionId"]


# ---------------------------------------------------------------------------
# Resume payloads
# ---------------------------------------------------------------------------


class TestParseOutcome:
    @pytest.mark.parametrize(
        "response,expected",
        [
            ({"completed": True}, "completed"),
            ({"skipped": True}, "skipped"),
            ({"expired": True}, "expired"),
            ({"outcome": "completed"}, "completed"),
            ({"outcome": "failed"}, "failed"),
            ("completed", "completed"),
            ("expired", "expired"),
        ],
    )
    def test_recognized_shapes(self, response, expected) -> None:
        assert parse_outcome(response)[0] == expected

    @pytest.mark.parametrize(
        "response",
        [None, 42, [], {}, {"nonsense": True}, "gibberish", {"outcome": "weird"}],
    )
    def test_anything_unrecognized_reads_as_skipped_and_never_raises(
        self, response
    ) -> None:
        # The user's only route out of a paused turn must not be able to fail
        # on a shape mismatch — same reasoning as `parse_answers`.
        assert parse_outcome(response)[0] == "skipped"

    def test_a_note_rides_along_and_is_bounded(self) -> None:
        outcome, note = parse_outcome({"completed": True, "note": "x" * 5000})

        assert outcome == "completed"
        assert note is not None and len(note) == 500

    def test_blank_note_is_dropped(self) -> None:
        assert parse_outcome({"completed": True, "note": "   "})[1] is None


class TestFormatOutcome:
    @pytest.mark.parametrize(
        "outcome", ["completed", "skipped", "expired", "failed"]
    )
    def test_every_outcome_renders_one_short_line(self, outcome) -> None:
        text = format_outcome(outcome)

        assert text and "\n" not in text
        # This text re-enters the conversation on every subsequent turn, so it
        # stays small by contract, not by luck.
        assert len(text) < 300

    def test_completed_tells_the_model_the_session_is_authenticated(self) -> None:
        assert "authenticated" in format_outcome("completed")

    def test_declined_tells_the_model_not_to_ask_again(self) -> None:
        assert "not ask again" in format_outcome("skipped")

    def test_a_note_is_appended(self) -> None:
        assert "(User note: no account)" in format_outcome("skipped", "no account")

    def test_no_outcome_line_contains_a_url(self) -> None:
        for outcome in ("completed", "skipped", "expired", "failed"):
            assert_no_url(format_outcome(outcome))


# ---------------------------------------------------------------------------
# Persistence round trip
# ---------------------------------------------------------------------------


class TestEncoding:
    def test_ref_round_trips_through_the_dynamo_string(self) -> None:
        decoded = decode_ref(encode_ref(_ref()))

        assert decoded is not None
        assert decoded["browserSessionId"] == "bs-1"
        assert decoded["viewport"] == {"width": 1280, "height": 800}

    @pytest.mark.parametrize("encoded", [None, "", "not json", "[1,2,3]"])
    def test_unreadable_rows_decode_to_none_rather_than_raising(self, encoded) -> None:
        assert decode_ref(encoded) is None

    def test_viewport_survives_so_the_dcv_stream_does_not_crop(self) -> None:
        # remoteWidth/remoteHeight must match the session viewport exactly;
        # carrying it rather than re-declaring 1280x800 in the SPA is what
        # stops the two drifting.
        ref = _ref(viewport={"width": 1920, "height": 1080})

        assert decode_ref(encode_ref(ref))["viewport"]["width"] == 1920
