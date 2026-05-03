"""Tests for the AES-GCM cookie codec.

Uses an injected `AESGCM` cipher to avoid mocking KMS — `CookieCodec` exposes
the `_cipher` attribute which we set directly. (Production callers always go
through `_ensure_cipher`, which is what the KMS-integration test exercises.)
"""

from __future__ import annotations

import base64
import os
import secrets

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apis.shared.sessions_bff.cookie import (
    CookieCodec,
    CookieDecodeError,
    _reset_default_codec_for_tests,
    _set_default_codec_for_tests,
    get_default_codec,
)
from apis.shared.sessions_bff.models import CookiePayload


def _codec_with_cipher() -> CookieCodec:
    codec = CookieCodec(kms_key_arn="arn:aws:kms:fake")
    codec._cipher = AESGCM(secrets.token_bytes(32))
    return codec


def test_seal_and_unseal_round_trip() -> None:
    codec = _codec_with_cipher()
    payload = CookiePayload(session_id="sess-abc-123", version=1)

    sealed = codec.seal(payload)
    decoded = codec.unseal(sealed)

    assert decoded.session_id == payload.session_id
    assert decoded.version == 1


def test_seal_produces_distinct_ciphertexts_each_call() -> None:
    """Nonce is random — two seals of the same payload must differ."""
    codec = _codec_with_cipher()
    payload = CookiePayload(session_id="sess-abc-123")
    assert codec.seal(payload) != codec.seal(payload)


def test_unseal_rejects_tampered_ciphertext() -> None:
    codec = _codec_with_cipher()
    sealed = codec.seal(CookiePayload(session_id="sess-x"))

    # Flip a byte deep in the ciphertext.
    raw = bytearray(base64.urlsafe_b64decode(sealed + "=" * (-len(sealed) % 4)))
    raw[-1] ^= 0x01
    tampered = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")

    with pytest.raises(CookieDecodeError):
        codec.unseal(tampered)


def test_unseal_rejects_garbage() -> None:
    codec = _codec_with_cipher()
    with pytest.raises(CookieDecodeError):
        codec.unseal("not-a-real-cookie-value")


def test_unseal_rejects_unknown_version() -> None:
    codec = _codec_with_cipher()
    cipher = codec._cipher
    nonce = secrets.token_bytes(12)
    ciphertext = cipher.encrypt(nonce, b'{"sid":"x"}', None)
    # Version byte = 99 (unknown).
    blob = bytes([99]) + nonce + ciphertext
    sealed = base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")
    with pytest.raises(CookieDecodeError):
        codec.unseal(sealed)


def test_unseal_rejects_missing_session_id() -> None:
    codec = _codec_with_cipher()
    cipher = codec._cipher
    nonce = secrets.token_bytes(12)
    # Valid version, AAD matches what unseal expects, but JSON has no `sid`.
    ciphertext = cipher.encrypt(nonce, b'{"v":1}', bytes([1]))
    blob = bytes([1]) + nonce + ciphertext
    sealed = base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")
    with pytest.raises(CookieDecodeError):
        codec.unseal(sealed)


def test_unseal_with_wrong_key_fails() -> None:
    codec_a = _codec_with_cipher()
    codec_b = _codec_with_cipher()  # different random key

    sealed = codec_a.seal(CookiePayload(session_id="sess-x"))
    with pytest.raises(CookieDecodeError):
        codec_b.unseal(sealed)


def test_seal_preserves_extras() -> None:
    codec = _codec_with_cipher()
    payload = CookiePayload(session_id="s", extras={"hint": "abc"})
    decoded = codec.unseal(codec.seal(payload))
    assert decoded.extras == {"hint": "abc"}


def test_default_codec_is_a_singleton() -> None:
    """The auth/callback route seals with this codec and the
    `SessionRefreshMiddleware` unseals with it on the next request — they
    must be the *same* instance, since each `CookieCodec` derives its own
    random AES key. A second instance would fail every unseal as 'bad seal'.
    """
    _reset_default_codec_for_tests()
    try:
        os.environ["BFF_COOKIE_SIGNING_KEY_ARN"] = "arn:aws:kms:fake"
        first = get_default_codec()
        second = get_default_codec()
        assert first is second
    finally:
        os.environ.pop("BFF_COOKIE_SIGNING_KEY_ARN", None)
        _reset_default_codec_for_tests()


def test_default_codec_round_trip_seals_and_unseals() -> None:
    """The bug we're guarding against: seal in one call site, unseal in
    another, both via the singleton — must succeed."""
    _reset_default_codec_for_tests()
    try:
        injected = _codec_with_cipher()
        _set_default_codec_for_tests(injected)

        sealing_codec = get_default_codec()
        unsealing_codec = get_default_codec()

        sealed = sealing_codec.seal(CookiePayload(session_id="sess-singleton"))
        decoded = unsealing_codec.unseal(sealed)
        assert decoded.session_id == "sess-singleton"
    finally:
        _reset_default_codec_for_tests()


def test_unseal_propagates_kms_infrastructure_errors() -> None:
    """KMS unavailable is not a decode error — it must surface so the caller
    can return 5xx instead of clearing the cookie and forcing re-login."""
    from unittest.mock import MagicMock

    fake_kms = MagicMock()
    fake_kms.generate_data_key.side_effect = RuntimeError("KMS unreachable")

    codec = CookieCodec(kms_key_arn="arn:aws:kms:fake", kms_client=fake_kms)
    with pytest.raises(RuntimeError, match="KMS unreachable"):
        codec.unseal("doesnt-matter")
