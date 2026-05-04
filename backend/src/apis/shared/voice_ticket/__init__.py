"""Voice WebSocket-upgrade ticket — single-use, HMAC-signed, short-lived.

Issued by app-api on a CSRF-protected POST and consumed by app-api on the
WebSocket upgrade before it relays to the AgentCore Runtime upstream. App-api
is both issuer and verifier; inference-api never sees the ticket. The Cognito
JWT that authenticates the upstream hop is held server-side in the BFF
session and forwarded by app-api — see ``app_api/voice/proxy.py``.
"""

from .codec import VoiceTicketClaims, VoiceTicketCodec, VoiceTicketError
from .replay import VoiceTicketReplayStore
from .service import VoiceTicketService, get_default_service

__all__ = [
    "VoiceTicketClaims",
    "VoiceTicketCodec",
    "VoiceTicketError",
    "VoiceTicketReplayStore",
    "VoiceTicketService",
    "get_default_service",
]
