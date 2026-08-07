"""Tests for the CLI device-authorization flow.

No sockets: every request is served by ``httpx.MockTransport``. No real waiting
either — ``sleep`` and ``clock`` are injected, because the polling loop is almost
entirely timing behaviour and asserting on it must not take ten minutes.

The properties worth protecting here are the ones where a plausible alternative
implementation is wrong:

* an unknown error code must be terminal, not "keep waiting";
* ``slow_down`` must widen the interval *permanently*, or the server's
  throttle-from-the-previous-poll rule means the client never escapes it;
* the loop must not sleep past the grant deadline;
* the sealed session must never appear in a repr or a log line.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from agentcore_tui.client.device_auth import (
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    SLOW_DOWN_INCREMENT_SECONDS,
    DeviceAuthClient,
    DeviceAuthorization,
    DeviceSession,
)
from agentcore_tui.errors import (
    ConnectionFailedError,
    DeviceAuthDeniedError,
    DeviceAuthError,
    DeviceAuthExpiredError,
    DeviceAuthRejectedError,
    RateLimitedError,
)

BASE_URL = "https://example.invalid/api"

AUTHORIZE_BODY = {
    "device_code": "d" * 43,
    "user_code": "Y4GN-WKY3",
    "verification_uri": f"{BASE_URL}/auth/cli/verify",
    "verification_uri_complete": f"{BASE_URL}/auth/cli/verify?user_code=Y4GN-WKY3",
    "expires_in": 600,
    "interval": 5,
}

TOKEN_BODY = {
    "session": "sealed-envelope-value",
    "expires_in": 28783,
    "user_id": "28d1d380-e051-708d-c10e-b460df161c04",
    "username": "colin",
}


def pending(error: str = "authorization_pending") -> httpx.Response:
    return httpx.Response(400, json={"error": error, "error_description": f"because {error}"})


def granted() -> httpx.Response:
    return httpx.Response(200, json=TOKEN_BODY)


class Clock:
    """A monotonic clock the test advances, so deadlines are exact."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_client(
    responses: list[httpx.Response],
    *,
    clock: Clock | None = None,
    capture: list[httpx.Request] | None = None,
) -> tuple[DeviceAuthClient, Clock]:
    """A client whose poll responses are served in order, last one repeating."""
    the_clock = clock or Clock()
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if request.url.path.endswith("/authorize"):
            return httpx.Response(200, json=AUTHORIZE_BODY)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = DeviceAuthClient(
        BASE_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=the_clock.sleep,
        clock=the_clock,
    )
    return client, the_clock


def authorization(**overrides: Any) -> DeviceAuthorization:
    fields: dict[str, Any] = {
        "device_code": "d" * 43,
        "user_code": "Y4GN-WKY3",
        "verification_uri": "https://example.invalid/api/auth/cli/verify",
        "verification_uri_complete": "https://example.invalid/api/auth/cli/verify?user_code=Y4GN-WKY3",
        "expires_in": 600,
        "interval": 5,
    }
    fields.update(overrides)
    return DeviceAuthorization(**fields)


# =====================================================================
# authorize()
# =====================================================================


class TestAuthorize:
    @pytest.mark.asyncio
    async def test_parses_the_full_rfc_8628_payload(self) -> None:
        client, _ = make_client([pending()])
        async with client:
            result = await client.authorize()
        assert result.device_code == "d" * 43
        assert result.user_code == "Y4GN-WKY3"
        assert result.verification_uri_complete.endswith("user_code=Y4GN-WKY3")
        assert result.expires_in == 600
        assert result.interval == 5

    @pytest.mark.asyncio
    async def test_posts_to_the_authorize_endpoint_with_no_credential(self) -> None:
        """Pre-authentication by necessity: sending a credential would be a bug."""
        captured: list[httpx.Request] = []
        client, _ = make_client([pending()], capture=captured)
        async with client:
            await client.authorize()
        request = captured[0]
        assert request.method == "POST"
        assert str(request.url) == f"{BASE_URL}/auth/cli/authorize"
        assert "authorization" not in {k.lower() for k in request.headers}
        assert "x-api-key" not in {k.lower() for k in request.headers}

    @pytest.mark.asyncio
    async def test_falls_back_to_the_plain_uri_when_complete_is_absent(self) -> None:
        """The user can still type the code, so this degrades rather than fails."""
        body = {k: v for k, v in AUTHORIZE_BODY.items() if k != "verification_uri_complete"}
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        async with DeviceAuthClient(BASE_URL, client=httpx.AsyncClient(transport=transport)) as client:
            result = await client.authorize()
        assert result.verification_uri_complete == AUTHORIZE_BODY["verification_uri"]

    @pytest.mark.asyncio
    async def test_a_missing_required_field_names_it(self) -> None:
        """Usually means the base URL is not an app-api. Say which field."""
        body = {k: v for k, v in AUTHORIZE_BODY.items() if k != "user_code"}
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        async with DeviceAuthClient(BASE_URL, client=httpx.AsyncClient(transport=transport)) as client:
            with pytest.raises(DeviceAuthError, match="user_code"):
                await client.authorize()

    @pytest.mark.asyncio
    async def test_nonsense_interval_falls_back_instead_of_busy_looping(self) -> None:
        """A zero interval would spin. Refuse the server's number, don't obey it."""
        body = {**AUTHORIZE_BODY, "interval": 0, "expires_in": -1}
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
        async with DeviceAuthClient(BASE_URL, client=httpx.AsyncClient(transport=transport)) as client:
            result = await client.authorize()
        assert result.interval == 5
        assert result.expires_in == 600

    @pytest.mark.asyncio
    async def test_html_error_page_does_not_raise_a_json_error(self) -> None:
        """A proxy in front of app-api returns HTML. That must read as a status."""
        transport = httpx.MockTransport(lambda _r: httpx.Response(502, text="<html>bad gateway</html>"))
        async with DeviceAuthClient(BASE_URL, client=httpx.AsyncClient(transport=transport)) as client:
            with pytest.raises(DeviceAuthError, match="HTTP 502"):
                await client.authorize()

    @pytest.mark.asyncio
    async def test_429_is_a_rate_limit_not_a_flow_failure(self) -> None:
        transport = httpx.MockTransport(lambda _r: httpx.Response(429, headers={"retry-after": "30"}))
        async with DeviceAuthClient(BASE_URL, client=httpx.AsyncClient(transport=transport)) as client:
            with pytest.raises(RateLimitedError) as caught:
                await client.authorize()
        assert caught.value.retry_after == 30

    @pytest.mark.asyncio
    async def test_unreachable_host_reports_the_base_url(self) -> None:
        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        transport = httpx.MockTransport(explode)
        async with DeviceAuthClient(BASE_URL, client=httpx.AsyncClient(transport=transport)) as client:
            with pytest.raises(ConnectionFailedError, match=BASE_URL):
                await client.authorize()


# =====================================================================
# poll_for_session()
# =====================================================================


class TestPollingToSuccess:
    @pytest.mark.asyncio
    async def test_returns_the_session_on_the_first_poll(self) -> None:
        client, clock = make_client([granted()])
        async with client:
            session = await client.poll_for_session(authorization())
        assert session == DeviceSession(**TOKEN_BODY)
        # Approved before the first poll: the flow must not have waited at all.
        assert clock.slept == []

    @pytest.mark.asyncio
    async def test_polls_immediately_then_waits_the_interval(self) -> None:
        """The first poll is undelayed on purpose — see poll_for_session."""
        client, clock = make_client([pending(), pending(), granted()])
        async with client:
            await client.poll_for_session(authorization(interval=5))
        assert clock.slept == [5.0, 5.0]

    @pytest.mark.asyncio
    async def test_sends_the_device_code_and_nothing_else(self) -> None:
        captured: list[httpx.Request] = []
        client, _ = make_client([granted()], capture=captured)
        async with client:
            await client.poll_for_session(authorization())
        poll = [r for r in captured if r.url.path.endswith("/token")][0]
        assert json.loads(poll.content) == {"device_code": "d" * 43}

    @pytest.mark.asyncio
    async def test_progress_callback_reports_wait_and_remaining(self) -> None:
        seen: list[tuple[int, int]] = []
        client, _ = make_client([pending(), granted()])
        async with client:
            await client.poll_for_session(
                authorization(interval=5, expires_in=600),
                on_progress=lambda wait, remaining: seen.append((wait, remaining)),
            )
        assert seen == [(5, 600)]


class TestSlowDown:
    @pytest.mark.asyncio
    async def test_widens_the_interval_permanently(self) -> None:
        """Not just for one wait.

        The server throttles from the *previous* poll's timestamp, so a client
        that reverts to the original interval can be told to slow down forever
        and burn its whole window.
        """
        client, clock = make_client([pending("slow_down"), pending(), pending(), granted()])
        async with client:
            await client.poll_for_session(authorization(interval=5))
        assert clock.slept == [
            5.0 + SLOW_DOWN_INCREMENT_SECONDS,
            5.0 + SLOW_DOWN_INCREMENT_SECONDS,
            5.0 + SLOW_DOWN_INCREMENT_SECONDS,
        ]

    @pytest.mark.asyncio
    async def test_widening_accumulates(self) -> None:
        client, clock = make_client([pending("slow_down"), pending("slow_down"), granted()])
        async with client:
            await client.poll_for_session(authorization(interval=5))
        assert clock.slept == [10.0, 15.0]

    @pytest.mark.asyncio
    async def test_widening_is_capped(self) -> None:
        """An unbounded interval could exceed the grant's own lifetime."""
        client, clock = make_client([pending("slow_down")] * 40 + [granted()])
        async with client:
            await client.poll_for_session(authorization(interval=5, expires_in=100_000))
        assert max(clock.slept) == float(MAX_INTERVAL_SECONDS)


class TestDeadline:
    @pytest.mark.asyncio
    async def test_expires_when_the_window_closes(self) -> None:
        client, _ = make_client([pending()])
        async with client:
            with pytest.raises(DeviceAuthExpiredError):
                await client.poll_for_session(authorization(interval=5, expires_in=12))

    @pytest.mark.asyncio
    async def test_never_sleeps_past_the_deadline(self) -> None:
        """Waiting a full interval with 2s left overshoots and misreports when."""
        client, clock = make_client([pending()])
        async with client:
            with pytest.raises(DeviceAuthExpiredError):
                await client.poll_for_session(authorization(interval=5, expires_in=12))
        assert sum(clock.slept) == 12.0
        assert clock.slept == [5.0, 5.0, 2.0]

    @pytest.mark.asyncio
    async def test_a_slow_interval_still_gets_one_poll(self) -> None:
        """Polling before the deadline check means a fast approval always wins."""
        client, _ = make_client([granted()])
        async with client:
            session = await client.poll_for_session(authorization(interval=600, expires_in=1))
        assert session.username == "colin"

    @pytest.mark.asyncio
    async def test_interval_has_a_floor(self) -> None:
        """However small a number the server sends, do not flood it."""
        client, clock = make_client([pending(), granted()])
        async with client:
            await client.poll_for_session(authorization(interval=1))
        assert clock.slept == [float(MIN_INTERVAL_SECONDS)]


class TestTerminalOutcomes:
    @pytest.mark.asyncio
    async def test_access_denied(self) -> None:
        client, _ = make_client([pending("access_denied")])
        async with client:
            with pytest.raises(DeviceAuthDeniedError, match="declined"):
                await client.poll_for_session(authorization())

    @pytest.mark.asyncio
    async def test_expired_token(self) -> None:
        client, _ = make_client([pending("expired_token")])
        async with client:
            with pytest.raises(DeviceAuthExpiredError):
                await client.poll_for_session(authorization())

    @pytest.mark.asyncio
    async def test_invalid_grant_is_distinct_from_expiry(self) -> None:
        """Also the response to re-polling a claimed code: single-use is enforced."""
        client, _ = make_client([pending("invalid_grant")])
        async with client:
            with pytest.raises(DeviceAuthRejectedError, match="no longer valid"):
                await client.poll_for_session(authorization())

    @pytest.mark.asyncio
    async def test_an_unknown_error_code_is_terminal(self) -> None:
        """The bug this guards: treating an unknown code as "keep waiting".

        That would poll a dead grant for the full ten minutes and then report a
        timeout, hiding whatever the server actually said.
        """
        client, clock = make_client([pending("something_new")])
        async with client:
            with pytest.raises(DeviceAuthError, match="because something_new"):
                await client.poll_for_session(authorization())
        assert clock.slept == []

    @pytest.mark.asyncio
    async def test_429_while_polling_is_not_slow_down(self) -> None:
        """The rate limiter, not the flow. Keyed on this device code's hash."""
        client, _ = make_client([httpx.Response(429, json={"detail": "too fast"})])
        async with client:
            with pytest.raises(RateLimitedError, match="too fast"):
                await client.poll_for_session(authorization())

    @pytest.mark.asyncio
    async def test_200_without_a_session_is_an_error(self) -> None:
        body = {k: v for k, v in TOKEN_BODY.items() if k != "session"}
        client, _ = make_client([httpx.Response(200, json=body)])
        async with client:
            with pytest.raises(DeviceAuthError, match="no session"):
                await client.poll_for_session(authorization())


class TestSecrets:
    def test_authorization_repr_hides_the_device_code(self) -> None:
        """The user code is meant to be read aloud; the device code is not."""
        rendered = repr(authorization())
        assert "d" * 43 not in rendered
        assert "Y4GN-WKY3" in rendered

    def test_session_repr_hides_the_sealed_value(self) -> None:
        rendered = repr(DeviceSession(**TOKEN_BODY))
        assert TOKEN_BODY["session"] not in rendered
        assert "colin" in rendered

    @pytest.mark.asyncio
    async def test_nothing_secret_reaches_the_log(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # `configure_logging` sets `propagate = False` on the package logger so
        # this app never writes to the terminal it is drawing on. caplog's
        # handler lives on the root logger, so without re-enabling propagation
        # this test passes vacuously the moment any other test has configured
        # logging — which is exactly how it first "passed".
        monkeypatch.setattr(logging.getLogger("agentcore_tui"), "propagate", True)
        caplog.set_level(logging.DEBUG, logger="agentcore_tui.client.device_auth")

        client, _ = make_client([granted()])
        async with client:
            result = await client.authorize()
            await client.poll_for_session(result)
        logged = caplog.text
        assert result.device_code not in logged
        assert str(TOKEN_BODY["session"]) not in logged
        # The user code is fine to log, and is the useful handle when debugging.
        assert "Y4GN-WKY3" in logged


class TestClientLifecycle:
    @pytest.mark.asyncio
    async def test_does_not_close_an_injected_client(self) -> None:
        """Borrowed sockets are the caller's to close."""
        injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: granted()))
        async with DeviceAuthClient(BASE_URL, client=injected):
            pass
        assert not injected.is_closed
        await injected.aclose()
