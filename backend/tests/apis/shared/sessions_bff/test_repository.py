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


# =====================================================================
# Cross-task refresh lock — try_acquire_refresh_lock / release_refresh_lock
# =====================================================================


@pytest.mark.asyncio
async def test_try_acquire_refresh_lock_succeeds_on_unlocked_row(
    repository, sample_record
) -> None:
    """The first contender claims the lock when no peer is holding one."""
    record = sample_record()
    await repository.put(record)

    acquired = await repository.try_acquire_refresh_lock(
        session_id=record.session_id,
        owner="task-A",
        lock_ttl_seconds=30,
    )
    assert acquired is True


@pytest.mark.asyncio
async def test_try_acquire_refresh_lock_blocks_concurrent_peer(
    repository, sample_record
) -> None:
    """While task-A's lock is fresh, task-B's acquisition MUST fail.

    This is the cross-task coalescing primitive — without it, two tasks
    would each call cognito-idp:initiate_auth with the same refresh token
    under desiredCount > 1.
    """
    record = sample_record()
    await repository.put(record)

    a = await repository.try_acquire_refresh_lock(
        session_id=record.session_id,
        owner="task-A",
        lock_ttl_seconds=30,
    )
    b = await repository.try_acquire_refresh_lock(
        session_id=record.session_id,
        owner="task-B",
        lock_ttl_seconds=30,
    )
    assert a is True
    assert b is False


@pytest.mark.asyncio
async def test_try_acquire_refresh_lock_takes_over_after_ttl_expires(
    repository, sample_record
) -> None:
    """A leader that crashed mid-refresh strands the lock for at most
    `lock_ttl_seconds`. After that, any peer can re-acquire — no manual
    cleanup required, no permanent stuck state."""
    record = sample_record()
    await repository.put(record)

    # task-A acquires with a 0-second TTL → lock_until = now, so any
    # contender at a later second sees `refresh_lock_until < :now`.
    a = await repository.try_acquire_refresh_lock(
        session_id=record.session_id,
        owner="task-A",
        lock_ttl_seconds=0,
    )
    assert a is True

    # Sleep 1s so the next contender's :now is strictly greater.
    time.sleep(1)

    b = await repository.try_acquire_refresh_lock(
        session_id=record.session_id,
        owner="task-B",
        lock_ttl_seconds=30,
    )
    assert b is True


@pytest.mark.asyncio
async def test_try_acquire_refresh_lock_distinct_sessions_dont_block(
    repository, sample_record
) -> None:
    rec_a = sample_record(session_id="sess-A")
    rec_b = sample_record(session_id="sess-B")
    await repository.put(rec_a)
    await repository.put(rec_b)

    a = await repository.try_acquire_refresh_lock(
        session_id=rec_a.session_id, owner="task-1", lock_ttl_seconds=30
    )
    b = await repository.try_acquire_refresh_lock(
        session_id=rec_b.session_id, owner="task-1", lock_ttl_seconds=30
    )
    assert a is True
    assert b is True


@pytest.mark.asyncio
async def test_release_refresh_lock_clears_attrs_for_owner(
    repository, sample_record
) -> None:
    record = sample_record()
    await repository.put(record)
    await repository.try_acquire_refresh_lock(
        session_id=record.session_id, owner="task-A", lock_ttl_seconds=30
    )

    await repository.release_refresh_lock(record.session_id, owner="task-A")

    # After release a peer can immediately acquire.
    b = await repository.try_acquire_refresh_lock(
        session_id=record.session_id, owner="task-B", lock_ttl_seconds=30
    )
    assert b is True


@pytest.mark.asyncio
async def test_release_refresh_lock_is_no_op_for_non_owner(
    repository, sample_record
) -> None:
    """Best-effort release: if a peer has already taken over the lock
    (because ours TTL'd), the release MUST NOT clear their lock attrs."""
    record = sample_record()
    await repository.put(record)
    await repository.try_acquire_refresh_lock(
        session_id=record.session_id, owner="task-A", lock_ttl_seconds=30
    )

    # task-B (who never held the lock) calls release — must not blow away
    # task-A's lock.
    await repository.release_refresh_lock(record.session_id, owner="task-B")

    # task-A's lock is still in force; a third contender can't acquire.
    c = await repository.try_acquire_refresh_lock(
        session_id=record.session_id, owner="task-C", lock_ttl_seconds=30
    )
    assert c is False


@pytest.mark.asyncio
async def test_update_tokens_with_lock_owner_clears_lock_atomically(
    repository, sample_record
) -> None:
    """Successful refresh persist clears the lock attributes in the same
    write so peers don't have to wait for the TTL to retry."""
    record = sample_record()
    await repository.put(record)
    await repository.try_acquire_refresh_lock(
        session_id=record.session_id, owner="task-A", lock_ttl_seconds=30
    )

    await repository.update_tokens(
        session_id=record.session_id,
        access_token="access.fresh",
        refresh_token="refresh.rotated",
        id_token="id.fresh",
        access_token_exp=int(time.time()) + 3600,
        last_seen_at=int(time.time()),
        expected_lock_owner="task-A",
    )

    # Lock cleared → another contender can acquire immediately.
    b = await repository.try_acquire_refresh_lock(
        session_id=record.session_id, owner="task-B", lock_ttl_seconds=30
    )
    assert b is True


@pytest.mark.asyncio
async def test_update_tokens_rejects_persist_when_peer_owns_the_lock(
    repository, sample_record
) -> None:
    """Stale-leader guard: if our lock TTL'd and a peer took over, we must
    NOT overwrite their freshly persisted tokens. ConditionalCheckFailed
    propagates so the caller can re-read DDB and adopt the peer's state."""
    from botocore.exceptions import ClientError

    record = sample_record()
    await repository.put(record)
    # Peer task acquired the lock.
    await repository.try_acquire_refresh_lock(
        session_id=record.session_id, owner="peer-task", lock_ttl_seconds=30
    )

    with pytest.raises(ClientError) as exc_info:
        await repository.update_tokens(
            session_id=record.session_id,
            access_token="access.stale",
            refresh_token="refresh.stale",
            id_token="id.stale",
            access_token_exp=int(time.time()) + 3600,
            last_seen_at=int(time.time()),
            expected_lock_owner="our-task",  # ≠ peer-task
        )
    assert (
        exc_info.value.response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )
