"""Tests for the hand-rolled Transcribe Streaming WebSocket codec.

The encoder is checked against botocore's own event-stream *decoder* — the
same parser the service's framing is specified by — and was validated live
against us-west-2 before these were written.
"""

from __future__ import annotations

import json
import struct
import zlib
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.credentials import ReadOnlyCredentials
from botocore.eventstream import EventStreamBuffer

from apis.app_api.dictation.transcribe import (
    TranscribeStreamError,
    TranscriptDecoder,
    encode_audio_event,
    language_query_params,
    presign_stream_url,
)

CREDS = ReadOnlyCredentials("AKIDEXAMPLE", "secret", "session-token")


def _encode_message(headers: dict[str, str], payload: bytes) -> bytes:
    """Generic event-stream encoder for fabricating *server* frames in tests."""
    encoded = b""
    for name, value in headers.items():
        n, v = name.encode(), value.encode()
        encoded += struct.pack(">B", len(n)) + n + struct.pack(">BH", 7, len(v)) + v
    prelude = struct.pack(">II", 16 + len(encoded) + len(payload), len(encoded))
    body = prelude + struct.pack(">I", zlib.crc32(prelude)) + encoded + payload
    return body + struct.pack(">I", zlib.crc32(body))


def _transcript_frame(results: list[dict]) -> bytes:
    return _encode_message(
        {":message-type": "event", ":event-type": "TranscriptEvent", ":content-type": "application/json"},
        json.dumps({"Transcript": {"Results": results}}).encode(),
    )


def test_audio_event_decodes_with_botocore() -> None:
    pcm = bytes(range(256)) * 10
    buffer = EventStreamBuffer()
    buffer.add_data(encode_audio_event(pcm))
    (message,) = list(buffer)
    assert message.headers == {
        ":content-type": "application/octet-stream",
        ":event-type": "AudioEvent",
        ":message-type": "event",
    }
    assert message.payload == pcm


def test_empty_audio_event_is_a_valid_end_of_stream_frame() -> None:
    buffer = EventStreamBuffer()
    buffer.add_data(encode_audio_event(b""))
    (message,) = list(buffer)
    assert message.payload == b""


def test_single_language_pins_language_code() -> None:
    assert language_query_params(["en-US"]) == {"language-code": "en-US"}


def test_multiple_languages_enable_identification_with_first_preferred() -> None:
    params = language_query_params(["en-US", "es-US"])
    assert params == {
        "identify-language": "true",
        "language-options": "en-US,es-US",
        "preferred-language": "en-US",
    }
    # The service rejects language-code alongside identify-language.
    assert "language-code" not in params


def test_language_list_must_not_be_empty() -> None:
    with pytest.raises(ValueError):
        language_query_params([])


def test_presigned_url_shape() -> None:
    url = presign_stream_url(credentials=CREDS, region="us-west-2", languages=["en-US"])
    parts = urlsplit(url)
    assert parts.scheme == "wss"
    assert parts.netloc == "transcribestreaming.us-west-2.amazonaws.com:8443"
    assert parts.path == "/stream-transcription-websocket"
    query = parse_qs(parts.query)
    assert query["media-encoding"] == ["pcm"]
    assert query["sample-rate"] == ["16000"]
    assert query["language-code"] == ["en-US"]
    assert query["X-Amz-Expires"] == ["300"]
    assert query["X-Amz-Security-Token"] == ["session-token"]
    assert query["X-Amz-Credential"][0].endswith("/us-west-2/transcribe/aws4_request")
    assert "X-Amz-Signature" in query


def test_decoder_reassembles_a_message_split_across_frames() -> None:
    frame = _transcript_frame(
        [{"ResultId": "r1", "IsPartial": True, "Alternatives": [{"Transcript": "Hello"}]}]
    )
    decoder = TranscriptDecoder()
    assert decoder.feed(frame[:10]) == []
    (segment,) = decoder.feed(frame[10:])
    assert (segment.result_id, segment.text, segment.is_partial) == ("r1", "Hello", True)


def test_decoder_skips_results_without_alternatives_and_reads_language() -> None:
    frame = _transcript_frame(
        [
            {"ResultId": "r0", "IsPartial": True, "Alternatives": []},
            {
                "ResultId": "r1",
                "IsPartial": False,
                "LanguageCode": "es-US",
                "Alternatives": [{"Transcript": "Hola."}],
            },
        ]
    )
    (segment,) = TranscriptDecoder().feed(frame)
    assert segment.text == "Hola."
    assert segment.is_partial is False
    assert segment.language_code == "es-US"


def test_decoder_raises_on_exception_frame() -> None:
    frame = _encode_message(
        {":message-type": "exception", ":exception-type": "LimitExceededException"},
        json.dumps({"Message": "too many streams"}).encode(),
    )
    with pytest.raises(TranscribeStreamError) as info:
        TranscriptDecoder().feed(frame)
    assert info.value.exception_type == "LimitExceededException"
    assert info.value.message == "too many streams"
