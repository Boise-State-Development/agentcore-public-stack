"""Tests for the voice-ticket replay store (in-memory fallback path)."""

from __future__ import annotations

import time

import pytest

from apis.shared.voice_ticket.replay import VoiceTicketReplayStore


@pytest.mark.asyncio
async def test_first_consume_succeeds_second_fails() -> None:
    store = VoiceTicketReplayStore(table_name="")
    exp = int(time.time()) + 60
    assert await store.consume("jti-1", exp=exp) is True
    assert await store.consume("jti-1", exp=exp) is False


@pytest.mark.asyncio
async def test_distinct_jtis_independent() -> None:
    store = VoiceTicketReplayStore(table_name="")
    exp = int(time.time()) + 60
    assert await store.consume("jti-A", exp=exp) is True
    assert await store.consume("jti-B", exp=exp) is True


@pytest.mark.asyncio
async def test_consume_rejects_empty_jti() -> None:
    store = VoiceTicketReplayStore(table_name="")
    with pytest.raises(ValueError):
        await store.consume("", exp=int(time.time()) + 60)


def test_in_memory_mode_when_table_unset() -> None:
    store = VoiceTicketReplayStore(table_name="")
    assert store.enabled is False
