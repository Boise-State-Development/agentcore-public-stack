"""Base user classes: login once, then behave like a browser tab.

Metric design (this is the part worth understanding before reading a report):

* ``POST /chat/stream`` is reported by Locust itself, and because the request
  is made with ``stream=True`` its response time is **time to response
  headers** — not the duration of the turn. Locust sets ``response_length`` to
  0 for streamed requests, so the byte counts on that row are meaningless.
* ``SSE chat: time to first token`` and ``SSE chat: full turn`` are fired
  explicitly below. These are the numbers that describe user experience.

Splitting them matters here. Per the observability steering doc, ALB
``TargetResponseTime`` does not complete until the stream closes, so a healthy
long turn looks like a slow request from the infrastructure's point of view.
Measuring time-to-first-token client-side is the only way to see whether the
platform got *responsive* quickly, independent of how long the answer was.
"""

from __future__ import annotations

import logging
import time
import uuid

from locust import HttpUser, between
from locust.exception import StopUser

from .auth import LoginError, establish_bff_session
from .config import ConfigError, Credential, LoadConfig, load_config, validate_host
from .sse import TURN_END_EVENT, SseEvent, iter_sse_events

logger = logging.getLogger(__name__)

TTFT_METRIC = "SSE chat: time to first token"
TURN_METRIC = "SSE chat: full turn"

# Round-robin index into the credential pool, so a pool smaller than the user
# count spreads evenly instead of piling every user onto the first account.
_credential_cursor = 0


def _next_credential(config: LoadConfig) -> Credential:
    global _credential_cursor
    credential = config.credentials[_credential_cursor % len(config.credentials)]
    _credential_cursor += 1
    return credential


class TurnResult:
    """What one chat turn produced."""

    __slots__ = ("chars", "first_token_ms", "total_ms", "stop_reason")

    def __init__(
        self,
        chars: int,
        first_token_ms: float | None,
        total_ms: float,
        stop_reason: str | None,
    ) -> None:
        self.chars = chars
        self.first_token_ms = first_token_ms
        self.total_ms = total_ms
        self.stop_reason = stop_reason


class AuthenticatedUser(HttpUser):
    """Logs in through the Hosted UI on start, then holds the session.

    Subclass and add tasks. One login per simulated user for the whole run,
    which is what a real browser tab does — re-authenticating per request would
    both distort the load and hammer Cognito.
    """

    abstract = True
    wait_time = between(5, 15)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.load_config: LoadConfig | None = None
        self.csrf_token: str | None = None
        self.credential: Credential | None = None

    def on_start(self) -> None:
        try:
            self.host = validate_host(self.host)
            self.load_config = load_config()
        except ConfigError as exc:
            # Configuration problems affect every user identically, so there is
            # no point letting hundreds of them fail one at a time.
            logger.error("Load test misconfigured: %s", exc)
            raise StopUser() from exc

        self.credential = _next_credential(self.load_config)
        try:
            self.csrf_token = establish_bff_session(self.client, self.load_config, self.credential)
        except LoginError as exc:
            logger.error("Login failed for %s: %s", self.credential.username, exc)
            raise StopUser() from exc

    def on_stop(self) -> None:
        """Drop the server-side session so a run does not leave rows behind."""
        if not self.csrf_token:
            return
        self.client.post(
            "/auth/logout",
            headers={"X-CSRF-Token": self.csrf_token},
            name="POST /auth/logout",
        )

    @property
    def csrf_headers(self) -> dict[str, str]:
        """CSRF header for unsafe requests.

        ``CSRFMiddleware`` enforces the double-submit check on every unsafe
        method once a BFF session is present, and ``/chat/stream`` is not
        exempt — there is a backend test asserting exactly that.
        """
        return {"X-CSRF-Token": self.csrf_token} if self.csrf_token else {}


class ChatUser(AuthenticatedUser):
    """Adds the SSE-instrumented chat turn."""

    abstract = True

    def new_conversation_id(self) -> str:
        """Mint a conversation id.

        Client-generated, matching the SPA: the backend creates the session row
        during the first turn rather than requiring a prior POST.
        """
        return str(uuid.uuid4())

    def build_payload(self, session_id: str, message: str) -> dict:
        config = self.load_config
        assert config is not None  # on_start guarantees this or stops the user

        # Mirrors buildChatRequestObject in the SPA's chat-request.service.ts.
        # None for model_id/provider is the "system default" signal.
        return {
            "session_id": session_id,
            "message": message,
            "model_id": config.model_id,
            "provider": config.provider,
            "enabled_tools": config.enabled_tools,
        }

    def chat_turn(self, session_id: str, message: str) -> TurnResult | None:
        """Send one turn and consume the whole stream.

        Returns ``None`` when the turn failed; the failure is already recorded
        against the relevant metric.
        """
        config = self.load_config
        assert config is not None

        payload = self.build_payload(session_id, message)
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **self.csrf_headers,
        }

        started = time.perf_counter()
        first_token_at: float | None = None
        chars = 0
        stop_reason: str | None = None
        saw_terminator = False

        with self.client.post(
            "/chat/stream",
            json=payload,
            headers=headers,
            stream=True,
            catch_response=True,
            name="POST /chat/stream",
            timeout=config.turn_timeout_seconds,
        ) as response:
            if response.status_code != 200:
                detail = self._describe_error(response.status_code)
                response.failure(detail)
                return None

            # Headers are in. Everything past here is stream time, which the
            # two custom metrics below cover.
            response.success()

            try:
                for event in iter_sse_events(response.iter_lines(decode_unicode=True)):
                    if first_token_at is None and event.is_first_token_candidate:
                        first_token_at = time.perf_counter()
                        self._fire(
                            TTFT_METRIC,
                            (first_token_at - started) * 1000.0,
                        )

                    chars += len(event.text)
                    stop_reason = self._track_stop_reason(event, stop_reason)

                    if event.name == TURN_END_EVENT:
                        saw_terminator = True
                        break
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                self._fire(
                    TURN_METRIC,
                    (time.perf_counter() - started) * 1000.0,
                    exception=exc,
                )
                return None

        total_ms = (time.perf_counter() - started) * 1000.0

        if not saw_terminator:
            self._fire(
                TURN_METRIC,
                total_ms,
                exception=RuntimeError(
                    f"stream ended without a '{TURN_END_EVENT}' event "
                    f"(last stopReason={stop_reason!r})"
                ),
            )
            return None

        if stop_reason == "error":
            # The agent emitted an error turn: quota block, model failure, or a
            # tool blowing up. HTTP was 200, so only the stream reveals it.
            self._fire(
                TURN_METRIC,
                total_ms,
                exception=RuntimeError("agent returned stopReason=error"),
            )
            return None

        self._fire(TURN_METRIC, total_ms, response_length=chars)
        return TurnResult(
            chars=chars,
            first_token_ms=None if first_token_at is None else (first_token_at - started) * 1000.0,
            total_ms=total_ms,
            stop_reason=stop_reason,
        )

    @staticmethod
    def _track_stop_reason(event: SseEvent, current: str | None) -> str | None:
        if event.name != "message_stop":
            return current
        reason = event.data.get("stopReason")
        return str(reason) if reason else current

    def _fire(
        self,
        name: str,
        response_time_ms: float,
        response_length: int = 0,
        exception: BaseException | None = None,
    ) -> None:
        self.environment.events.request.fire(
            request_type="SSE",
            name=name,
            response_time=response_time_ms,
            response_length=response_length,
            exception=exception,
            context=self.context(),
        )

    @staticmethod
    def _describe_error(status_code: int) -> str:
        """Attach the likely cause to the status code.

        These three are the ones a load run actually hits, and each is a
        different kind of problem — worth naming so a report does not just say
        "403" and leave the reader guessing.
        """
        hints = {
            401: "401 — no active BFF session (cookie expired or not stored; https required)",
            403: "403 — CSRF token missing or invalid",
            429: "429 — rate limited",
            502: "502 — inference API unreachable",
            504: "504 — inference API timed out",
        }
        return hints.get(status_code, f"unexpected status {status_code}")
