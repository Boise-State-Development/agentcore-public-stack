"""Streaming-behavior integration test for `POST /chat/stream`.

The migration plan calls out a buffering risk on SSE events that arrive
late in the stream — most importantly the `oauth_required` event, which
is emitted *after* `message_stop` and would be hidden from the SPA if
the proxy (or any intermediary) read the upstream response to completion
before flushing headers downstream.

This test runs the proxy behind a real uvicorn server (NOT
`httpx.ASGITransport`, which buffers the entire body before exposing
headers — useless for measuring TTFB) and points it at a slow upstream
that yields one SSE chunk, sleeps, then yields more. We assert:

  - response headers reach the client well under 200ms (TTFB),
  - `X-Accel-Buffering: no` is set on the response,
  - total stream consumption takes at least the upstream gap, proving
    the body is actually streamed (not buffered then flushed).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from apis.app_api.chat import proxy_routes
from apis.app_api.chat.proxy_routes import router as proxy_router
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User


# Upstream delay between SSE chunks — short enough to keep the test fast,
# long enough that buffering vs. streaming is unambiguously distinguishable.
_UPSTREAM_GAP_SECONDS = 0.3
_TTFB_BUDGET_SECONDS = 0.2


def _user() -> User:
    user = User(
        email="alice@example.com",
        user_id="user-sub",
        name="Alice",
        roles=["user"],
    )
    user.raw_token = "access.token"
    return user


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _UvicornInThread:
    """Tiny harness: run uvicorn in a daemon thread on a free port, block
    until the socket accepts connections, and tear it down on exit."""

    def __init__(self, app: FastAPI) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                lifespan="off",
                access_log=False,
            )
        )
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_UvicornInThread":
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        # Wait for socket to accept connections — bounded so we fail fast
        # if uvicorn never came up.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("uvicorn server failed to start within 5s")

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(proxy_router)
    app.dependency_overrides[get_current_user_from_session] = _user
    return app


def _patch_slow_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the proxy's upstream client with one whose MockTransport
    yields one SSE chunk, sleeps, then yields more."""

    async def slow_content():
        yield b'event: message_start\ndata: {"role": "assistant"}\n\n'
        await asyncio.sleep(_UPSTREAM_GAP_SECONDS)
        yield b'event: content_block_delta\ndata: {"text": "hi"}\n\n'
        yield b'event: message_stop\ndata: {"stopReason": "end_turn"}\n\n'
        yield b'event: oauth_required\ndata: {"providerId":"slack"}\n\n'
        yield b"event: done\ndata: {}\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=slow_content(),
            headers={"content-type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        proxy_routes,
        "_build_upstream_client",
        lambda: httpx.AsyncClient(transport=transport),
    )


@pytest.mark.asyncio
async def test_ttfb_under_200ms_with_x_accel_buffering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_slow_upstream(monkeypatch)
    app = _build_app()

    with _UvicornInThread(app) as server:
        async with httpx.AsyncClient(base_url=server.url, timeout=10.0) as client:
            t0 = time.monotonic()
            async with client.stream(
                "POST", "/chat/stream", json={"message": "hi"}
            ) as response:
                ttfb = time.monotonic() - t0
                assert response.status_code == 200
                assert response.headers["x-accel-buffering"] == "no"
                assert response.headers["cache-control"] == "no-cache"
                assert response.headers["content-type"].startswith(
                    "text/event-stream"
                )
                assert ttfb < _TTFB_BUDGET_SECONDS, (
                    f"TTFB {ttfb:.3f}s exceeded {_TTFB_BUDGET_SECONDS}s budget — "
                    "the proxy is buffering upstream before flushing headers."
                )

                chunks = []
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                body = b"".join(chunks)
                total = time.monotonic() - t0

    assert total >= _UPSTREAM_GAP_SECONDS, (
        f"Total {total:.3f}s shorter than upstream gap {_UPSTREAM_GAP_SECONDS}s "
        "— upstream stream did not actually slow-yield."
    )
    assert b"oauth_required" in body
    assert b"event: done" in body
