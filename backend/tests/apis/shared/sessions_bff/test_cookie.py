"""Tests for the AES-GCM cookie codec.

Two layers of coverage:

  1. Round-trip / decode tests — use an injected `AESGCM` cipher (set on
     `_cipher` directly) so we don't need to mock Secrets Manager.
  2. `_ensure_cipher` path — exercises the deploy-time-bootstrapped data
     key flow (`secretsmanager:GetSecretValue` -> SHA-256 -> AESGCM cipher)
     with mock clients. This is the path that runs in production every
     time a task starts.

The cross-task seal/unseal regression — a cookie sealed by one process
unsealing on a *different* process — is locked in by
`test_two_codecs_with_same_secret_derive_the_same_cipher`.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apis.shared.sessions_bff.cookie import (
    CookieCodec,
    CookieDataKeyUnavailable,
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
    must be the *same* instance within a process so we don't refetch the
    data-key secret on every cookie operation.

    Cross-process consistency (Task A's seal unsealing on Task B) is locked
    in by `test_two_codecs_with_same_secret_derive_the_same_cipher`.
    """
    _reset_default_codec_for_tests()
    try:
        os.environ["BFF_COOKIE_SIGNING_KEY_ARN"] = "arn:aws:kms:fake"
        os.environ["BFF_COOKIE_DATA_KEY_SECRET_ARN"] = (
            "arn:aws:secretsmanager:us-east-1:0:secret:bff-data-key"
        )
        first = get_default_codec()
        second = get_default_codec()
        assert first is second
    finally:
        os.environ.pop("BFF_COOKIE_SIGNING_KEY_ARN", None)
        os.environ.pop("BFF_COOKIE_DATA_KEY_SECRET_ARN", None)
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


# =====================================================================
# `_ensure_cipher` — Secrets Manager fetch + SHA-256 derivation path.
# =====================================================================

KMS_KEY_ARN = "arn:aws:kms:us-east-1:0:key/test"
DATA_KEY_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:0:secret:bff-data-key"


def _make_sm_mock(secret_string: str) -> MagicMock:
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": secret_string}
    return sm


def test_ensure_cipher_fetches_secret_and_derives_key() -> None:
    """Happy path: codec fetches the secret from Secrets Manager, derives
    a 32-byte AES-256 key with SHA-256, then seals/unseals successfully."""
    secret_string = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKL012345"  # 44 chars
    sm = _make_sm_mock(secret_string)

    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        secrets_manager_client=sm,
    )
    sealed = codec.seal(CookiePayload(session_id="sess-bootstrapped"))
    assert codec.unseal(sealed).session_id == "sess-bootstrapped"

    sm.get_secret_value.assert_called_once_with(SecretId=DATA_KEY_SECRET_ARN)


def test_ensure_cipher_derived_key_matches_sha256_of_secret() -> None:
    """Lock the KDF: a future change must keep the same derivation, or
    every cookie sealed by an old task fails to unseal on a new task
    after deploy."""
    secret_string = "deterministic-secret-for-kdf-pinning-test-1234"
    sm = _make_sm_mock(secret_string)

    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        secrets_manager_client=sm,
    )
    # Force initialization without exposing _cipher's key directly: use a
    # parallel cipher with the expected key, encrypt, and decrypt with the
    # codec. If the codec didn't derive via SHA-256, decrypt fails.
    codec.seal(CookiePayload(session_id="x"))
    expected_key = hashlib.sha256(secret_string.encode("utf-8")).digest()
    expected_cipher = AESGCM(expected_key)
    nonce = secrets.token_bytes(12)
    ciphertext = expected_cipher.encrypt(nonce, b'{"sid":"y"}', bytes([1]))
    blob = bytes([1]) + nonce + ciphertext
    sealed = base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")
    decoded = codec.unseal(sealed)
    assert decoded.session_id == "y"


def test_ensure_cipher_caches_after_first_call() -> None:
    """Hot-path requirement: only one Secrets Manager call per process."""
    sm = _make_sm_mock("a" * 44)
    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        secrets_manager_client=sm,
    )
    for _ in range(5):
        codec.seal(CookiePayload(session_id="x"))
    assert sm.get_secret_value.call_count == 1


def test_two_codecs_with_same_secret_derive_the_same_cipher() -> None:
    """Regression lock for the dev `bad seal` 401 storm.

    Two independent `CookieCodec` instances simulate two ECS tasks. Both
    fetch the SAME secret string from Secrets Manager and derive the same
    32-byte key via SHA-256. A cookie sealed on `task_a` MUST unseal on
    `task_b`. Pre-fix, each task generated its own random data key and
    this failed.
    """
    secret_string = "shared-secret-across-tasks-1234567890ABCDEFGH"
    sm_a = _make_sm_mock(secret_string)
    sm_b = _make_sm_mock(secret_string)

    task_a = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        secrets_manager_client=sm_a,
    )
    task_b = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        secrets_manager_client=sm_b,
    )

    sealed_on_a = task_a.seal(CookiePayload(session_id="sess-cross-task"))
    decoded_on_b = task_b.unseal(sealed_on_a)
    assert decoded_on_b.session_id == "sess-cross-task"


def test_ensure_cipher_propagates_secrets_manager_failure() -> None:
    """Secrets Manager unreachable must surface as `CookieDataKeyUnavailable`
    so the request returns 5xx — never as a decode error that clears the
    user's cookie."""
    sm = MagicMock()
    sm.get_secret_value.side_effect = RuntimeError("Secrets Manager unreachable")
    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        secrets_manager_client=sm,
    )
    with pytest.raises(CookieDataKeyUnavailable):
        codec.unseal("anything")


def test_ensure_cipher_rejects_empty_secret_string() -> None:
    """Bootstrap not yet completed (or secret manually wiped) — fail loud
    rather than silently invalidate every active session."""
    sm = _make_sm_mock("")
    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        secrets_manager_client=sm,
    )
    with pytest.raises(CookieDataKeyUnavailable, match="bootstrap missing"):
        codec.unseal("anything")


def test_ensure_cipher_missing_config_surfaces_as_decode_error() -> None:
    """No KMS ARN or no secret ARN — same shape as today's "BFF disabled"
    path. Treated as `bad seal` so the middleware clears the cookie."""
    codec = CookieCodec(kms_key_arn="", data_key_secret_arn="")
    with pytest.raises(CookieDecodeError):
        codec.unseal("anything")
