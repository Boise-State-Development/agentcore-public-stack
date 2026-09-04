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
    STEER_QUEUE_MAX_CHARS,
    STEER_QUEUE_MAX_ENTRIES,
    SessionBusyError,
    SessionLease,
    SteerQueueFullError,
    acquire_session_lease,
    clear_steer_entry,
    is_session_lease_held,
    peek_steer_queue,
    release_session_lease,
    remove_steer_entry,
    renew_session_lease,
    seed_steer_queue,
    request_session_cancel,
    request_session_steer,
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


class TestIsHeld:
    """The read-only probe app-api uses to explain a Runtime 424 rewrite."""

    @pytest.mark.asyncio
    async def test_reports_held_while_a_turn_owns_the_lease(
        self, sessions_metadata_table
    ):
        await acquire_session_lease("s1", "u1")
        assert await is_session_lease_held("s1", "u1") is True

    @pytest.mark.asyncio
    async def test_reports_free_when_no_lease_exists(self, sessions_metadata_table):
        assert await is_session_lease_held("s1", "u1") is False

    @pytest.mark.asyncio
    async def test_reports_free_after_release(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await release_session_lease(lease)
        assert await is_session_lease_held("s1", "u1") is False

    @pytest.mark.asyncio
    async def test_expired_lease_reads_as_free(self, sessions_metadata_table):
        # Matches acquire's condition: a lapsed lease is not a conflict, so it
        # must never be reported as one (that would turn a container crash's
        # 424 into a misleading "already responding" notice).
        now = int(time.time())
        sessions_metadata_table.put_item(
            Item={
                "PK": "USER#u1",
                "SK": "LEASE#s1",
                "leaseOwner": "dead-owner",
                "leaseExpiresAt": now - 1,
                "ttl": now + 3600,
            }
        )
        assert await is_session_lease_held("s1", "u1") is False

    @pytest.mark.asyncio
    async def test_is_scoped_to_the_named_session_and_user(
        self, sessions_metadata_table
    ):
        await acquire_session_lease("s1", "u1")
        assert await is_session_lease_held("s2", "u1") is False
        assert await is_session_lease_held("s1", "u2") is False


class TestCancel:
    @pytest.mark.asyncio
    async def test_renew_reports_no_cancel_by_default(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        assert await renew_session_lease(lease) is False

    @pytest.mark.asyncio
    async def test_request_cancel_is_observed_by_owner_renew(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        armed = await request_session_cancel("s1", "u1")
        assert armed is True
        # The container running the turn sees it on its next heartbeat renew.
        assert await renew_session_lease(lease) is True

    @pytest.mark.asyncio
    async def test_request_cancel_with_no_active_lease_is_noop(self, sessions_metadata_table):
        # No turn streaming → nothing to cancel.
        assert await request_session_cancel("s1", "u1") is False

    @pytest.mark.asyncio
    async def test_cancel_is_owner_scoped_across_takeover(self, sessions_metadata_table):
        # A Stop arms a cancel against the current owner; then that turn ends
        # and a new one force-acquires (resume). The new owner must NOT inherit
        # the old cancel.
        first = await acquire_session_lease("s1", "u1")
        await request_session_cancel("s1", "u1")
        assert await renew_session_lease(first) is True  # armed for `first`

        resumed = await acquire_session_lease("s1", "u1", force=True)
        assert resumed.owner != first.owner
        # acquire cleared the stale marker; the new owner sees no cancel.
        item = _lease_item(sessions_metadata_table)
        assert "cancelRequestedFor" not in item
        assert await renew_session_lease(resumed) is False

    @pytest.mark.asyncio
    async def test_cancel_after_takeover_targets_only_new_owner(self, sessions_metadata_table):
        first = await acquire_session_lease("s1", "u1")
        resumed = await acquire_session_lease("s1", "u1", force=True)
        # A fresh Stop now arms against the current (resumed) owner.
        await request_session_cancel("s1", "u1")
        assert await renew_session_lease(resumed) is True
        # The superseded owner never sees it (it lost the lease anyway).
        assert await renew_session_lease(first) is False

    @pytest.mark.asyncio
    async def test_release_after_cancel_frees_session(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await request_session_cancel("s1", "u1")
        await release_session_lease(lease)
        assert _lease_item(sessions_metadata_table) is None
        # Resend acquires cleanly, with no leftover cancel marker.
        again = await acquire_session_lease("s1", "u1")
        assert again is not None
        assert await renew_session_lease(again) is False


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


class TestSteerInbox:
    """The lease row's mid-turn steering inbox (docs/specs/mid-turn-steering.md).

    Same item, same owner-scoping, same fail-soft posture as the cancel marker.
    The property that matters throughout: the user's words are either injected
    exactly once or left for the end-of-turn flush — never both, never neither.
    """

    @pytest.mark.asyncio
    async def test_steer_is_queued_and_peekable_by_owner(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        assert await request_session_steer("s1", "u1", text="use the other file", entry_id="e1") is True

        entries = await peek_steer_queue(lease)
        assert [e["id"] for e in entries] == ["e1"]
        assert entries[0]["text"] == "use the other file"
        assert entries[0]["at"]

    @pytest.mark.asyncio
    async def test_steer_with_no_active_lease_is_noop(self, sessions_metadata_table):
        # No turn streaming → nothing to steer. The SPA sends a normal turn.
        assert await request_session_steer("s1", "u1", text="hi", entry_id="e1") is False
        assert _lease_item(sessions_metadata_table) is None

    @pytest.mark.asyncio
    async def test_steer_after_turn_ended_is_rejected(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await release_session_lease(lease)
        # The turn ended between the user typing and the POST landing. This
        # race resolving to "not queued" is the correct outcome.
        assert await request_session_steer("s1", "u1", text="hi", entry_id="e1") is False

    @pytest.mark.asyncio
    async def test_peek_does_not_consume(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await request_session_steer("s1", "u1", text="hi", entry_id="e1")
        # Commit-on-append: AfterToolsEvent also fires on the interrupt path,
        # where the mutated message is discarded. A peek that consumed would
        # destroy the user's words on exactly that path.
        assert len(await peek_steer_queue(lease)) == 1
        assert len(await peek_steer_queue(lease)) == 1

    @pytest.mark.asyncio
    async def test_entries_are_peeked_in_arrival_order(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        for i in range(3):
            await request_session_steer("s1", "u1", text=f"t{i}", entry_id=f"e{i}")
        assert [e["id"] for e in await peek_steer_queue(lease)] == ["e0", "e1", "e2"]

    @pytest.mark.asyncio
    async def test_peek_is_owner_scoped(self, sessions_metadata_table):
        first = await acquire_session_lease("s1", "u1")
        await request_session_steer("s1", "u1", text="for the first turn", entry_id="e1")
        # The turn ends and a resume force-acquires. The new owner must not
        # inherit the previous turn's inbox.
        resumed = await acquire_session_lease("s1", "u1", force=True)
        assert resumed.owner != first.owner
        assert await peek_steer_queue(resumed) == []

    @pytest.mark.asyncio
    async def test_peek_with_no_lease_row_is_empty(self, sessions_metadata_table):
        lease = SessionLease(session_id="s1", user_id="u1", owner="ghost")
        assert await peek_steer_queue(lease) == []

    @pytest.mark.asyncio
    async def test_clear_removes_only_the_named_entry(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        for i in range(3):
            await request_session_steer("s1", "u1", text=f"t{i}", entry_id=f"e{i}")

        assert await clear_steer_entry(lease, "e1") is True
        assert [e["id"] for e in await peek_steer_queue(lease)] == ["e0", "e2"]

    @pytest.mark.asyncio
    async def test_clear_is_idempotent(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await request_session_steer("s1", "u1", text="hi", entry_id="e1")

        assert await clear_steer_entry(lease, "e1") is True
        # A re-delivery after a lost ack must not remove a later entry that
        # slid into the vacated index.
        await request_session_steer("s1", "u1", text="second", entry_id="e2")
        assert await clear_steer_entry(lease, "e1") is False
        assert [e["id"] for e in await peek_steer_queue(lease)] == ["e2"]

    @pytest.mark.asyncio
    async def test_clear_is_owner_scoped(self, sessions_metadata_table):
        first = await acquire_session_lease("s1", "u1")
        await request_session_steer("s1", "u1", text="hi", entry_id="e1")
        resumed = await acquire_session_lease("s1", "u1", force=True)
        # A superseded turn cannot consume the current owner's inbox.
        assert await clear_steer_entry(first, "e1") is False

    @pytest.mark.asyncio
    async def test_remove_withdraws_a_queued_entry_for_the_user(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await request_session_steer("s1", "u1", text="hi", entry_id="e1")
        # The user deleted the pending-ack chip from the composer.
        assert await remove_steer_entry("s1", "u1", "e1") is True
        assert await peek_steer_queue(lease) == []

    @pytest.mark.asyncio
    async def test_remove_unknown_entry_is_noop(self, sessions_metadata_table):
        await acquire_session_lease("s1", "u1")
        assert await remove_steer_entry("s1", "u1", "nope") is False

    @pytest.mark.asyncio
    async def test_queue_entry_cap_is_enforced(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        for i in range(STEER_QUEUE_MAX_ENTRIES):
            await request_session_steer("s1", "u1", text="x", entry_id=f"e{i}")
        with pytest.raises(SteerQueueFullError):
            await request_session_steer("s1", "u1", text="x", entry_id="over")
        assert len(await peek_steer_queue(lease)) == STEER_QUEUE_MAX_ENTRIES

    @pytest.mark.asyncio
    async def test_queue_size_cap_is_enforced(self, sessions_metadata_table):
        await acquire_session_lease("s1", "u1")
        await request_session_steer("s1", "u1", text="x" * (STEER_QUEUE_MAX_CHARS - 10), entry_id="e1")
        with pytest.raises(SteerQueueFullError):
            await request_session_steer("s1", "u1", text="y" * 100, entry_id="e2")

    @pytest.mark.asyncio
    async def test_release_deletes_the_inbox_with_the_lease(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await request_session_steer("s1", "u1", text="hi", entry_id="e1")
        await release_session_lease(lease)
        # No separate GC path: an unconsumed inbox cannot outlive its turn.
        assert _lease_item(sessions_metadata_table) is None

    @pytest.mark.asyncio
    async def test_cancel_and_steer_coexist_on_the_row(self, sessions_metadata_table):
        """Cancel beats steering: the stop is observed, the inbox is left alone.

        The SPA's queue entry survives the stop and the user can resend it.
        """
        lease = await acquire_session_lease("s1", "u1")
        await request_session_steer("s1", "u1", text="hi", entry_id="e1")
        await request_session_cancel("s1", "u1")

        assert await renew_session_lease(lease) is True
        assert [e["id"] for e in await peek_steer_queue(lease)] == ["e1"]


class TestSteerSeeding:
    """Carrying queued follow-ups into a turn that is just starting.

    The paused-turn path (docs/specs/mid-turn-steering.md): a turn paused for
    consent has no running loop to steer, and the pause releases its lease —
    inbox and all. The resume request carries the entries and they are seeded
    onto the resumed turn's lease, where the ordinary hook picks them up.
    """

    @pytest.mark.asyncio
    async def test_seeded_entries_are_peekable_by_the_new_turn(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        assert await seed_steer_queue(lease, [{"id": "e1", "text": "use the other file"}]) == 1

        entries = await peek_steer_queue(lease)
        assert [e["id"] for e in entries] == ["e1"]
        assert entries[0]["text"] == "use the other file"

    @pytest.mark.asyncio
    async def test_seeding_replaces_rather_than_appends(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await seed_steer_queue(lease, [{"id": "e1", "text": "one"}])
        await seed_steer_queue(lease, [{"id": "e2", "text": "two"}])
        # A seed runs at turn start on a lease we just took, so there is nothing
        # legitimate to append to — appending would re-inject a retried resume's
        # first payload alongside its second.
        assert [e["id"] for e in await peek_steer_queue(lease)] == ["e2"]

    @pytest.mark.asyncio
    async def test_seeding_and_live_steering_coexist(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        await seed_steer_queue(lease, [{"id": "carried", "text": "from the pause"}])
        # The resumed turn is a live turn like any other; a steer typed during
        # it lands behind the carried one.
        await request_session_steer("s1", "u1", text="and this too", entry_id="live")

        assert [e["id"] for e in await peek_steer_queue(lease)] == ["carried", "live"]

    @pytest.mark.asyncio
    async def test_seeding_is_owner_scoped(self, sessions_metadata_table):
        first = await acquire_session_lease("s1", "u1")
        resumed = await acquire_session_lease("s1", "u1", force=True)
        assert await seed_steer_queue(first, [{"id": "e1", "text": "hi"}]) == 0
        assert await peek_steer_queue(resumed) == []

    @pytest.mark.asyncio
    async def test_nothing_to_seed_is_a_noop(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        assert await seed_steer_queue(lease, []) == 0
        assert await seed_steer_queue(None, [{"id": "e1", "text": "hi"}]) == 0
        assert await peek_steer_queue(lease) == []

    @pytest.mark.asyncio
    async def test_malformed_entries_are_dropped(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        seeded = await seed_steer_queue(
            lease,
            [{"id": "", "text": "no id"}, {"id": "e2", "text": ""}, {"id": "e3", "text": "ok"}],
        )
        assert seeded == 1
        assert [e["id"] for e in await peek_steer_queue(lease)] == ["e3"]

    @pytest.mark.asyncio
    async def test_seeding_respects_the_entry_cap(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        seeded = await seed_steer_queue(
            lease,
            [{"id": f"e{i}", "text": "x"} for i in range(STEER_QUEUE_MAX_ENTRIES + 3)],
        )
        assert seeded == STEER_QUEUE_MAX_ENTRIES

    @pytest.mark.asyncio
    async def test_seeding_respects_the_size_cap(self, sessions_metadata_table):
        lease = await acquire_session_lease("s1", "u1")
        seeded = await seed_steer_queue(
            lease,
            [
                {"id": "e1", "text": "x" * (STEER_QUEUE_MAX_CHARS - 10)},
                {"id": "e2", "text": "y" * 100},
            ],
        )
        assert seeded == 1

    @pytest.mark.asyncio
    async def test_acquire_clears_a_previous_turns_inbox(self, sessions_metadata_table):
        """The reason seeding is safe at all.

        Owner-scoping hides a stale queue from `peek_steer_queue`, but seeding
        stamps `steerFor` to the NEW owner — which would make a previous turn's
        leftovers visible and inject them into a turn they were never meant for.
        Acquire clears the inbox so that cannot happen.
        """
        first = await acquire_session_lease("s1", "u1")
        await request_session_steer("s1", "u1", text="from the old turn", entry_id="stale")

        resumed = await acquire_session_lease("s1", "u1", force=True)
        item = _lease_item(sessions_metadata_table)
        assert "steerQueue" not in item
        assert "steerFor" not in item

        await seed_steer_queue(resumed, [{"id": "carried", "text": "from the pause"}])
        assert [e["id"] for e in await peek_steer_queue(resumed)] == ["carried"]

    @pytest.mark.asyncio
    async def test_a_retried_resume_seeds_fresh(self, sessions_metadata_table):
        """The acquire→seed order in the route is load-bearing.

        Acquire REMOVEs the inbox, so a duplicate resume's own seed is what
        applies — the previous attempt's entries cannot linger and be injected
        alongside them.
        """
        lease = await acquire_session_lease("s1", "u1")
        await seed_steer_queue(lease, [{"id": "carried", "text": "from the pause"}])

        retried = await acquire_session_lease("s1", "u1", force=True)
        assert await peek_steer_queue(retried) == []

        await seed_steer_queue(retried, [{"id": "carried", "text": "from the pause"}])
        assert [e["id"] for e in await peek_steer_queue(retried)] == ["carried"]
