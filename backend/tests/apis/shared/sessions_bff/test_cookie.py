"""Tests for the AES-GCM cookie codec.

Two layers of coverage:

  1. Round-trip / decode tests — use an injected `AESGCM` cipher (set on
     `_cipher` directly) so we don't need to mock Secrets Manager or KMS.
  2. `_ensure_cipher` path — exercises the deploy-time-bootstrapped wrapped
     data key flow (`secretsmanager:GetSecretValue` ->
     `kms:Decrypt(KeyId=...)` -> AESGCM cipher) with mock clients. This is
     the path that runs in production every time a task starts.

The cross-task seal/unseal regression — a cookie sealed by one process
unsealing on a *different* process — is locked in by
`test_two_codecs_with_same_wrapped_blob_decrypt_to_the_same_cipher`.
"""

from __future__ import annotations

import base64
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
    wrapped data key from Secrets Manager + KMS on every cookie operation.

    Cross-process consistency (Task A's seal unsealing on Task B) is locked
    in by `test_two_codecs_with_same_wrapped_blob_decrypt_to_the_same_cipher`.
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
# `_ensure_cipher` — wrapped data key fetch + KMS unwrap path.
# =====================================================================

KMS_KEY_ARN = "arn:aws:kms:us-east-1:0:key/test"
DATA_KEY_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:0:secret:bff-data-key"


def _wrap_plaintext_key_for_test(plaintext: bytes) -> tuple[bytes, str]:
    """Stand-in for `kms:GenerateDataKey`'s ciphertext — opaque bytes in,
    base64 string out (matches what CDK's AwsCustomResource stores in
    Secrets Manager)."""
    fake_wrapped = b"fake-wrapped:" + plaintext
    return fake_wrapped, base64.b64encode(fake_wrapped).decode("ascii")


def _make_mocks_for(plaintext_key: bytes) -> tuple[MagicMock, MagicMock, str]:
    fake_wrapped, b64 = _wrap_plaintext_key_for_test(plaintext_key)
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": b64}
    kms = MagicMock()
    kms.decrypt.return_value = {"Plaintext": plaintext_key}
    return sm, kms, b64


def test_ensure_cipher_fetches_wrapped_blob_and_unwraps() -> None:
    """Happy path: codec fetches the wrapped blob from Secrets Manager and
    unwraps it via KMS, then seals/unseals successfully."""
    plaintext = secrets.token_bytes(32)
    sm, kms, _ = _make_mocks_for(plaintext)

    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        kms_client=kms,
        secrets_manager_client=sm,
    )
    sealed = codec.seal(CookiePayload(session_id="sess-bootstrapped"))
    assert codec.unseal(sealed).session_id == "sess-bootstrapped"

    sm.get_secret_value.assert_called_once_with(SecretId=DATA_KEY_SECRET_ARN)
    kms.decrypt.assert_called_once()
    # Defense against blob substitution: KeyId MUST be pinned on Decrypt.
    kwargs = kms.decrypt.call_args.kwargs
    assert kwargs["KeyId"] == KMS_KEY_ARN
    assert kwargs["CiphertextBlob"] == b"fake-wrapped:" + plaintext


def test_ensure_cipher_caches_after_first_call() -> None:
    """Hot-path requirement: only one Secrets Manager + KMS call per process."""
    sm, kms, _ = _make_mocks_for(secrets.token_bytes(32))
    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        kms_client=kms,
        secrets_manager_client=sm,
    )
    for _ in range(5):
        codec.seal(CookiePayload(session_id="x"))
    assert sm.get_secret_value.call_count == 1
    assert kms.decrypt.call_count == 1


def test_two_codecs_with_same_wrapped_blob_decrypt_to_the_same_cipher() -> None:
    """Regression lock for the dev `bad seal` 401 storm.

    Two independent `CookieCodec` instances simulate two ECS tasks. Both
    fetch the SAME wrapped blob from Secrets Manager and unwrap it with
    the same CMK. A cookie sealed on `task_a` MUST unseal on `task_b`.
    Pre-fix, each task generated its own random data key and this failed.
    """
    plaintext = secrets.token_bytes(32)
    sm_a, kms_a, _ = _make_mocks_for(plaintext)
    sm_b, kms_b, _ = _make_mocks_for(plaintext)

    task_a = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        kms_client=kms_a,
        secrets_manager_client=sm_a,
    )
    task_b = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        kms_client=kms_b,
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
        kms_client=MagicMock(),
        secrets_manager_client=sm,
    )
    with pytest.raises(CookieDataKeyUnavailable):
        codec.unseal("anything")


def test_ensure_cipher_propagates_kms_decrypt_failure() -> None:
    sm, _, _ = _make_mocks_for(secrets.token_bytes(32))
    kms = MagicMock()
    kms.decrypt.side_effect = RuntimeError("KMS unreachable")
    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        kms_client=kms,
        secrets_manager_client=sm,
    )
    with pytest.raises(CookieDataKeyUnavailable):
        codec.unseal("anything")


def test_ensure_cipher_rejects_empty_secret_string() -> None:
    """Bootstrap not yet completed (or secret manually wiped) — fail loud
    rather than silently invalidate every active session."""
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": ""}
    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        kms_client=MagicMock(),
        secrets_manager_client=sm,
    )
    with pytest.raises(CookieDataKeyUnavailable, match="bootstrap missing"):
        codec.unseal("anything")


def test_ensure_cipher_rejects_non_base64_secret() -> None:
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": "!!! not base64 !!!"}
    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        kms_client=MagicMock(),
        secrets_manager_client=sm,
    )
    with pytest.raises(CookieDataKeyUnavailable, match="not valid base64"):
        codec.unseal("anything")


def test_ensure_cipher_rejects_wrong_size_plaintext_key() -> None:
    """KMS returned something, but it's not a 32-byte AES-256 key. Bail
    rather than constructing an invalid `AESGCM` cipher."""
    sm = MagicMock()
    sm.get_secret_value.return_value = {
        "SecretString": base64.b64encode(b"wrapped").decode("ascii")
    }
    kms = MagicMock()
    kms.decrypt.return_value = {"Plaintext": b"too-short"}
    codec = CookieCodec(
        kms_key_arn=KMS_KEY_ARN,
        data_key_secret_arn=DATA_KEY_SECRET_ARN,
        kms_client=kms,
        secrets_manager_client=sm,
    )
    with pytest.raises(CookieDataKeyUnavailable, match="32-byte"):
        codec.unseal("anything")


def test_ensure_cipher_missing_config_surfaces_as_decode_error() -> None:
    """No KMS ARN or no secret ARN — same shape as today's "BFF disabled"
    path. Treated as `bad seal` so the middleware clears the cookie."""
    codec = CookieCodec(kms_key_arn="", data_key_secret_arn="")
    with pytest.raises(CookieDecodeError):
        codec.unseal("anything")
