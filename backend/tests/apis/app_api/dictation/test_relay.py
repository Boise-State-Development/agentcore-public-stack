"""Relay tests against a fake Transcribe WebSocket server.

The fake decodes the relay's AudioEvents with botocore's parser, so these also
prove the framing on the wire, and replies with scripted TranscriptEvents.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

import pytest
from aiohttp import WSMsgType, web
from botocore.eventstream import EventStreamBuffer
from starlette.websockets import WebSocketState

from apis.app_api.dictation.relay import relay_dictation_stream
from tests.apis.app_api.dictation.test_transcribe import _encode_message, _transcript_frame


class FakeClient:
    """The SPA side: scripted inbound messages, recorded outbound JSON."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[dict] = asyncio.Queue()
        self.sent: list[dict] = []
        self.application_state = WebSocketState.CONNECTED

    async def receive(self) -> dict:
        return await self.inbox.get()

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def audio(self, pcm: bytes) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": pcm})

    def stop(self) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "stop"})})

    def disconnect(self) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == kind]


Handler = Callable[[web.WebSocketResponse, list[bytes]], Awaitable[None]]


async def _serve(aiohttp_handler: Handler) -> tuple[web.AppRunner, str, list[bytes]]:
    """Start a fake Transcribe; return (runner, url, audio payloads received)."""
    received: list[bytes] = []

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await aiohttp_handler(ws, received)
        return ws

    app = web.Application()
    app.router.add_get("/stream", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"ws://127.0.0.1:{port}/stream", received


async def _read_audio_until_end(ws: web.WebSocketResponse, received: list[bytes]) -> None:
    buffer = EventStreamBuffer()
    async for msg in ws:
        if msg.type != WSMsgType.BINARY:
            continue
        buffer.add_data(msg.data)
        for message in buffer:
            assert message.headers[":event-type"] == "AudioEvent"
            received.append(message.payload)
            if message.payload == b"":
                return


async def _run(client: FakeClient, url: str, max_seconds: float = 300.0) -> None:
    await asyncio.wait_for(
        relay_dictation_stream(client_ws=client, upstream_url=url, user_id="u1", max_seconds=max_seconds),
        timeout=5,
    )


@pytest.mark.asyncio
async def test_done_flushes_tail_and_reports_stopped() -> None:
    async def transcribe(ws: web.WebSocketResponse, received: list[bytes]) -> None:
        await ws.send_bytes(
            _transcript_frame([{"ResultId": "a", "IsPartial": True, "Alternatives": [{"Transcript": "Hello"}]}])
        )
        await _read_audio_until_end(ws, received)
        await ws.send_bytes(
            _transcript_frame(
                [{"ResultId": "a", "IsPartial": False, "Alternatives": [{"Transcript": "Hello there."}]}]
            )
        )
        await ws.close()

    runner, url, received = await _serve(transcribe)
    try:
        client = FakeClient()
        client.audio(b"\x01\x00" * 1600)
        client.stop()
        await _run(client, url)
    finally:
        await runner.cleanup()

    assert client.sent[0] == {"type": "ready"}
    assert received == [b"\x01\x00" * 1600, b""]
    transcripts = client.of_type("transcript")
    assert [(t["text"], t["partial"]) for t in transcripts] == [("Hello", True), ("Hello there.", False)]
    assert client.sent[-1] == {"type": "done", "reason": "stopped"}


@pytest.mark.asyncio
async def test_trailing_partial_is_promoted_to_final() -> None:
    """Language-ID streams can close on a short clip with no final result."""

    async def transcribe(ws: web.WebSocketResponse, received: list[bytes]) -> None:
        await _read_audio_until_end(ws, received)
        await ws.send_bytes(
            _transcript_frame(
                [{"ResultId": "z", "IsPartial": True, "LanguageCode": "en-US", "Alternatives": [{"Transcript": "Fix the bug."}]}]
            )
        )
        await ws.close()

    runner, url, _ = await _serve(transcribe)
    try:
        client = FakeClient()
        client.audio(b"\x00\x00" * 800)
        client.stop()
        await _run(client, url)
    finally:
        await runner.cleanup()

    transcripts = client.of_type("transcript")
    assert transcripts[-1] == {
        "type": "transcript",
        "id": "z",
        "text": "Fix the bug.",
        "partial": False,
        "language": "en-US",
    }
    assert client.sent[-1] == {"type": "done", "reason": "stopped"}


@pytest.mark.asyncio
async def test_client_disconnect_tears_down_without_done() -> None:
    closed = asyncio.Event()

    async def transcribe(ws: web.WebSocketResponse, received: list[bytes]) -> None:
        async for _ in ws:
            pass
        closed.set()

    runner, url, _ = await _serve(transcribe)
    try:
        client = FakeClient()
        client.audio(b"\x00\x00" * 1600)
        client.disconnect()
        await _run(client, url)
        await asyncio.wait_for(closed.wait(), timeout=2)
    finally:
        await runner.cleanup()

    assert client.of_type("done") == []
    assert client.of_type("error") == []


@pytest.mark.asyncio
async def test_time_limit_ends_stream_with_limit_reason() -> None:
    async def transcribe(ws: web.WebSocketResponse, received: list[bytes]) -> None:
        await _read_audio_until_end(ws, received)
        await ws.close()

    runner, url, received = await _serve(transcribe)
    try:
        client = FakeClient()
        # 0.15s allowed = 4,800 bytes; send 3,200 + 3,200 — the second is truncated.
        client.audio(b"\x00\x00" * 1600)
        client.audio(b"\x00\x00" * 1600)
        await _run(client, url, max_seconds=0.15)
    finally:
        await runner.cleanup()

    assert [len(p) for p in received] == [3200, 1600, 0]
    assert client.sent[-1] == {"type": "done", "reason": "limit"}


@pytest.mark.asyncio
async def test_limit_exceeded_maps_to_busy_error() -> None:
    async def transcribe(ws: web.WebSocketResponse, received: list[bytes]) -> None:
        await ws.send_bytes(
            _encode_message(
                {":message-type": "exception", ":exception-type": "LimitExceededException"},
                b'{"Message":"too many"}',
            )
        )
        await ws.close()

    runner, url, _ = await _serve(transcribe)
    try:
        client = FakeClient()
        await _run(client, url)
    finally:
        await runner.cleanup()

    (error,) = client.of_type("error")
    assert error["code"] == "busy"
    assert client.of_type("done") == []


@pytest.mark.asyncio
async def test_unreachable_upstream_reports_unavailable() -> None:
    client = FakeClient()
    await _run(client, "ws://127.0.0.1:9/unreachable")
    assert client.sent == [
        {"type": "error", "code": "unavailable", "message": client.sent[0]["message"]}
    ]


@pytest.mark.asyncio
async def test_oversized_client_frame_is_refused() -> None:
    async def transcribe(ws: web.WebSocketResponse, received: list[bytes]) -> None:
        async for _ in ws:
            pass

    runner, url, received = await _serve(transcribe)
    try:
        client = FakeClient()
        client.audio(b"\x00" * (64 * 1024 + 1))
        await _run(client, url)
    finally:
        await runner.cleanup()

    assert received == []
    assert client.of_type("error")[0]["code"] == "bad_audio"
