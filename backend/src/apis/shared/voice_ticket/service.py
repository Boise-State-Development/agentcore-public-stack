"""High-level facade — issue + verify + replay enforcement in one call.

The codec and replay store are useful in isolation for tests, but production
code paths want a single ``service.issue()`` / ``service.verify_and_consume()``
call against a process-wide singleton. Resolving the signing key and the
replay table is lazy: the first call fetches the secret from Secrets Manager
and caches the bytes for the lifetime of the process.
"""

from __future__ import annotations

import json
import logging
import os
from threading import Lock
from typing import Optional

import boto3

from .codec import VoiceTicketClaims, VoiceTicketCodec, VoiceTicketError
from .replay import VoiceTicketReplayStore, get_default_store

logger = logging.getLogger(__name__)


class VoiceTicketService:
    """Bundles a codec with a replay store.

    ``verify_and_consume`` is the only call that touches both — keeping the
    "verify then mark consumed" sequence atomic to one call site means a
    future caller can't accidentally verify without enforcing single-use.
    """

    def __init__(
        self,
        *,
        codec: VoiceTicketCodec,
        replay_store: VoiceTicketReplayStore,
        ttl_seconds: int = 60,
    ) -> None:
        self._codec = codec
        self._replay_store = replay_store
        self._ttl_seconds = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def issue(self, *, user_id: str, session_id: str) -> tuple[str, VoiceTicketClaims]:
        return self._codec.issue(
            user_id=user_id,
            session_id=session_id,
            ttl_seconds=self._ttl_seconds,
        )

    async def verify_and_consume(self, ticket: str) -> VoiceTicketClaims:
        """Verify the ticket and mark its jti consumed.

        Raises ``VoiceTicketError`` on signature/expiry/format failure or on
        replay (jti already recorded). Replays are reported as a generic
        ``VoiceTicketError`` so callers don't branch on the cause — the user
        flow is identical: reject the WS upgrade, ask the SPA to re-fetch.
        """
        claims = self._codec.verify(ticket)
        accepted = await self._replay_store.consume(claims.jti, exp=claims.exp)
        if not accepted:
            raise VoiceTicketError("ticket already consumed")
        return claims


# ─── Lazy process-wide singleton ────────────────────────────────────────

_default_service: Optional[VoiceTicketService] = None
_init_lock = Lock()


def _resolve_signing_key(
    *,
    secret_arn: Optional[str] = None,
    region: Optional[str] = None,
    secrets_manager_client: Optional[object] = None,
) -> bytes:
    """Fetch the HMAC signing key from Secrets Manager.

    Tolerates both the plain-string SecretString format and a
    ``{"secret": "..."}`` JSON wrapper (which is the shape CDK writes via
    ``generateSecretString`` with a ``generateStringKey``).
    """
    arn = secret_arn or os.environ.get("VOICE_TICKET_SIGNING_SECRET_ARN") or ""
    if not arn:
        raise RuntimeError("VOICE_TICKET_SIGNING_SECRET_ARN is not configured")
    sm_region = (
        region
        or os.environ.get("AWS_REGION")
        or "us-west-2"
    )
    sm = secrets_manager_client or boto3.client("secretsmanager", region_name=sm_region)
    response = sm.get_secret_value(SecretId=arn)
    value = response.get("SecretString") or ""
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
            value = parsed.get("secret") or parsed.get("clientSecret") or value
        except json.JSONDecodeError:
            logger.debug(
                "voice ticket signing secret looked like JSON but failed to decode; using raw value"
            )
    if not value:
        raise RuntimeError("voice ticket signing secret resolved to empty string")
    return value.encode("utf-8")


def get_default_service() -> VoiceTicketService:
    """Process-wide singleton. First call fetches the signing secret."""
    global _default_service
    if _default_service is not None:
        return _default_service
    with _init_lock:
        if _default_service is not None:
            return _default_service
        key = _resolve_signing_key()
        codec = VoiceTicketCodec(key)
        _default_service = VoiceTicketService(
            codec=codec,
            replay_store=get_default_store(),
        )
        return _default_service


def _reset_default_service_for_tests() -> None:
    global _default_service
    _default_service = None
