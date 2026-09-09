"""Guards for the app-tool error envelope (AgentCore 424 flattening).

inference-api runs behind AgentCore Runtime, which rewrites any non-2xx to a
generic 424 and discards the message. These tests pin the 200 + envelope
workaround and, most importantly, that a malformed upstream can never make
app-api emit a 401 (which would sign the user out).
"""

from __future__ import annotations

import json

import pytest

from apis.shared.mcp_apps.error_envelope import (
    ENVELOPE_KEY,
    app_tool_error_body,
    app_tool_error_response,
    build_error_envelope,
    read_error_envelope,
)


def test_round_trips_message_and_status() -> None:
    payload = build_error_envelope(
        "Authorization required for 'google_tasks'. Connect the account, "
        "then try again.",
        409,
    )
    assert read_error_envelope(payload) == (
        "Authorization required for 'google_tasks'. Connect the account, "
        "then try again.",
        409,
    )


@pytest.mark.parametrize("code", [400, 403, 404, 409, 422, 502])
def test_relayable_statuses_pass_through(code: int) -> None:
    assert read_error_envelope(build_error_envelope("nope", code)) == (
        "nope",
        code,
    )


def test_ordinary_result_is_not_an_envelope() -> None:
    # A successful dispatch payload must relay unchanged.
    assert read_error_envelope({"toolUseId": "tu-1", "result": {}}) is None


def test_plain_error_body_is_not_an_envelope() -> None:
    # A direct (non-AgentCore) inference-api error keeps its own status,
    # so app-api must not mistake it for an envelope.
    assert read_error_envelope({"error": "boom"}) is None


def test_non_dict_payload_is_not_an_envelope() -> None:
    assert read_error_envelope(["not", "a", "dict"]) is None
    assert read_error_envelope(None) is None


def test_non_dict_envelope_value_is_ignored() -> None:
    assert read_error_envelope({ENVELOPE_KEY: "not-a-dict"}) is None


# --- the safety property: never let upstream choose a 401 -------------------


def test_401_is_never_relayed() -> None:
    """A 401 would trip the SPA's interceptor and sign the user out."""
    message, status = read_error_envelope(build_error_envelope("nope", 401))
    assert status == 502
    assert message == "nope"


@pytest.mark.parametrize("code", [401, 301, 500, 200, -1, 999])
def test_unlisted_status_collapses_to_502(code: int) -> None:
    _, status = read_error_envelope(build_error_envelope("nope", code))
    assert status == 502


@pytest.mark.parametrize("code", [None, "abc", {"a": 1}, [409]])
def test_malformed_status_collapses_to_502(code: object) -> None:
    _, status = read_error_envelope({ENVELOPE_KEY: {"message": "m", "code": code}})
    assert status == 502


def test_numeric_string_status_is_coerced_then_whitelisted() -> None:
    # Coercible, so it is honoured — but still checked against the
    # whitelist, so a coercible 401 is no more relayable than an int one.
    assert read_error_envelope({ENVELOPE_KEY: {"message": "m", "code": "409"}}) == (
        "m",
        409,
    )
    _, status = read_error_envelope({ENVELOPE_KEY: {"message": "m", "code": "401"}})
    assert status == 502


@pytest.mark.parametrize("message", [None, "", "   ", 42])
def test_malformed_message_falls_back(message: object) -> None:
    text, _ = read_error_envelope(
        {ENVELOPE_KEY: {"message": message, "code": 409}}
    )
    assert text == "Tool call failed"


# --- the response helper inference-api actually returns ---------------------


def test_error_response_is_http_200() -> None:
    """The 200 is the fix.

    Returning the real status here is exactly what AgentCore Runtime
    flattens into a 424 with the message discarded — the bug this envelope
    exists to route around. If this assertion ever fails, the consent
    message stops reaching users.
    """
    resp = app_tool_error_response("connect the account", 409)
    assert resp.status_code == 200


def test_error_response_body_carries_code_and_message() -> None:
    resp = app_tool_error_response("connect the account", 409)
    assert json.loads(resp.body) == {
        ENVELOPE_KEY: {"code": 409, "message": "connect the account"}
    }


def test_error_response_round_trips_through_the_reader() -> None:
    resp = app_tool_error_response("connect the account", 409)
    assert read_error_envelope(json.loads(resp.body)) == (
        "connect the account",
        409,
    )


# --- the body the SPA's two consumers actually read ------------------------


def test_error_body_carries_both_keys() -> None:
    """`error` for the App bridge, `detail` for the global toast.

    A string-valued `error` alone matches none of ErrorService's four
    lookups, so the toast fell back to "The request conflicts with the
    current state." while the real message sat unread in the body.
    """
    assert app_tool_error_body("connect the account") == {
        "error": "connect the account",
        "detail": "connect the account",
    }


def test_error_body_detail_is_what_the_toast_reads() -> None:
    # ErrorService priority 1 is a top-level string `detail`.
    body = app_tool_error_body("Authorization required for 'google-tasks'.")
    assert isinstance(body.get("detail"), str)
    assert body["detail"] == "Authorization required for 'google-tasks'."
