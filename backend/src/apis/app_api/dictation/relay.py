"""Relay between the SPA's dictation socket and Transcribe Streaming.

Wire protocol with the SPA — deliberately smaller than Transcribe's, so the
browser needs no event-stream codec:

    Client → server
        binary frame            raw PCM16LE, 16 kHz mono (the voice recorder's format)
        {"type": "stop"}        user pressed Done: flush the tail, then finish

    Server → client
        {"type": "ready"}                                   upstream is open; start sending audio
        {"type": "transcript", "id", "text", "partial", "language"}
        {"type": "error", "code", "message"}                terminal
        {"type": "done", "reason": "stopped" | "limit"}     terminal; nothing follows

Cancelling is closing the socket: the transcript is thrown away client-side,
so the relay just tears the upstream down.

**Trailing partials are promoted to final.** With language identification on,
Transcribe was observed (live, us-west-2, 2026-09-23) to close the stream after
a short clip with only partial results — "Fix the bug." never received an
``IsPartial: false``. Pinned-language streams finalize the same clip. So on an
orderly close, any segment whose last word was a partial is re-sent as final;
without that, the SPA would sit on text it may never commit.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import aiohttp
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .transcribe import (
    SAMPLE_RATE_HZ,
    TranscribeStreamError,
    TranscriptDecoder,
    TranscriptSegment,
    encode_audio_event,
)

logger = logging.getLogger(__name__)

_UPSTREAM_CONNECT_TIMEOUT = 10.0

# How long Done waits for Transcribe to flush the tail after the end-of-stream
# frame. The final segment typically lands well inside a second.
_FLUSH_TIMEOUT_SECONDS = 10.0

# PCM16 mono: two bytes per sample.
_BYTES_PER_SECOND = SAMPLE_RATE_HZ * 2

# The recorder sends 100ms (3,200-byte) chunks. Anything far larger is not our
# SPA; refuse it rather than forward an arbitrarily large frame upstream.
_MAX_CLIENT_FRAME_BYTES = 64 * 1024

# Stable codes the SPA branches on; the message is what it shows.
_ERROR_MESSAGES = {
    "unavailable": "Dictation is unavailable right now. Please try again.",
    "busy": "Too many people are dictating right now. Please try again in a moment.",
    "bad_audio": "Dictation couldn't read the microphone audio.",
}


async def relay_dictation_stream(
    *,
    client_ws: WebSocket,
    upstream_url: str,
    user_id: str,
    max_seconds: float,
) -> None:
    """Open the Transcribe socket and relay until Done, cancel, limit or error.

    Caller must have accepted ``client_ws`` and is responsible for closing it.
    """
    timeout = aiohttp.ClientTimeout(total=None, connect=_UPSTREAM_CONNECT_TIMEOUT)
    state = {"audio_bytes": 0}
    outcome = "cancelled"
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            upstream = await session.ws_connect(upstream_url, max_msg_size=0)
        except Exception as exc:
            logger.error("Dictation upstream connect failed: %s", exc)
            await _send_error(client_ws, "unavailable")
            return

        try:
            await _send_json(client_ws, {"type": "ready"})
            pending: dict[str, TranscriptSegment] = {}
            to_client = asyncio.create_task(_pump_upstream_to_client(upstream, client_ws, pending))
            to_upstream = asyncio.create_task(
                _pump_client_to_upstream(client_ws, upstream, state, max_seconds)
            )

            done, _ = await asyncio.wait(
                [to_client, to_upstream], return_when=asyncio.FIRST_COMPLETED
            )

            if to_upstream in done:
                outcome = _task_result(to_upstream) or "cancelled"
                if outcome == "cancelled":
                    await _cancel(to_client)
                    return
                # Done or the time limit: the end-of-stream frame is sent, so
                # let Transcribe flush the tail before reporting completion.
                try:
                    await asyncio.wait_for(asyncio.shield(to_client), _FLUSH_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    logger.warning("Dictation upstream did not close within flush timeout")
                    await _cancel(to_client)
                upstream_error = _task_result(to_client) if to_client.done() else None
            else:
                # Upstream ended first: an exception frame, or the service
                # closed the stream on its own. Either way the client is done.
                await _cancel(to_upstream)
                upstream_error = _task_result(to_client)
                outcome = "error" if upstream_error else "stopped"

            await _flush_pending(client_ws, pending)
            if upstream_error:
                await _send_error(client_ws, upstream_error)
            else:
                await _send_json(client_ws, {"type": "done", "reason": outcome})
        finally:
            if not upstream.closed:
                try:
                    await upstream.close()
                except Exception as exc:
                    logger.debug("Ignoring error closing dictation upstream: %s", exc)
            # One line per stream is the metering record: Transcribe bills by
            # audio duration, and nothing else in the platform sees these minutes.
            logger.info(
                "Dictation stream ended: user=%s audio_seconds=%.1f outcome=%s",
                user_id,
                state["audio_bytes"] / _BYTES_PER_SECOND,
                outcome,
            )


async def _pump_client_to_upstream(
    client_ws: WebSocket,
    upstream: aiohttp.ClientWebSocketResponse,
    state: dict,
    max_seconds: float,
) -> str:
    """Forward audio; return ``"stopped"``, ``"limit"`` or ``"cancelled"``."""
    max_bytes = int(max_seconds * _BYTES_PER_SECOND)
    try:
        while True:
            message = await client_ws.receive()
            if message.get("type") == "websocket.disconnect":
                return "cancelled"

            pcm = message.get("bytes")
            if pcm is not None:
                if len(pcm) > _MAX_CLIENT_FRAME_BYTES:
                    await _send_error(client_ws, "bad_audio")
                    return "cancelled"
                if not pcm:
                    # An empty AudioEvent is Transcribe's end-of-stream; a
                    # stray empty frame must not end the user's dictation.
                    continue
                remaining = max_bytes - state["audio_bytes"]
                pcm = pcm[:remaining]
                state["audio_bytes"] += len(pcm)
                await upstream.send_bytes(encode_audio_event(pcm))
                if state["audio_bytes"] >= max_bytes:
                    await upstream.send_bytes(encode_audio_event(b""))
                    return "limit"
                continue

            text = message.get("text")
            if text is not None and _is_stop(text):
                await upstream.send_bytes(encode_audio_event(b""))
                return "stopped"
    except WebSocketDisconnect:
        return "cancelled"


async def _pump_upstream_to_client(
    upstream: aiohttp.ClientWebSocketResponse,
    client_ws: WebSocket,
    pending: dict[str, TranscriptSegment],
) -> Optional[str]:
    """Forward transcripts until the upstream closes; return an error code or None."""
    decoder = TranscriptDecoder()
    async for msg in upstream:
        if msg.type == aiohttp.WSMsgType.BINARY:
            try:
                segments = decoder.feed(msg.data)
            except TranscribeStreamError as exc:
                logger.warning("Transcribe stream error: %s", exc)
                return "busy" if exc.exception_type == "LimitExceededException" else "unavailable"
            for segment in segments:
                if segment.is_partial:
                    pending[segment.result_id] = segment
                else:
                    pending.pop(segment.result_id, None)
                await _send_segment(client_ws, segment)
        elif msg.type == aiohttp.WSMsgType.ERROR:
            logger.warning("Dictation upstream WS error: %s", upstream.exception())
            return "unavailable"
    return None


async def _flush_pending(client_ws: WebSocket, pending: dict[str, TranscriptSegment]) -> None:
    """Re-send every segment still awaiting its final as final (see module doc)."""
    for segment in list(pending.values()):
        await _send_segment(
            client_ws,
            TranscriptSegment(
                result_id=segment.result_id,
                text=segment.text,
                is_partial=False,
                language_code=segment.language_code,
            ),
        )
    pending.clear()


def _is_stop(text: str) -> bool:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and parsed.get("type") == "stop"


def _task_result(task: asyncio.Task) -> Optional[str]:
    if task.cancelled():
        return None
    exc = task.exception()
    if exc is not None:
        if not isinstance(exc, WebSocketDisconnect):
            logger.warning("Dictation relay task failed: %s", exc)
        return None
    return task.result()


async def _cancel(task: asyncio.Task) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        # Expected after an explicit cancel; nothing is waiting on the result.
        pass


async def _send_segment(client_ws: WebSocket, segment: TranscriptSegment) -> None:
    await _send_json(
        client_ws,
        {
            "type": "transcript",
            "id": segment.result_id,
            "text": segment.text,
            "partial": segment.is_partial,
            "language": segment.language_code,
        },
    )


async def _send_error(client_ws: WebSocket, code: str) -> None:
    await _send_json(
        client_ws,
        {"type": "error", "code": code, "message": _ERROR_MESSAGES.get(code, _ERROR_MESSAGES["unavailable"])},
    )


async def _send_json(client_ws: WebSocket, payload: dict) -> None:
    """Best-effort send — the SPA may already have closed (that is how it cancels)."""
    if client_ws.application_state != WebSocketState.CONNECTED:
        return
    try:
        await client_ws.send_json(payload)
    except Exception as exc:
        logger.debug("Dictation client send failed: %s", exc)
