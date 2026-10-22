"""Tests for StreamSafeGZipMiddleware.

app-api compresses its JSON surface, and the entire risk of doing so lands on
the one surface it must never touch: `text/event-stream`. Compressing an SSE
stream — or merely *buffering its response headers* — degrades or breaks the
chat path, which is the product's main path.

So most of what follows is about what the middleware refuses to do. The
sharpest test is `test_excluded_response_start_is_sent_before_first_chunk`:
Starlette's stock responders withhold `http.response.start` until the first
body message arrives, which on a chat turn is the model's first token. That
would put the agent's thinking time in front of every client's "stream is
open" transition, so this middleware forwards the start message the moment
it can see the response is excluded.
"""

from __future__ import annotations

import asyncio
import gzip
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES
from starlette.responses import Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from apis.shared.middleware.compression import (
    EXCLUDED_CONTENT_TYPES,
    StreamSafeGZipMiddleware,
)

GZIP = {"Accept-Encoding": "gzip"}

#: Comfortably over the 500-byte floor these tests configure, and
#: compressible enough that the result is smaller than the input.
BIG_PAYLOAD = {"rows": [{"name": f"row {i}", "status": "OK"} for i in range(200)]}


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(StreamSafeGZipMiddleware, minimum_size=500, compresslevel=6)

    @app.get("/big")
    def big() -> dict:
        return BIG_PAYLOAD

    @app.get("/small")
    def small() -> dict:
        return {"ok": True}

    @app.get("/sse")
    def sse() -> StreamingResponse:
        async def events():
            for i in range(50):
                yield f"data: {json.dumps({'i': i, 'text': 'x' * 100})}\n\n".encode()

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/zip")
    def archive() -> Response:
        return Response(b"PK\x03\x04" + b"\x00" * 4000, media_type="application/zip")

    @app.get("/pre-encoded")
    def pre_encoded() -> Response:
        # A handler that compressed its own body — e.g. a proxied upstream
        # response relayed with its encoding intact.
        return Response(
            gzip.compress(b"already encoded " * 500),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"},
        )

    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# --- the JSON surface: this is the payoff ---------------------------------


def test_large_json_is_compressed_and_round_trips(client: TestClient) -> None:
    raw = client.get("/big", headers={"Accept-Encoding": "identity"})
    compressed = client.get("/big", headers=GZIP)

    assert compressed.headers["content-encoding"] == "gzip"
    # httpx decodes transparently; the decoded body must be byte-identical.
    assert compressed.json() == BIG_PAYLOAD
    assert int(compressed.headers["content-length"]) < len(raw.content)
    assert "accept-encoding" in compressed.headers["vary"].lower()


def test_small_json_is_left_alone(client: TestClient) -> None:
    response = client.get("/small", headers=GZIP)

    assert "content-encoding" not in response.headers
    assert response.json() == {"ok": True}


def test_client_without_gzip_gets_an_uncompressed_body(client: TestClient) -> None:
    response = client.get("/big", headers={"Accept-Encoding": "identity"})

    assert "content-encoding" not in response.headers
    assert response.json() == BIG_PAYLOAD
    # Still varies, so a cache can't hand this body to a gzip client.
    assert "accept-encoding" in response.headers["vary"].lower()


# --- the SSE surface: this is the risk ------------------------------------


def test_sse_is_never_compressed(client: TestClient) -> None:
    response = client.get("/sse", headers=GZIP)

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.headers["content-type"].startswith("text/event-stream")
    # Every frame intact, in order, and readable as plain text.
    frames = [f for f in response.text.split("\n\n") if f]
    assert len(frames) == 50
    assert json.loads(frames[0].removeprefix("data: "))["i"] == 0
    assert json.loads(frames[-1].removeprefix("data: "))["i"] == 49


async def _drive(middleware: StreamSafeGZipMiddleware, scope: Scope) -> list[Message]:
    """Run `middleware` over `scope`, returning the messages it sent."""
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:  # pragma: no cover - never awaited
        return {"type": "http.disconnect"}

    await middleware(scope, receive, send)
    return sent


@pytest.mark.parametrize("accept_encoding", [b"gzip", b"identity"])
def test_excluded_response_start_is_sent_before_first_chunk(
    accept_encoding: bytes,
) -> None:
    """Headers must not wait on the agent's first token.

    The stub below opens an SSE response and then stalls, exactly like a turn
    that spends its first seconds on a tool call. Stock `GZipMiddleware`
    would hold `http.response.start` for the whole stall; this must not.
    """
    first_chunk_released = asyncio.Event()

    async def stalling_sse(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
            }
        )
        await first_chunk_released.wait()
        await send({"type": "http.response.body", "body": b"data: hi\n\n"})

    middleware = StreamSafeGZipMiddleware(stalling_sse, minimum_size=500)
    scope: Scope = {
        "type": "http",
        "headers": [(b"accept-encoding", accept_encoding)],
    }

    async def scenario() -> list[Message]:
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        async def receive() -> Message:  # pragma: no cover - never awaited
            return {"type": "http.disconnect"}

        task = asyncio.create_task(middleware(scope, receive, send))
        # Let the stub reach its stall, then look at what the client has.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        headers_before_body = list(sent)

        first_chunk_released.set()
        await task
        return headers_before_body

    headers_before_body = asyncio.run(scenario())

    assert [m["type"] for m in headers_before_body] == ["http.response.start"]
    assert not any(
        name.lower() == b"content-encoding"
        for name, _ in headers_before_body[0]["headers"]
    )


def test_compressible_streaming_response_still_withholds_start() -> None:
    """The eager path is scoped to excluded responses only.

    A compressible stream *must* keep buffering its start message — that is
    where `Content-Encoding` gets added and `Content-Length` dropped.
    """
    released = asyncio.Event()

    async def stalling_json(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await released.wait()
        await send(
            {"type": "http.response.body", "body": b"x" * 2000, "more_body": True}
        )
        await send({"type": "http.response.body", "body": b""})

    middleware = StreamSafeGZipMiddleware(stalling_json, minimum_size=500)
    scope: Scope = {"type": "http", "headers": [(b"accept-encoding", b"gzip")]}

    async def scenario() -> tuple[list[Message], list[Message]]:
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        async def receive() -> Message:  # pragma: no cover - never awaited
            return {"type": "http.disconnect"}

        task = asyncio.create_task(middleware(scope, receive, send))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        before = list(sent)

        released.set()
        await task
        return before, sent

    before, after = asyncio.run(scenario())

    assert before == []
    assert after[0]["type"] == "http.response.start"
    assert (b"content-encoding", b"gzip") in after[0]["headers"]


# --- other pass-throughs ---------------------------------------------------


def test_pre_encoded_body_is_not_re_encoded(client: TestClient) -> None:
    response = client.get("/pre-encoded", headers=GZIP)

    assert response.headers["content-encoding"] == "gzip"
    # One layer of gzip, not two: httpx strips exactly one.
    assert response.content == b"already encoded " * 500


def test_already_compressed_payload_types_are_skipped(client: TestClient) -> None:
    response = client.get("/zip", headers=GZIP)

    assert "content-encoding" not in response.headers
    assert response.content.startswith(b"PK\x03\x04")


def test_non_http_scopes_are_passed_straight_through() -> None:
    """Voice mode is a WebSocket proxy; it must never meet a responder."""
    seen: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    middleware = StreamSafeGZipMiddleware(inner)
    scope: Scope = {"type": "websocket", "headers": [(b"accept-encoding", b"gzip")]}

    asyncio.run(_drive(middleware, scope))

    assert seen == [scope]


def test_starlette_exclusions_are_carried_forward() -> None:
    """Drift guard on the pinned Starlette.

    `text/event-stream` is excluded here *because* it is excluded upstream —
    if a version bump ever renames or empties that constant, this fails
    rather than silently un-excluding SSE.
    """
    assert "text/event-stream" in DEFAULT_EXCLUDED_CONTENT_TYPES
    assert set(DEFAULT_EXCLUDED_CONTENT_TYPES) <= set(EXCLUDED_CONTENT_TYPES)
