"""The CLI device-authorization flow: how this client gets a BFF session.

app-api's own flow, not Cognito's — Cognito does not implement RFC 8628, and the
BFF app client is confidential so a code exchange could never happen out here
anyway. Loopback redirect was also ruled out: Cognito matches ``redirect_uri``
byte-for-byte and does not honour RFC 8252's variable-port rule, which a
container cannot satisfy. So the client polls instead of receiving a redirect.

The shape, from the client's side:

1. ``POST /auth/cli/authorize`` returns a ``device_code`` this process keeps, a
   short ``user_code`` for the human, and where to send them.
2. The human opens ``verification_uri_complete``, signs in normally, approves.
3. ``POST /auth/cli/token`` is polled until it stops saying "not yet". Success
   returns a **sealed session** — the same envelope the SPA holds in a cookie,
   which is what makes it safe to hand out and useless to inspect.

Two things about the wire that shape the code below:

* **Every non-success poll is an HTTP 400** carrying an RFC 8628 ``error`` code.
  The status is therefore not the diagnosis; the code is. Branching on status
  would collapse "wait longer" and "this will never work" into one case.
* **``slow_down`` is not advisory.** The server throttles from the *previous*
  poll's timestamp, so a client that ignores the interval never advances past
  ``slow_down`` — it can burn its whole ten-minute window being told to wait.
  The interval is widened permanently when that arrives, per RFC 8628 §3.5.

Transport only: no keyring, no config writing, no printing. The caller decides
where the session goes and how progress is shown, which is what lets the CLI and
(later) the TUI share this.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .. import __version__
from ..errors import (
    ConnectionFailedError,
    DeviceAuthDeniedError,
    DeviceAuthError,
    DeviceAuthExpiredError,
    DeviceAuthRejectedError,
    RateLimitedError,
)
from .endpoints import Endpoints

logger = logging.getLogger(__name__)

USER_AGENT = f"agentcore-tui/{__version__}"

#: Fallbacks for a server that omits them. The real server always sends both.
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_EXPIRES_IN_SECONDS = 600

#: Added to the poll interval each time the server says ``slow_down``.
#: RFC 8628 §3.5 specifies five seconds.
SLOW_DOWN_INCREMENT_SECONDS = 5

#: Refuse to poll faster than this however small an interval the server asks
#: for, so a misconfigured or hostile response cannot turn this loop into a
#: request flood against the deployment.
MIN_INTERVAL_SECONDS = 1

#: Ceiling on the widened interval. Without one, a long run of `slow_down` could
#: push the next poll past the grant's own expiry and turn a recoverable
#: throttle into a guaranteed timeout.
MAX_INTERVAL_SECONDS = 60

#: Wall-clock cap on one HTTP call. Polling is a fast endpoint; a hung socket
#: should surface rather than silently consume the approval window.
REQUEST_TIMEOUT_SECONDS = 30.0


# RFC 8628 error codes. Grouped on a class rather than left as module-level
# constants because `match` treats a bare name as a *capture* pattern: `case
# _PENDING:` would bind every value to `_PENDING` and make the rest of the
# branches unreachable. A dotted name is a value pattern, which is what this
# needs. Ruff catches the mistake, but only after it is written.
class _Code:
    PENDING = "authorization_pending"
    SLOW_DOWN = "slow_down"
    EXPIRED = "expired_token"
    DENIED = "access_denied"
    INVALID = "invalid_grant"


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    """A started sign-in: what to show the human, and how to poll for it."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int = DEFAULT_EXPIRES_IN_SECONDS
    interval: int = DEFAULT_INTERVAL_SECONDS

    def __repr__(self) -> str:
        # The device code is the secret half of this pair. The user code is not
        # — it is meant to be read aloud — so showing it aids debugging.
        return f"DeviceAuthorization(user_code={self.user_code!r}, expires_in={self.expires_in})"


@dataclass(frozen=True, slots=True)
class DeviceSession:
    """A claimed session. ``session`` is returned by the server exactly once."""

    session: str
    expires_in: int
    user_id: str
    username: str

    def __repr__(self) -> str:
        return f"DeviceSession(username={self.username!r}, expires_in={self.expires_in})"


#: Called before each wait with (seconds until the next poll, seconds left).
#: Lets a caller render progress without this module knowing about a terminal.
ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class _Pending:
    """Not approved yet. ``slow_down`` means widen the interval and continue.

    An explicit result type rather than ``None`` plus state on the client: the
    poll loop needs two bits of information ("keep waiting" and "you were too
    fast"), and stashing the second on the instance would make the loop's
    behaviour depend on an attribute no signature mentions.
    """

    slow_down: bool = False


class DeviceAuthClient:
    """Drives the device flow against one deployment.

    Constructor-injectable with an ``httpx.AsyncClient`` so tests drive it with
    ``httpx.MockTransport`` and never open a socket. ``sleep`` and ``clock`` are
    injectable for the same reason: the polling loop is mostly timing behaviour,
    and asserting on it must not take ten real minutes.
    """

    __slots__ = ("_client", "_clock", "_endpoints", "_owns_client", "_sleep")

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._endpoints = Endpoints(base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
        self._sleep = sleep or asyncio.sleep
        # Monotonic: the deadline must survive a clock adjustment mid-flow.
        self._clock = clock or time.monotonic

    async def __aenter__(self) -> DeviceAuthClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying client, but only if we created it."""
        if self._owns_client:
            await self._client.aclose()

    # -- step 1: start -------------------------------------------------------

    async def authorize(self) -> DeviceAuthorization:
        """Start a sign-in. Returns the codes and where to send the human."""
        response = await self._post(self._endpoints.cli_authorize, payload={})

        if response.status_code == 429:
            raise RateLimitedError(_detail(response) or "Too many sign-in attempts", retry_after=_retry_after(response))
        if response.status_code != 200:
            raise DeviceAuthError(
                _detail(response) or f"Could not start sign-in (HTTP {response.status_code})",
                hint="Check the base URL points at app-api, and that the deployment includes the CLI auth routes.",
            )

        body = _json_object(response)
        try:
            authorization = DeviceAuthorization(
                device_code=str(body["device_code"]),
                user_code=str(body["user_code"]),
                verification_uri=str(body["verification_uri"]),
                # Fall back to the plain URI: the user can still type the code.
                verification_uri_complete=str(body.get("verification_uri_complete") or body["verification_uri"]),
                expires_in=_positive_int(body.get("expires_in"), DEFAULT_EXPIRES_IN_SECONDS),
                interval=_positive_int(body.get("interval"), DEFAULT_INTERVAL_SECONDS),
            )
        except KeyError as exc:
            raise DeviceAuthError(
                f"The server's sign-in response was missing {exc.args[0]!r}",
                hint="This usually means the base URL is not an app-api, or the deployment predates CLI sign-in.",
            ) from exc

        logger.info("Device authorization started, user_code=%s expires_in=%ss", authorization.user_code, authorization.expires_in)
        return authorization

    # -- step 3: poll --------------------------------------------------------

    async def poll_for_session(
        self,
        authorization: DeviceAuthorization,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> DeviceSession:
        """Poll until the sign-in is approved, or fail with a typed error.

        Polls immediately, then waits ``interval`` between attempts. The first
        poll is not delayed on purpose: it costs one request and it proves the
        grant exists, so a client pointed at the wrong host or carrying a
        mangled device code learns now rather than after a five-second wait.

        Raises rather than returning a status, because every non-success outcome
        is terminal for *this* authorization: the caller's only recovery is to
        start a new one, and there is no partial result worth handing back.
        """
        interval = max(authorization.interval, MIN_INTERVAL_SECONDS)
        deadline = self._clock() + authorization.expires_in

        while True:
            result = await self._poll_once(authorization.device_code)
            if isinstance(result, DeviceSession):
                logger.info("Device authorization claimed for %s", result.username)
                return result

            if result.slow_down:
                interval = min(interval + SLOW_DOWN_INCREMENT_SECONDS, MAX_INTERVAL_SECONDS)
                logger.debug("Server asked us to slow down; interval now %ss", interval)

            remaining = deadline - self._clock()
            if remaining <= 0:
                raise DeviceAuthExpiredError()
            # Do not sleep past the deadline: waiting a full interval when only
            # two seconds remain wastes the tail of the window and reports an
            # expiry later than it happened.
            wait = min(float(interval), remaining)
            if on_progress is not None:
                on_progress(int(wait), int(remaining))
            await self._sleep(wait)

    async def _poll_once(self, device_code: str) -> DeviceSession | _Pending:
        """One poll. :class:`_Pending` means keep waiting; terminal cases raise."""
        response = await self._post(self._endpoints.cli_token, payload={"device_code": device_code})

        if response.status_code == 200:
            body = _json_object(response)
            try:
                return DeviceSession(
                    session=str(body["session"]),
                    expires_in=_positive_int(body.get("expires_in"), 0),
                    user_id=str(body.get("user_id", "")),
                    username=str(body.get("username", "")),
                )
            except KeyError as exc:
                raise DeviceAuthError(
                    "The server approved the sign-in but returned no session",
                    hint="Retry the sign-in. If it recurs, the deployment's session sealing may be misconfigured.",
                ) from exc

        if response.status_code == 429:
            # Distinct from `slow_down`: the rate limiter, not the flow. Keyed
            # on this device code's hash, so it is this client's own doing.
            raise RateLimitedError(_detail(response) or "Polling too fast", retry_after=_retry_after(response))

        error = str(_json_object(response).get("error", ""))

        match error:
            case _Code.PENDING:
                return _Pending()
            case _Code.SLOW_DOWN:
                return _Pending(slow_down=True)
            case _Code.DENIED:
                raise DeviceAuthDeniedError()
            case _Code.EXPIRED:
                raise DeviceAuthExpiredError()
            case _Code.INVALID:
                raise DeviceAuthRejectedError()
            case _:
                # An unrecognised code is terminal. Treating an unknown error as
                # "keep waiting" would poll a dead grant for ten minutes and
                # report a timeout instead of what the server actually said.
                raise DeviceAuthError(
                    _detail(response) or f"The sign-in failed ({error or f'HTTP {response.status_code}'})",
                    hint="Run `agentcore-tui login --sso` to start a new sign-in.",
                )

    # -- transport -----------------------------------------------------------

    async def _post(self, url: str, *, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self._client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
        except httpx.HTTPError as exc:
            raise ConnectionFailedError(self._endpoints.base, f"{type(exc).__name__}: {exc}") from exc


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """The response body as a dict, or an empty one.

    Never raises: a proxy returning an HTML error page must surface as the
    status-code path, not as a JSON decode traceback.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _detail(response: httpx.Response) -> str:
    """A human-readable message from the server, if it sent one."""
    body = _json_object(response)
    for key in ("error_description", "detail", "message"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _retry_after(response: httpx.Response) -> int | None:
    try:
        return int(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _positive_int(value: object, fallback: int) -> int:
    """Coerce a server-supplied number, refusing nonsense.

    A zero or negative interval would busy-loop, and a negative expiry would
    make the deadline already past.
    """
    try:
        parsed = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback
