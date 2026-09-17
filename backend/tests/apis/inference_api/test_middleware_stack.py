"""inference-api must not go back to stock `GZipMiddleware`.

This service exists to serve one route — the `/invocations` SSE turn — and
Starlette's stock gzip middleware withholds `http.response.start` until the
first body chunk arrives. On an SSE turn that chunk is the model's first
token, so the response headers landed behind the agent's whole thinking time,
with or without `Accept-Encoding: gzip`. app-api's chat proxy reads this
response's `content-type` before it can open its own stream to the SPA, so
the delay reached the browser.

The swap to `StreamSafeGZipMiddleware` is easy to undo by accident — the two
have the same constructor and compress identically — so it is pinned here.
"""

from __future__ import annotations

from starlette.middleware.gzip import GZipMiddleware

from apis.inference_api.main import app
from apis.shared.middleware.compression import StreamSafeGZipMiddleware


def test_compression_is_the_stream_safe_variant() -> None:
    installed = [m.cls for m in app.user_middleware]

    assert StreamSafeGZipMiddleware in installed
    # `is not` rather than membership: the stream-safe class *subclasses*
    # `GZipMiddleware`, so an `in` check would pass either way.
    assert not any(cls is GZipMiddleware for cls in installed)


def test_compression_settings_are_unchanged_by_the_swap() -> None:
    gzip_middleware = next(
        m for m in app.user_middleware if m.cls is StreamSafeGZipMiddleware
    )

    assert gzip_middleware.kwargs["minimum_size"] == 1000
    assert gzip_middleware.kwargs["compresslevel"] == 6
