"""Loopback redirect receiver (RFC 8252 §7.3).

The browser is the user-agent for the authorization request; the CLI needs the
resulting code back. RFC 8252's answer for native apps is a short-lived HTTP
listener on the loopback interface.

Two Cognito-specific constraints shape this:

* Cognito matches ``redirect_uri`` byte-for-byte and does **not** honour the
  RFC's "treat the port as variable for loopback" rule. So the port cannot be
  ephemeral — it must be one that was registered on the app client, which is why
  this takes a candidate list and tries each in turn.
* Only ``localhost``, ``127.0.0.1``, and ``[::1]`` may use plain http. This
  binds ``127.0.0.1`` explicitly rather than ``0.0.0.0`` so the listener is not
  reachable from the network for the seconds it is alive.
"""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from ..errors import AgentCoreTuiError

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/callback"
BIND_HOST = "127.0.0.1"

#: The redirect host registered on the app client. Cognito needs the URI to
#: match exactly, and `localhost` is what the callback URLs are registered as.
REDIRECT_HOST = "localhost"


class LoopbackError(AgentCoreTuiError):
    """No registered port could be bound."""

    hint = "Close whatever is using those ports, or register another port on the app client."


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """What came back on the redirect."""

    code: str | None
    state: str | None
    error: str | None = None
    error_description: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.code) and not self.error


_SUCCESS_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Signed in</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;padding:3rem;text-align:center">
<h1 style="font-size:1.25rem">Signed in</h1>
<p style="color:#555">You can close this tab and return to the terminal.</p>
</body></html>"""

_FAILURE_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Sign-in failed</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;padding:3rem;text-align:center">
<h1 style="font-size:1.25rem">Sign-in failed</h1>
<p style="color:#555">Return to the terminal for details.</p>
</body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Serves exactly one redirect, then lets the server shut down."""

    # Set by the receiver before serving.
    result: CallbackResult | None = None

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") not in (CALLBACK_PATH.rstrip("/"), ""):
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)

        def first(name: str) -> str | None:
            values = params.get(name)
            return values[0] if values else None

        result = CallbackResult(
            code=first("code"),
            state=first("state"),
            error=first("error"),
            error_description=first("error_description"),
        )
        type(self).result = result

        body = _SUCCESS_HTML if result.ok else _FAILURE_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # The URL in the address bar holds the authorization code; discourage
        # anything from caching or referring it onward.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silence stdout logging — it would paint over the terminal UI."""
        logger.debug("loopback: " + format, *args)


class LoopbackReceiver:
    """Binds a registered loopback port and waits for one redirect."""

    def __init__(self, ports: list[int]) -> None:
        if not ports:
            raise LoopbackError("No callback ports configured")
        self._ports = ports
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int | None = None
        # Set in start(): a fresh subclass per attempt, so a retry never sees a
        # previous attempt's captured result.
        self._handler: type[_CallbackHandler] | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise LoopbackError("Receiver is not listening")
        return self._port

    @property
    def redirect_uri(self) -> str:
        """Must equal a URI registered on the app client, exactly."""
        return f"http://{REDIRECT_HOST}:{self.port}{CALLBACK_PATH}"

    def __enter__(self) -> LoopbackReceiver:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def start(self) -> None:
        """Bind the first available candidate port and serve in a thread."""
        last_error: OSError | None = None
        for port in self._ports:
            try:
                handler = type("_BoundCallbackHandler", (_CallbackHandler,), {"result": None})
                server = HTTPServer((BIND_HOST, port), handler)
            except OSError as exc:
                logger.debug("port %d unavailable: %s", port, exc)
                last_error = exc
                continue

            self._server = server
            self._handler = handler
            self._port = port
            self._thread = threading.Thread(target=server.serve_forever, daemon=True, name="agentcore-tui-oauth")
            self._thread.start()
            logger.info("loopback listener on http://%s:%d%s", BIND_HOST, port, CALLBACK_PATH)
            return

        raise LoopbackError(
            f"Could not bind any of the registered callback ports: {self._ports}" + (f" (last error: {last_error})" if last_error else "")
        )

    def wait(self, timeout: float = 300.0) -> CallbackResult:
        """Block until the redirect arrives, or time out.

        The timeout has to be generous: the user may be completing MFA or an
        SSO hop in the browser.
        """
        if self._server is None:
            raise LoopbackError("Receiver is not listening")

        deadline = threading.Event()
        waited = 0.0
        step = 0.1
        while waited < timeout:
            result = getattr(self._handler, "result", None)
            if isinstance(result, CallbackResult):
                return result
            deadline.wait(step)
            waited += step

        raise LoopbackError(
            f"No redirect received within {timeout:.0f}s",
            hint="The browser never returned to the CLI. Check that it opened, and retry.",
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def find_free_port(ports: list[int]) -> int | None:
    """First candidate port that can currently be bound, or None."""
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((BIND_HOST, port))
            except OSError:
                continue
            return port
    return None
