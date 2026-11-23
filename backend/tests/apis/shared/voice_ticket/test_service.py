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
    # Change the first character, which always encodes 6 bits of the digest.
    # Overwriting the tail with a fixed "AA" left the ticket unchanged about
    # once in 1,024 runs: whenever the real signature already ended in "AA".
    forged_sig = ("B" if sig[0] == "A" else "A") + sig[1:]
    forged = f"{body}.{forged_sig}"
    assert forged != ticket
    with pytest.raises(VoiceTicketError):
        await service.verify_and_consume(forged)
    # The legitimate ticket must still be redeemable.
    verified = await service.verify_and_consume(ticket)
    assert verified.jti == claims.jti


@pytest.mark.asyncio
async def test_ticket_is_only_valid_for_its_purpose() -> None:
    service = _make_service()
    dictation_ticket, _ = service.issue(user_id="user-1", session_id="", purpose="dictation")
    with pytest.raises(VoiceTicketError):
        await service.verify_and_consume(dictation_ticket)  # voice by default
    # A wrong-socket attempt must not burn the ticket for its real purpose.
    verified = await service.verify_and_consume(dictation_ticket, purpose="dictation")
    assert verified.purpose == "dictation"

    voice_ticket, _ = service.issue(user_id="user-1", session_id="sess-A")
    with pytest.raises(VoiceTicketError):
        await service.verify_and_consume(voice_ticket, purpose="dictation")
