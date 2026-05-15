"""Tests for the voice-ticket HMAC codec."""

from __future__ import annotations

import base64
import json
import time

import pytest

from apis.shared.voice_ticket.codec import VoiceTicketCodec, VoiceTicketError


SIGNING_KEY = b"k" * 64


def _make_codec() -> VoiceTicketCodec:
    return VoiceTicketCodec(SIGNING_KEY)


def test_issue_then_verify_roundtrip() -> None:
    codec = _make_codec()
    ticket, claims = codec.issue(user_id="user-1", session_id="sess-A", ttl_seconds=60)
    verified = codec.verify(ticket)
    assert verified.user_id == "user-1"
    assert verified.session_id == "sess-A"
    assert verified.jti == claims.jti
    assert verified.exp - verified.iat == 60


def test_issue_rejects_empty_inputs() -> None:
    codec = _make_codec()
    with pytest.raises(ValueError):
        codec.issue(user_id="", session_id="sess")
    with pytest.raises(ValueError):
        codec.issue(user_id="user", session_id="")


def test_construct_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        VoiceTicketCodec(b"")


def test_verify_rejects_tampered_payload() -> None:
    codec = _make_codec()
    ticket, _ = codec.issue(user_id="user-1", session_id="sess-A")
    body, sig = ticket.split(".", 1)

    # Re-encode the payload with a different sub but keep the original sig.
    decoded = json.loads(_b64url_decode_to_bytes(body))
    decoded["sub"] = "attacker"
    tampered_body = _b64url_encode(json.dumps(decoded, separators=(",", ":")).encode("utf-8"))
    tampered_ticket = f"{tampered_body}.{sig}"

    with pytest.raises(VoiceTicketError):
        codec.verify(tampered_ticket)


def test_verify_rejects_signature_signed_with_other_key() -> None:
    codec = _make_codec()
    other = VoiceTicketCodec(b"x" * 64)
    ticket, _ = other.issue(user_id="user-1", session_id="sess-A")
    with pytest.raises(VoiceTicketError):
        codec.verify(ticket)


def test_verify_rejects_expired_ticket() -> None:
    codec = _make_codec()
    issued_at = int(time.time()) - 120
    ticket, _ = codec.issue(user_id="user-1", session_id="sess-A", ttl_seconds=60, now=issued_at)
    with pytest.raises(VoiceTicketError):
        codec.verify(ticket)


def test_verify_rejects_malformed_ticket() -> None:
    codec = _make_codec()
    for bad in ["", ".", "abc", "abc.", ".abc", "no-dot-here"]:
        with pytest.raises(VoiceTicketError):
            codec.verify(bad)


def test_verify_rejects_unsupported_version() -> None:
    codec = _make_codec()
    ticket, _ = codec.issue(user_id="user-1", session_id="sess-A")
    body, _, _ = ticket.partition(".")
    decoded = json.loads(_b64url_decode_to_bytes(body))
    decoded["v"] = 99
    bad_body = _b64url_encode(json.dumps(decoded, separators=(",", ":")).encode("utf-8"))
    # Re-sign with our key so signature passes; the version check should still fail.
    sig = _b64url_encode(_hmac(SIGNING_KEY, bad_body.encode("ascii")))
    with pytest.raises(VoiceTicketError):
        codec.verify(f"{bad_body}.{sig}")


# --- helpers ----------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode_to_bytes(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _hmac(key: bytes, body: bytes) -> bytes:
    import hashlib
    import hmac

    return hmac.new(key, body, hashlib.sha256).digest()
