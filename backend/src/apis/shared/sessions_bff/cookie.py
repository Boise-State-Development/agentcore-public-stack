"""Cookie codec — AES-GCM-sealed session id with a KMS-wrapped data key.

Phase 1 CDK provisioned `BFFCookieSigningKey` as a symmetric KMS key. We
follow the envelope-encryption pattern from the CDK comment:

    1. At first use, call `kms:GenerateDataKey(KeyId=...)` to get a 256-bit
       AES key. The plaintext key is held in process memory; the wrapped
       blob is discarded.
    2. Cookie value = base64url( version || nonce || AES-GCM(payload) ).
       The KMS key is *not* embedded — rotation requires a task restart,
       which is fine for Phase 2 (rotation is on the Phase 7 hardening list).
    3. `unseal` is constant-time on failure: any decode/auth-tag error maps
       to `CookieDecodeError` so callers can't time-distinguish failure modes.

Payload is JSON-encoded so we can extend `CookiePayload` later without
breaking format compatibility (the version byte gates that). The whole
sealed cookie fits comfortably under 256 bytes for a 36-char session id.
"""

from __future__ import annotations

import base64
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


class CookieCodec:
    """Stateful seal/unseal pair backed by a process-cached KMS data key.

    Construct one per process. The first `seal()` or `unseal()` call lazily
    fetches the data key via `kms:GenerateDataKey`; subsequent calls reuse
    the cached cipher. Thread-safe on the lazy-init path.
    """

    def __init__(
        self,
        kms_key_arn: Optional[str] = None,
        *,
        kms_client: Optional[object] = None,
    ) -> None:
        if kms_key_arn is None:
            kms_key_arn = os.environ.get("BFF_COOKIE_SIGNING_KEY_ARN") or ""
        self._kms_key_arn = kms_key_arn
        self._kms_client = kms_client
        self._cipher: Optional[AESGCM] = None
        self._init_lock = Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._kms_key_arn)

    def _ensure_cipher(self) -> AESGCM:
        if self._cipher is not None:
            return self._cipher
        with self._init_lock:
            if self._cipher is not None:
                return self._cipher
            if not self._kms_key_arn:
                raise CookieDecodeError()
            kms = self._kms_client or boto3.client("kms")
            response = kms.generate_data_key(
                KeyId=self._kms_key_arn,
                KeySpec="AES_256",
            )
            plaintext_key = response["Plaintext"]
            self._cipher = AESGCM(plaintext_key)
            logger.info("BFF cookie codec initialized (KMS data key fetched)")
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
        ciphertext = cipher.encrypt(nonce, body, associated_data=None)
        blob = struct.pack("!B", _COOKIE_VERSION) + nonce + ciphertext
        return base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")

    def unseal(self, value: str) -> CookiePayload:
        """Reverse `seal`. Any failure raises `CookieDecodeError` with no
        information about the cause."""
        try:
            cipher = self._ensure_cipher()
            padded = value + "=" * (-len(value) % 4)
            blob = base64.urlsafe_b64decode(padded.encode("ascii"))
            if len(blob) < _VERSION_BYTES + _NONCE_BYTES + 16:
                raise CookieDecodeError()
            (version,) = struct.unpack("!B", blob[:_VERSION_BYTES])
            if version != _COOKIE_VERSION:
                raise CookieDecodeError()
            nonce = blob[_VERSION_BYTES : _VERSION_BYTES + _NONCE_BYTES]
            ciphertext = blob[_VERSION_BYTES + _NONCE_BYTES :]
            body = cipher.decrypt(nonce, ciphertext, associated_data=None)
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
        except (InvalidTag, ValueError, KeyError, json.JSONDecodeError):
            raise CookieDecodeError()
        except Exception:
            # Belt-and-suspenders: any unexpected error becomes the same
            # opaque failure to preserve constant-time behavior.
            raise CookieDecodeError()
