"""Amazon Transcribe Streaming over its WebSocket API — no SDK.

``boto3`` does not implement ``StartStreamTranscription`` (it is an HTTP/2
event-stream operation), and the Python streaming SDKs are separate packages.
The WebSocket flavour needs far less: a SigV4 *query*-presigned URL, and AWS
event-stream framing on each frame. Presigning is ``botocore.auth``; decoding
is ``botocore.eventstream``; the only thing botocore lacks is an event-stream
*encoder*, which is ``encode_audio_event`` below — one message shape, three
fixed string headers.

Unlike the HTTP/2 flavour, WebSocket audio frames carry no per-chunk
signature: the presigned upgrade is the whole authentication.

Reference: https://docs.aws.amazon.com/transcribe/latest/dg/streaming-setting-up.html
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import ReadOnlyCredentials
from botocore.eventstream import EventStreamBuffer

SAMPLE_RATE_HZ = 16000

# Hard ceiling set by the service for a presigned WebSocket URL. It bounds only
# the window to *open* the socket — an established stream runs on regardless.
_PRESIGN_EXPIRES_SECONDS = 300

_STRING_HEADER_TYPE = 7


@dataclass(frozen=True)
class TranscriptSegment:
    """One Transcribe result, reduced to what the composer needs.

    ``result_id`` is stable across the partial revisions of a segment, so the
    client replaces in place until ``is_partial`` goes False and then commits.
    """

    result_id: str
    text: str
    is_partial: bool
    language_code: Optional[str] = None


class TranscribeStreamError(Exception):
    """Transcribe sent an exception frame (bad request, limit exceeded, ...)."""

    def __init__(self, exception_type: str, message: str) -> None:
        super().__init__(f"{exception_type}: {message}")
        self.exception_type = exception_type
        self.message = message


def language_query_params(languages: list[str]) -> dict[str, str]:
    """Query parameters that select the transcription language.

    One language pins ``language-code``. Two or more switch on single-language
    identification — the service requires at least two options and rejects
    ``language-code`` alongside ``identify-language`` — with the first listed
    as ``preferred-language``, which the docs note speeds identification on
    short clips (dictation is mostly short clips).
    """
    if not languages:
        raise ValueError("at least one language is required")
    if len(languages) == 1:
        return {"language-code": languages[0]}
    return {
        "identify-language": "true",
        "language-options": ",".join(languages),
        "preferred-language": languages[0],
    }


def presign_stream_url(
    *,
    credentials: ReadOnlyCredentials,
    region: str,
    languages: list[str],
    sample_rate: int = SAMPLE_RATE_HZ,
) -> str:
    """Return a ``wss://`` URL that opens a PCM transcription stream."""
    params = {
        "media-encoding": "pcm",
        "sample-rate": str(sample_rate),
        # Stabilised partials stop earlier words flickering while the user is
        # still mid-sentence — the composer shows partials live.
        "enable-partial-results-stabilization": "true",
        "partial-results-stability": "medium",
        **language_query_params(languages),
    }
    url = (
        f"https://transcribestreaming.{region}.amazonaws.com:8443"
        f"/stream-transcription-websocket?{urlencode(params)}"
    )
    request = AWSRequest(method="GET", url=url)
    SigV4QueryAuth(credentials, "transcribe", region, expires=_PRESIGN_EXPIRES_SECONDS).add_auth(
        request
    )
    return "wss://" + request.url[len("https://"):]


def _encode_string_header(name: str, value: str) -> bytes:
    name_bytes = name.encode("utf-8")
    value_bytes = value.encode("utf-8")
    return (
        struct.pack(">B", len(name_bytes))
        + name_bytes
        + struct.pack(">BH", _STRING_HEADER_TYPE, len(value_bytes))
        + value_bytes
    )


_AUDIO_EVENT_HEADERS = b"".join(
    [
        _encode_string_header(":content-type", "application/octet-stream"),
        _encode_string_header(":event-type", "AudioEvent"),
        _encode_string_header(":message-type", "event"),
    ]
)


def encode_audio_event(pcm: bytes) -> bytes:
    """Frame raw PCM as an ``AudioEvent`` event-stream message.

    Layout: total length, headers length, prelude CRC, headers, payload,
    message CRC — all big-endian, CRC32 as in gzip. An empty ``pcm`` is the
    end-of-stream signal: Transcribe flushes its final results and closes.
    """
    total_length = 16 + len(_AUDIO_EVENT_HEADERS) + len(pcm)
    prelude = struct.pack(">II", total_length, len(_AUDIO_EVENT_HEADERS))
    prelude_crc = struct.pack(">I", zlib.crc32(prelude))
    body = prelude + prelude_crc + _AUDIO_EVENT_HEADERS + pcm
    return body + struct.pack(">I", zlib.crc32(body))


class TranscriptDecoder:
    """Turn Transcribe's binary frames into ``TranscriptSegment``s.

    Stateful because event-stream messages are not guaranteed to align with
    WebSocket frames; ``EventStreamBuffer`` reassembles across feeds.
    """

    def __init__(self) -> None:
        self._buffer = EventStreamBuffer()

    def feed(self, data: bytes) -> list[TranscriptSegment]:
        """Consume a frame; return the segments it completed.

        Raises ``TranscribeStreamError`` on an exception message.
        """
        self._buffer.add_data(data)
        segments: list[TranscriptSegment] = []
        for message in self._buffer:
            headers = message.headers
            message_type = headers.get(":message-type")
            if message_type == "exception":
                exception_type = str(headers.get(":exception-type") or "Exception")
                raise TranscribeStreamError(exception_type, _exception_message(message.payload))
            if message_type != "event" or headers.get(":event-type") != "TranscriptEvent":
                continue
            segments.extend(_segments_from_payload(message.payload))
        return segments


def _exception_message(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict):
        return str(parsed.get("Message") or parsed.get("message") or text)
    return text


def _segments_from_payload(payload: bytes) -> list[TranscriptSegment]:
    body = json.loads(payload)
    results = (body.get("Transcript") or {}).get("Results") or []
    segments: list[TranscriptSegment] = []
    for result in results:
        alternatives = result.get("Alternatives") or []
        if not alternatives:
            continue
        segments.append(
            TranscriptSegment(
                result_id=str(result.get("ResultId") or ""),
                text=str(alternatives[0].get("Transcript") or ""),
                is_partial=bool(result.get("IsPartial")),
                language_code=result.get("LanguageCode"),
            )
        )
    return segments
