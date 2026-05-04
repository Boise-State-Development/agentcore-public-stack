"""Tests for the high-level VoiceTicketService facade.

These cover the end-to-end "verify then mark consumed" sequence that the
WebSocket route depends on. Replay must be rejected even if the signature
and expiry are valid.
"""

from __future__ import annotations

import pytest

from apis.shared.voice_ticket.codec import VoiceTicketCodec, VoiceTicketError
from apis.shared.voice_ticket.replay import VoiceTicketReplayStore
from apis.shared.voice_ticket.service import VoiceTicketService


def _make_service() -> VoiceTicketService:
    return VoiceTicketService(
        codec=VoiceTicketCodec(b"k" * 64),
        replay_store=VoiceTicketReplayStore(table_name=""),
        ttl_seconds=60,
    )


@pytest.mark.asyncio
async def test_issue_then_verify_consumes_jti() -> None:
    service = _make_service()
    ticket, claims = service.issue(user_id="user-1", session_id="sess-A")
    verified = await service.verify_and_consume(ticket)
    assert verified.user_id == "user-1"
    assert verified.session_id == "sess-A"
    assert verified.jti == claims.jti


@pytest.mark.asyncio
async def test_replay_is_rejected() -> None:
    service = _make_service()
    ticket, _ = service.issue(user_id="user-1", session_id="sess-A")
    await service.verify_and_consume(ticket)
    with pytest.raises(VoiceTicketError):
        await service.verify_and_consume(ticket)


@pytest.mark.asyncio
async def test_invalid_signature_does_not_consume_jti() -> None:
    """A failed signature check must short-circuit before the replay store
    sees the jti, otherwise an attacker could lock out a real user by
    pre-consuming jti's they observe.
    """
    service = _make_service()
    ticket, claims = service.issue(user_id="user-1", session_id="sess-A")
    body, _, sig = ticket.partition(".")
    forged = f"{body}.{sig[:-2]}AA"  # tamper sig
    with pytest.raises(VoiceTicketError):
        await service.verify_and_consume(forged)
    # The legitimate ticket must still be redeemable.
    verified = await service.verify_and_consume(ticket)
    assert verified.jti == claims.jti
