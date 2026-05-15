"""Ticket codec — base64url(payload) "." base64url(HMAC-SHA256(payload, key)).

Payload is a compact JSON object: ``{"v":1,"sub":...,"sid":...,"jti":...,"iat":...,"exp":...}``.
Both halves are base64url without padding so the ticket survives a query
string or ``Sec-WebSocket-Protocol`` value without escaping.

The codec is stateless aside from holding the signing key. A caching wrapper
in ``service.py`` lazy-fetches the key from Secrets Manager once per process.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional


_TICKET_VERSION = 1


class VoiceTicketError(Exception):
    """Raised when a ticket fails to decode, verify, or has expired.

    The message is intentionally generic — verifiers should not echo it back
    to clients, and callers should not branch on the message text. Distinguish
    only by exception type.
    """


@dataclass(frozen=True)
class VoiceTicketClaims:
    """Verified ticket payload.

    ``jti`` is the random per-ticket id used by the replay store to enforce
    single-use. ``exp`` is epoch seconds.
    """

    user_id: str
    session_id: str
    jti: str
    iat: int
    exp: int


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise VoiceTicketError("invalid base64url") from exc


class VoiceTicketCodec:
    """Issue and verify HMAC-signed voice tickets.

    Construct one per process with the shared signing key. ``issue`` mints a
    fresh ticket, ``verify`` validates signature, expiry, and ``v`` version.
    Replay protection is the caller's responsibility — pass the returned
    ``jti`` to ``VoiceTicketReplayStore.consume``.
    """

    def __init__(self, signing_key: bytes) -> None:
        if not signing_key:
            raise ValueError("signing_key must be non-empty")
        self._key = signing_key

    def issue(
        self,
        *,
        user_id: str,
        session_id: str,
        ttl_seconds: int = 60,
        now: Optional[int] = None,
    ) -> tuple[str, VoiceTicketClaims]:
        if not user_id or not session_id:
            raise ValueError("user_id and session_id are required")
        issued_at = int(now if now is not None else time.time())
        claims = VoiceTicketClaims(
            user_id=user_id,
            session_id=session_id,
            jti=secrets.token_urlsafe(16),
            iat=issued_at,
            exp=issued_at + ttl_seconds,
        )
        payload = {
            "v": _TICKET_VERSION,
            "sub": claims.user_id,
            "sid": claims.session_id,
            "jti": claims.jti,
            "iat": claims.iat,
            "exp": claims.exp,
        }
        body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        sig = _b64url_encode(self._sign(body.encode("ascii")))
        return f"{body}.{sig}", claims

    def verify(self, ticket: str, *, now: Optional[int] = None) -> VoiceTicketClaims:
        if not isinstance(ticket, str) or "." not in ticket:
            raise VoiceTicketError("malformed ticket")
        body, _, sig = ticket.partition(".")
        if not body or not sig:
            raise VoiceTicketError("malformed ticket")
        expected_sig = _b64url_encode(self._sign(body.encode("ascii")))
        if not hmac.compare_digest(sig, expected_sig):
            raise VoiceTicketError("signature mismatch")
        try:
            payload = json.loads(_b64url_decode(body))
        except json.JSONDecodeError as exc:
            raise VoiceTicketError("invalid payload") from exc
        if not isinstance(payload, dict) or payload.get("v") != _TICKET_VERSION:
            raise VoiceTicketError("unsupported ticket version")

        try:
            user_id = str(payload["sub"])
            session_id = str(payload["sid"])
            jti = str(payload["jti"])
            iat = int(payload["iat"])
            exp = int(payload["exp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VoiceTicketError("invalid payload") from exc

        clock = int(now if now is not None else time.time())
        if exp <= clock:
            raise VoiceTicketError("ticket expired")

        return VoiceTicketClaims(
            user_id=user_id,
            session_id=session_id,
            jti=jti,
            iat=iat,
            exp=exp,
        )

    def _sign(self, body: bytes) -> bytes:
        return hmac.new(self._key, body, hashlib.sha256).digest()
