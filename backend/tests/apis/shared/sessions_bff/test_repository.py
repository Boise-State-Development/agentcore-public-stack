"""Tests for SessionRepository against moto-backed DynamoDB."""

from __future__ import annotations

import time

import pytest


@pytest.mark.asyncio
async def test_get_returns_none_for_missing_session(repository) -> None:
    assert await repository.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_put_then_get_round_trip(repository, sample_record) -> None:
    record = sample_record()
    await repository.put(record)
    fetched = await repository.get(record.session_id)
    assert fetched is not None
    assert fetched.session_id == record.session_id
    assert fetched.user_id == record.user_id
    assert fetched.username == record.username
    assert fetched.cognito_access_token == record.cognito_access_token
    assert fetched.cognito_refresh_token == record.cognito_refresh_token
    assert fetched.id_token == record.id_token
    assert fetched.csrf_secret == record.csrf_secret


@pytest.mark.asyncio
async def test_update_tokens_replaces_token_attrs(repository, sample_record) -> None:
    record = sample_record()
    await repository.put(record)

    new_exp = int(time.time()) + 7200
    await repository.update_tokens(
        session_id=record.session_id,
        access_token="new.access",
        refresh_token="new.refresh",
        id_token="new.id",
        access_token_exp=new_exp,
        last_seen_at=int(time.time()),
    )

    fetched = await repository.get(record.session_id)
    assert fetched is not None
    assert fetched.cognito_access_token == "new.access"
    assert fetched.cognito_refresh_token == "new.refresh"
    assert fetched.id_token == "new.id"
    assert fetched.access_token_exp == new_exp


@pytest.mark.asyncio
async def test_get_treats_ttl_expired_row_as_missing(
    repository, sample_record
) -> None:
    """DDB TTL eviction is best-effort; the repo enforces it on read."""
    record = sample_record(ttl=int(time.time()) - 1)
    await repository.put(record)
    assert await repository.get(record.session_id) is None


@pytest.mark.asyncio
async def test_delete_removes_row(repository, sample_record) -> None:
    record = sample_record()
    await repository.put(record)
    await repository.delete(record.session_id)
    assert await repository.get(record.session_id) is None


@pytest.mark.asyncio
async def test_disabled_repository_is_inert() -> None:
    from apis.shared.sessions_bff.repository import SessionRepository

    repo = SessionRepository(table_name="")
    assert repo.enabled is False
    # All ops succeed silently — no exceptions, no AWS calls.
    assert await repo.get("any") is None
    await repo.delete("any")
