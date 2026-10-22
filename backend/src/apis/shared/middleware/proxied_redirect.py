"""ProxiedRedirectMiddleware — keep app-generated redirects on the public URL.

app-api never sees the URL the browser actually asked for. CloudFront's
`/api/*` behaviour strips the `/api` prefix, sends the request to the ALB
under the origin's own hostname (`ALL_VIEWER_EXCEPT_HOST_HEADER`), and the
ALB terminates TLS — so a request the user made as

    GET https://dev.boisestate.ai/api/agents/

arrives at Starlette as path `/agents/`, `Host: api.dev.boisestate.ai`,
scheme `http`. Starlette's `redirect_slashes` then answers it with an
*absolute* `Location` built from what it can see:

    Location: http://api.dev.boisestate.ai/agents

which is wrong three times over. The browser refuses it outright as mixed
content ("was loaded over HTTPS, but requested an insecure resource"), so
the caller silently gets nothing; the internal ALB hostname leaks into a
page the user can read; and even if a client did follow it, the host is a
different origin, so the `__Host-` BFF session cookies would not be sent
and the redirected request would 401.

This middleware rewrites exactly those self-referential redirects into a
root-relative `Location`, restoring the proxy's stripped prefix from
`X-Forwarded-Prefix` (stamped by the CloudFront path-strip Function):

    Location: /api/agents

A root-relative `Location` inherits the browser's own scheme and host, so it
is correct under CloudFront, behind a bare ALB, and on localhost without the
app having to be told which one it is under.

**Only self-referential redirects are touched.** A `Location` pointing at
another host is left exactly as-is — that is the BFF's OAuth flow bouncing
to Cognito/Entra and back to the SPA origin, and rewriting those would break
sign-in.

Implemented as raw ASGI rather than `BaseHTTPMiddleware` on purpose: it needs
nothing but the response headers, and `BaseHTTPMiddleware` would wrap every
response body in an extra stream — including the SSE chat streams, which are
the one thing in this service least worth putting another layer around.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Set by the CloudFront path-strip Function to the prefix it removed.
FORWARDED_PREFIX_HEADER = "x-forwarded-prefix"


def _sanitize_prefix(raw: str | None) -> str:
    """Normalise `X-Forwarded-Prefix` to `''` or `/segment[/segment...]`.

    The header reaches us through CloudFront, which overwrites it on every
    request to this origin, so a viewer cannot choose its value. It is still
    normalised here rather than trusted: this middleware writes the result
    into a `Location`, and a value like `//evil.example` would turn a
    same-origin redirect into a scheme-relative jump off-site.
    """
    if not raw:
        return ""
    prefix = raw.strip().rstrip("/")
    if not prefix.startswith("/") or prefix.startswith("//"):
        return ""
    return prefix


def rewrite_location(location: str, request_headers: Headers) -> str | None:
    """Return the rewritten `Location`, or `None` to leave it alone.

    `None` covers everything that is already correct or none of our business:
    an already-relative `Location`, and any absolute one aimed at a different
    host than the one this request arrived on.
    """
    if not location:
        return None

    parts = urlsplit(location)
    if not parts.netloc:
        # Already relative — the browser resolves it against the public URL.
        return None

    request_host = request_headers.get("host", "")
    if parts.netloc.lower() != request_host.lower():
        # Some other origin (Cognito, Entra, the SPA). Not ours to touch.
        return None

    prefix = _sanitize_prefix(request_headers.get(FORWARDED_PREFIX_HEADER))
    rewritten = f"{prefix}{parts.path}"
    if parts.query:
        rewritten = f"{rewritten}?{parts.query}"
    if parts.fragment:
        rewritten = f"{rewritten}#{parts.fragment}"

    # A redirect to the host root with no prefix leaves nothing to anchor on.
    if not rewritten.startswith("/"):
        rewritten = f"/{rewritten}"
    return rewritten


class ProxiedRedirectMiddleware:
    """Rewrite self-referential absolute redirects to root-relative ones."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_headers = Headers(scope=scope)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = message["status"]
                if 300 <= status < 400:
                    headers = MutableHeaders(scope=message)
                    location = headers.get("location")
                    rewritten = rewrite_location(location or "", request_headers)
                    if rewritten is not None:
                        logger.debug(
                            "Rewrote proxied redirect %s -> %s", location, rewritten
                        )
                        headers["location"] = rewritten
            await send(message)

        await self.app(scope, receive, send_wrapper)
