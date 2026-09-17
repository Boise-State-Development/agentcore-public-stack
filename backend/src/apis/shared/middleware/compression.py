"""StreamSafeGZipMiddleware — gzip JSON responses without touching SSE.

Nothing compressed app-api responses before this. CloudFront can't do it for
us: the `/api/*` behaviour is deliberately `compress: false`
(`spa-distribution-construct.ts`) precisely so CloudFront never buffers a
`text/event-stream`, and that decision stands — compression belongs at the
origin, where the response's own content type is known, not at an edge that
has to guess from a path pattern.

The payoff is on the JSON surface, which is most of what app-api serves; the
risk is entirely on the SSE surface, which is the product's main path (the
chat proxy in `app_api/chat/proxy_routes.py`, plus assistants and
`chat/converse_routes.py`). So this middleware is defined by what it refuses
to touch.

**Two things are passed straight through, untouched:**

1. Responses whose `Content-Type` is in :data:`EXCLUDED_CONTENT_TYPES` —
   `text/event-stream` first and foremost, plus payload types that are
   already compressed on the wire (zip archives, raster images, media,
   web fonts), where gzip costs CPU and returns nothing.
2. Responses that already carry a `Content-Encoding`, whatever it is. That
   header is the origin saying "this body is already encoded"; re-encoding
   it would produce a body no client can read.

Starlette's own `GZipMiddleware` skips both of those as of its
`DEFAULT_EXCLUDED_CONTENT_TYPES`/`content_encoding_set` checks, and this
class reuses that machinery rather than reimplementing it. What it adds is
the part Starlette does not do: **it sends `http.response.start` eagerly.**

Starlette's responders withhold the response-start message until the first
`http.response.body` arrives, because until then they can't know whether
they'll need to set `Content-Encoding` and drop `Content-Length`. That is
correct for a body they might compress, and wrong for one they've already
decided not to. On an SSE turn the first body chunk is the model's first
token — seconds away, or up to `_SSE_KEEPALIVE_SECONDS` if the turn opens
with a silent tool call — and today those response headers go out
immediately. Buffering them until the first event would delay every client's
"stream is open" transition behind the agent's thinking time, on the one
path in this service least worth adding a layer to (the same reasoning that
made `ProxiedRedirectMiddleware` raw ASGI rather than `BaseHTTPMiddleware`).

So: as soon as the response-start message identifies an excluded response,
this forwards it and flips into pure passthrough for the rest of the
exchange. Everything else — threshold, `Vary`, streaming compression,
`http.response.pathsend` — is Starlette's, unmodified.

Non-HTTP scopes (the voice WebSocket proxy) never reach a responder at all.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import (
    DEFAULT_EXCLUDED_CONTENT_TYPES,
    GZipMiddleware,
    GZipResponder,
    IdentityResponder,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Content-type prefixes this middleware never compresses.
#:
#: `text/event-stream` is the correctness entry — gzipping an SSE stream
#: delays or breaks event flushes. It comes from Starlette's own default so
#: the two can't drift; the rest are efficiency entries, payload types that
#: arrive already compressed and would only burn CPU for ~0% gain. Matched
#: with `str.startswith`, so a charset parameter (`text/event-stream;
#: charset=utf-8`, which Starlette appends to every `text/*` media type)
#: still matches.
#:
#: Deliberately *not* excluded: `application/pdf` and `image/svg+xml`, both
#: of which compress usefully and both of which CloudFront itself lists as
#: compressible.
EXCLUDED_CONTENT_TYPES: tuple[str, ...] = (
    *DEFAULT_EXCLUDED_CONTENT_TYPES,
    "application/gzip",
    "application/x-7z-compressed",
    "application/x-bzip2",
    "application/x-gzip",
    "application/x-zip-compressed",
    "application/zip",
    "audio/",
    "font/woff",
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "video/",
)


def is_excluded(headers: Headers) -> bool:
    """Whether this response must be passed through uncompressed.

    True for an already-encoded body and for any content type in
    :data:`EXCLUDED_CONTENT_TYPES`. A response with no `Content-Type` at all
    is not excluded — Starlette's size threshold already covers the empty
    bodies that case usually means.
    """
    if "content-encoding" in headers:
        return True
    return headers.get("content-type", "").startswith(EXCLUDED_CONTENT_TYPES)


class _EagerPassthroughMixin:
    """Forward an excluded response's start message without buffering it.

    Mixed in ahead of Starlette's responders so `super()` still owns every
    response this does *not* claim.
    """

    #: Class-level default; flipped on the instance by the first
    #: `http.response.start` that turns out to be excluded.
    _passthrough = False

    async def send_with_compression(self, message: Message) -> None:
        if not self._passthrough:
            if message["type"] != "http.response.start":
                await super().send_with_compression(message)
                return
            if not is_excluded(Headers(raw=message["headers"])):
                await super().send_with_compression(message)
                return
            # Excluded: nothing downstream will alter the headers, so the
            # client can have them now rather than when the first chunk
            # lands. `started` keeps the base class consistent in case
            # anything else consults it.
            self._passthrough = True
            self.started = True
        await self.send(message)


class _IdentityPassthroughResponder(_EagerPassthroughMixin, IdentityResponder):
    """Non-gzip clients: adds `Vary`, compresses nothing."""


class _GZipPassthroughResponder(_EagerPassthroughMixin, GZipResponder):
    """gzip-capable clients: compresses everything not excluded."""


class StreamSafeGZipMiddleware(GZipMiddleware):
    """`GZipMiddleware` that never buffers an excluded response.

    Same constructor and same behaviour for compressible responses; see the
    module docstring for what changes and why.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # WebSockets (voice mode) and lifespan: not ours.
            await self.app(scope, receive, send)
            return

        responder: ASGIApp
        if "gzip" in Headers(scope=scope).get("Accept-Encoding", ""):
            responder = _GZipPassthroughResponder(
                self.app, self.minimum_size, compresslevel=self.compresslevel
            )
        else:
            responder = _IdentityPassthroughResponder(self.app, self.minimum_size)

        await responder(scope, receive, send)
