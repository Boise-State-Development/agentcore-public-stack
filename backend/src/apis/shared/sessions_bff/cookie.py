"""Cookie codec — AES-GCM-sealed session id with a SHA-256-derived data key.

The CDK provisions two collaborating resources:

    - `BFFCookieSigningKey` — symmetric KMS CMK that encrypts the secret at
      rest. App-api never calls KMS directly; SecretsManager invokes
      `kms:Decrypt` on the caller's behalf when `GetSecretValue` runs.
    - `BFFCookieDataKeySecret` — Secrets Manager secret holding a 44-char
      high-entropy random string (~261 bits of entropy) generated once at
      stack create.

Every app-api task on first use:

    1. Reads the secret string from `BFFCookieDataKeySecret`.
    2. Derives the 32-byte AES-256 key with `SHA-256(secret_string)` — a
       single-shot KDF that is secure when the input has ≥256 bits of
       entropy (a 44-char alphanumeric secret has ~261).
    3. Caches the resulting `AESGCM` cipher as the process-wide singleton.

This shared-secret design replaces the prior pattern of each task calling
`kms:GenerateDataKey` directly: that produced a fresh random key per
process, so under `desiredCount > 1` cookies sealed by Task A unsealed as
`bad seal` on Task B (every page-load fan-out became a 401 storm). It
also avoids the chained `AwsCustomResource` bootstrap design that broke
on first deploy because the framework Lambda JSON-stringifies KMS's
`Uint8Array` ciphertext as `{"0":233,...}`, exceeding the 4 KB
CloudFormation response limit.

    - Cookie value = base64url( version || nonce || AES-GCM(payload) ).
    - The KMS key is *not* embedded — rotation requires regenerating the
      secret AND restarting all tasks; in-flight cookies sealed under the
      old key fail to unseal (Phase 7 hardening: kid-versioned cookies
      enable hot rotation).
    - `unseal` is constant-time on failure: any decode/auth-tag error maps
      to `CookieDecodeError` so callers can't time-distinguish failure modes.

Payload is JSON-encoded so we can extend `CookiePayload` later without
breaking format compatibility (the version byte gates that). The whole
sealed cookie fits comfortably under 256 bytes for a 36-char session id.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import struct
from threading import Lock
from typing import Optional

import boto3
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .models import CookiePayload

logger = logging.getLogger(__name__)

_COOKIE_VERSION = 1
_NONCE_BYTES = 12  # AES-GCM standard
_VERSION_BYTES = 1


class CookieDecodeError(Exception):
    """Raised when a cookie value can't be unsealed.

    Carries no detail — the caller treats every failure identically (clear
    the cookie, force re-login). Distinguishing causes leaks oracle bits.
    """


class CookieDataKeyUnavailable(Exception):
    """Raised when the data-key secret can't be fetched at startup.

    Distinct from `CookieDecodeError` so callers can return 5xx (transient
    infra problem — Secrets Manager unreachable, secret empty) rather than
    silently clearing every active user's cookie.
    """


class CookieCodec:
    """Stateful seal/unseal pair backed by a process-cached AES-GCM cipher.

    Construct one per process. The first `seal()` or `unseal()` call lazily
    fetches the **shared** data-key secret from Secrets Manager and
    derives the AES key via SHA-256; subsequent calls reuse the cached
    cipher. Thread-safe on the lazy-init path.

    Across multiple ECS tasks (`desiredCount > 1`), every task's codec
    derives the **same** plaintext key, so cookies sealed by any task
    unseal on any other task. This is the property that the prior
    `kms:GenerateDataKey`-per-process design lacked.
    """

    def __init__(
        self,
        kms_key_arn: Optional[str] = None,
        *,
        data_key_secret_arn: Optional[str] = None,
        secrets_manager_client: Optional[object] = None,
    ) -> None:
        # `kms_key_arn` is retained for config-shape compatibility (and so
        # callers can introspect which CMK encrypts the secret at rest), but
        # the codec no longer calls KMS directly — SecretsManager handles
        # decryption on the caller's behalf when GetSecretValue runs.
        if kms_key_arn is None:
            kms_key_arn = os.environ.get("BFF_COOKIE_SIGNING_KEY_ARN") or ""
        if data_key_secret_arn is None:
            data_key_secret_arn = (
                os.environ.get("BFF_COOKIE_DATA_KEY_SECRET_ARN") or ""
            )
        self._kms_key_arn = kms_key_arn
        self._data_key_secret_arn = data_key_secret_arn
        self._secrets_manager_client = secrets_manager_client
        self._cipher: Optional[AESGCM] = None
        self._init_lock = Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._kms_key_arn) and bool(self._data_key_secret_arn)

    def _ensure_cipher(self) -> AESGCM:
        if self._cipher is not None:
            return self._cipher
        with self._init_lock:
            if self._cipher is not None:
                return self._cipher
            # Configuration missing — surface as decode error so the
            # middleware path stays the same as for a `bad seal` and clears
            # the cookie. (This branch is normally only hit in tests or in
            # a misconfigured deploy; the env vars are populated by CDK.)
            if not self._kms_key_arn or not self._data_key_secret_arn:
                raise CookieDecodeError()

            sm = self._secrets_manager_client or boto3.client("secretsmanager")
            try:
                secret = sm.get_secret_value(SecretId=self._data_key_secret_arn)
            except Exception as exc:
                # Infra failure — propagate so the request returns 5xx
                # rather than silently invalidating sessions.
                raise CookieDataKeyUnavailable(
                    f"Failed to fetch BFF data key secret from Secrets Manager: {exc}"
                ) from exc
            secret_string = secret.get("SecretString") or ""
            if not secret_string:
                raise CookieDataKeyUnavailable(
                    "BFF cookie data key secret is empty — bootstrap missing"
                )

            # Single-shot KDF: SHA-256 of a high-entropy random input
            # produces a uniformly distributed 32-byte AES-256 key. The
            # CDK generates a 44-char alphanumeric secret (~261 bits of
            # entropy), so the SHA-256 output is statistically
            # indistinguishable from random.
            plaintext_key = hashlib.sha256(secret_string.encode("utf-8")).digest()

            self._cipher = AESGCM(plaintext_key)
            logger.info(
                "BFF cookie codec initialized "
                "(data key derived from Secrets Manager secret via SHA-256)"
            )
            return self._cipher

    def seal(self, payload: CookiePayload) -> str:
        """Encode and seal a payload into a cookie value string."""
        cipher = self._ensure_cipher()
        body = json.dumps(
            {
                "sid": payload.session_id,
                "v": payload.version,
                **({"x": payload.extras} if payload.extras else {}),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        # Bind the version byte into the GCM authentication tag — flipping
        # the leading version on the wire then invalidates the tag rather
        # than just tripping the version check downstream.
        version_aad = struct.pack("!B", _COOKIE_VERSION)
        ciphertext = cipher.encrypt(nonce, body, associated_data=version_aad)
        blob = version_aad + nonce + ciphertext
        return base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")

    def unseal(self, value: str) -> CookiePayload:
        """Reverse `seal`.

        Decode-style failures (bad ciphertext, tampered tag, garbage input,
        unknown version) raise `CookieDecodeError` with no information about
        the cause — callers treat every decode failure identically.

        Infrastructure failures from `_ensure_cipher` (Secrets Manager
        unavailable, etc.) propagate up so the middleware can return 5xx
        instead of silently clearing the session cookie and forcing every
        active user to re-login on a transient hiccup.
        """
        # Cipher acquisition is intentionally outside the try/except below —
        # a botocore error here must not be coerced into CookieDecodeError.
        cipher = self._ensure_cipher()
        try:
            padded = value + "=" * (-len(value) % 4)
            blob = base64.urlsafe_b64decode(padded.encode("ascii"))
            if len(blob) < _VERSION_BYTES + _NONCE_BYTES + 16:
                raise CookieDecodeError()
            version_bytes = blob[:_VERSION_BYTES]
            (version,) = struct.unpack("!B", version_bytes)
            if version != _COOKIE_VERSION:
                raise CookieDecodeError()
            nonce = blob[_VERSION_BYTES : _VERSION_BYTES + _NONCE_BYTES]
            ciphertext = blob[_VERSION_BYTES + _NONCE_BYTES :]
            body = cipher.decrypt(nonce, ciphertext, associated_data=version_bytes)
            data = json.loads(body.decode("utf-8"))
            sid = data.get("sid")
            if not isinstance(sid, str) or not sid:
                raise CookieDecodeError()
            return CookiePayload(
                session_id=sid,
                version=int(data.get("v", _COOKIE_VERSION)),
                extras=data.get("x") or {},
            )
        except CookieDecodeError:
            raise
        except (
            InvalidTag,
            ValueError,
            KeyError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            UnicodeEncodeError,
            struct.error,
        ):
            raise CookieDecodeError()


# Process-wide singleton. The first `seal` or `unseal` call fetches the
# shared data-key secret from Secrets Manager and derives the AES key via
# SHA-256; subsequent calls reuse the same `AESGCM` cipher. Across
# processes (e.g. multiple ECS tasks under `desiredCount > 1`), every
# task's singleton derives the **same** plaintext key — so a cookie
# sealed by any task unseals on any other task, including across rolling
# deploys where two task revisions briefly coexist. The seal happens in
# the auth/callback route, the unseal happens in `SessionRefreshMiddleware`
# and the voice WebSocket route, and they all MUST go through this
# singleton.
_default_codec: Optional[CookieCodec] = None
_default_codec_lock = Lock()


def get_default_codec() -> CookieCodec:
    global _default_codec
    if _default_codec is not None:
        return _default_codec
    with _default_codec_lock:
        if _default_codec is None:
            _default_codec = CookieCodec()
    return _default_codec


def _reset_default_codec_for_tests() -> None:
    """Drop the singleton so tests can rebuild it under their own fixtures."""
    global _default_codec
    with _default_codec_lock:
        _default_codec = None


def _set_default_codec_for_tests(codec: CookieCodec) -> None:
    """Install a pre-built codec (typically with `_cipher` pre-injected so
    no Secrets Manager call is made) as the process-wide singleton."""
    global _default_codec
    with _default_codec_lock:
        _default_codec = codec
