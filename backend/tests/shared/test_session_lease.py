"""Tests for the per-session single-flight lease (concurrency guard).

Covers the server-side race PR #653's follow-up closes: two concurrent
/invocations for one session. See docs/specs/session-single-flight-guard.md and
apis/shared/sessions/session_lease.py.

All tests run against a moto-backed ``sessions-metadata`` table via the shared
``sessions_metadata_table`` fixture.
"""

import time

import pytest

from apis.shared.sessions.session_lease import (
    LEASE_WINDOW_SECONDS,
    SessionBusyError,
    SessionLease,
    acquire_session_lease,
    release_session_lease,
    renew_session_lease,
)


def _lease_item(table, session_id="s1", user_id="u1"):
    resp = table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"LEASE#{session_id}"})
    return resp.get("Item")


class TestAcquire:
    @pytest.mark.asyncio
    async def test_acquire_writes_lease_item(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        assert isinstance(lease, SessionLease)
        assert lease.owner

        item = _lease_item(sessions_metadata_table)
        assert item is not None
        assert item["PK"] == "USER#u1"
        assert item["SK"] == "LEASE#s1"
        assert item["leaseOwner"] == lease.owner
        # leaseExpiresAt is the app-level validity check; ttl is the auto-reap
        # backstop set further out.
        assert int(item["leaseExpiresAt"]) > int(time.time())
        assert int(item["ttl"]) > int(item["leaseExpiresAt"])

    @pytest.mark.asyncio
    async def test_second_concurrent_acquire_is_rejected(self, sessions_metadata_table):
        first = await acquire_session_lease("s1", "u1")
        assert first is not None
        # A duplicate arriving while the first turn holds an unexpired lease.
        with pytest.raises(SessionBusyError):
            await acquire_session_lease("s1", "u1")

    @pytest.mark.asyncio
    async def test_acquire_over_expired_lease_succeeds(self, sessions_metadata_table):
        # Simulate a crashed turn that left a stale (expired) lease behind:
        # write the item directly with a past leaseExpiresAt.
        now = int(time.time())
        sessions_metadata_table.put_item(
            Item={
                "PK": "USER#u1",
                "SK": "LEASE#s1",
                "leaseOwner": "dead-owner",
                "leaseExpiresAt": now - 10,
                "ttl": now + 3600,
            }
        )
        lease = await acquire_session_lease("s1", "u1")
        assert lease is not None
        assert lease.owner != "dead-owner"
        assert _lease_item(sessions_metadata_table)["leaseOwner"] == lease.owner

    @pytest.mark.asyncio
    async def test_distinct_sessions_do_not_conflict(self, sessions_metadata_table):
        a = await acquire_session_lease("s1", "u1")
        b = await acquire_session_lease("s2", "u1")
        assert a is not None and b is not None
        assert a.owner != b.owner

    @pytest.mark.asyncio
    async def test_force_takes_over_active_lease(self, sessions_metadata_table):
        # Resume / continuation: an active lease must NOT block them.
        first = await acquire_session_lease("s1", "u1")
        assert first is not None
        resumed = await acquire_session_lease("s1", "u1", force=True)
        assert resumed is not None
        assert resumed.owner != first.owner
        # The forced acquire installs its own lease so a *fresh* duplicate
        # arriving during the resume is still rejected.
        assert _lease_item(sessions_metadata_table)["leaseOwner"] == resumed.owner
        with pytest.raises(SessionBusyError):
            await acquire_session_lease("s1", "u1")

    @pytest.mark.asyncio
    async def test_no_table_configured_returns_none(self, aws, monkeypatch):
        # Local / no-DynamoDB path: guard is inactive, never blocks a turn.
        monkeypatch.delenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", raising=False)
        assert await acquire_session_lease("s1", "u1") is None


class TestRenew:
    @pytest.mark.asyncio
    async def test_renew_extends_window_for_owner(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        # Backdate the stored window so a renewal is observably larger even
        # within the same wall-clock second.
        sessions_metadata_table.update_item(
            Key={"PK": lease.pk, "SK": lease.sk},
            UpdateExpression="SET leaseExpiresAt = :old",
            ExpressionAttributeValues={":old": int(time.time()) - 5},
        )
        await renew_session_lease(lease)
        item = _lease_item(sessions_metadata_table)
        assert int(item["leaseExpiresAt"]) >= int(time.time()) + LEASE_WINDOW_SECONDS - 1
        assert item["leaseOwner"] == lease.owner

    @pytest.mark.asyncio
    async def test_renew_by_non_owner_is_noop(self, sessions_metadata_table):
        await acquire_session_lease("s1", "u1")
        original = _lease_item(sessions_metadata_table)
        # A container that lost the lease tries to renew — owner-scoped, so the
        # current owner's window is untouched and no error surfaces.
        stale = SessionLease(session_id="s1", user_id="u1", owner="stale-owner")
        await renew_session_lease(stale)
        after = _lease_item(sessions_metadata_table)
        assert after["leaseOwner"] == original["leaseOwner"]
        assert int(after["leaseExpiresAt"]) == int(original["leaseExpiresAt"])

    @pytest.mark.asyncio
    async def test_renew_none_is_noop(self, sessions_metadata_table):
        await renew_session_lease(None)  # must not raise


class TestRelease:
    @pytest.mark.asyncio
    async def test_release_deletes_own_lease(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await release_session_lease(lease)
        assert _lease_item(sessions_metadata_table) is None
        # Session is free again — a new turn can acquire.
        again = await acquire_session_lease("s1", "u1")
        assert again is not None

    @pytest.mark.asyncio
    async def test_release_by_non_owner_leaves_lease(self, sessions_metadata_table):
        real = await acquire_session_lease("s1", "u1")
        stale = SessionLease(session_id="s1", user_id="u1", owner="stale-owner")
        # A lapsed-then-retaken owner must not delete the new owner's lease.
        await release_session_lease(stale)
        item = _lease_item(sessions_metadata_table)
        assert item is not None
        assert item["leaseOwner"] == real.owner

    @pytest.mark.asyncio
    async def test_release_is_idempotent(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await release_session_lease(lease)
        await release_session_lease(lease)  # second is a no-op conditional miss
        assert _lease_item(sessions_metadata_table) is None

    @pytest.mark.asyncio
    async def test_release_none_is_noop(self, sessions_metadata_table):
        await release_session_lease(None)  # must not raise


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_acquire_release_reacquire_cycle(self, sessions_metadata_table):
        """A normal turn: acquire, run, release; the next turn acquires cleanly."""
        for _ in range(3):
            lease = await acquire_session_lease("s1", "u1")
            assert lease is not None
            # Duplicate during the turn is rejected.
            with pytest.raises(SessionBusyError):
                await acquire_session_lease("s1", "u1")
            await release_session_lease(lease)
