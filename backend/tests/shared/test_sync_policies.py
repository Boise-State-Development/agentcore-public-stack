"""Sync policy repository tests (moto DynamoDB).

Covers the runaway-prevention data invariants:
- sparse DueSyncIndex (paused policies are physically absent)
- conditional re-arm (double-fired dispatcher tick is idempotent)
- breaker counters (failure streaks vs source-gone streaks)
- per-assistant cap and one-policy-per-source uniqueness
- delete cascades (per-source and per-assistant)
"""

import pytest

from apis.shared.sync_policies.models import DUE_INDEX_PK
from apis.shared.sync_policies.service import (
    DuplicateSyncPolicy,
    RunNowCooldown,
    SyncPolicyLimitExceeded,
    change_policy_interval,
    create_sync_policy,
    delete_reauth_marker,
    delete_sync_policies_for_assistant,
    delete_sync_policies_for_source,
    get_sync_policy,
    list_due_policies,
    list_sync_policies,
    put_reauth_marker,
    rearm_policy,
    record_sync_result,
    resume_inactive_policies,
    resume_reauth_policies,
    set_policy_state,
    trigger_run_now,
)

pytestmark = pytest.mark.asyncio

ASSISTANT_ID = "ast-abc123def456"
USER_ID = "user-1"

FUTURE = "2999-01-01T00:00:00+00:00Z"
PAST = "2000-01-01T00:00:00+00:00Z"


async def _make_policy(source_ref="doc-1", source_type="drive_file", interval="daily"):
    return await create_sync_policy(
        assistant_id=ASSISTANT_ID,
        source_type=source_type,
        source_ref=source_ref,
        interval=interval,
        created_by_user_id=USER_ID,
    )


class TestCreateAndGet:
    async def test_create_get_roundtrip(self, assistants_table):
        policy = await _make_policy()

        assert policy.policy_id.startswith("syn-")
        assert policy.state == "active"
        assert policy.next_sync_at is not None
        assert policy.consecutive_failures == 0

        fetched = await get_sync_policy(ASSISTANT_ID, policy.policy_id)
        assert fetched is not None
        assert fetched.source_ref == "doc-1"
        assert fetched.source_type == "drive_file"
        assert fetched.interval == "daily"
        assert fetched.created_by_user_id == USER_ID

    async def test_active_policy_has_due_index_keys(self, assistants_table):
        policy = await _make_policy()

        item = assistants_table.get_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"SYNCPOL#{policy.policy_id}"}
        )["Item"]
        assert item["GSI4_PK"] == DUE_INDEX_PK
        assert item["GSI4_SK"] == f"{policy.next_sync_at}#{policy.policy_id}"

    async def test_get_missing_returns_none(self, assistants_table):
        assert await get_sync_policy(ASSISTANT_ID, "syn-missing") is None

    async def test_duplicate_source_rejected(self, assistants_table):
        await _make_policy(source_ref="doc-1")
        with pytest.raises(DuplicateSyncPolicy):
            await _make_policy(source_ref="doc-1")

    async def test_per_assistant_cap_enforced(self, assistants_table, monkeypatch):
        monkeypatch.setenv("KB_SYNC_MAX_POLICIES_PER_ASSISTANT", "2")
        await _make_policy(source_ref="doc-1")
        await _make_policy(source_ref="doc-2")
        with pytest.raises(SyncPolicyLimitExceeded):
            await _make_policy(source_ref="doc-3")


class TestDueSweep:
    async def test_due_query_returns_only_overdue(self, assistants_table):
        overdue = await _make_policy(source_ref="doc-1")
        pending = await _make_policy(source_ref="doc-2")

        # Force one policy overdue and keep one in the future
        assert await set_policy_state(ASSISTANT_ID, overdue.policy_id, "active", next_sync_at=PAST)
        assert await set_policy_state(ASSISTANT_ID, pending.policy_id, "active", next_sync_at=FUTURE)

        due = await list_due_policies()
        assert [p.policy_id for p in due] == [overdue.policy_id]

    async def test_due_query_respects_limit(self, assistants_table):
        for i in range(3):
            policy = await _make_policy(source_ref=f"doc-{i}")
            await set_policy_state(ASSISTANT_ID, policy.policy_id, "active", next_sync_at=PAST)

        due = await list_due_policies(limit=2)
        assert len(due) == 2

    async def test_paused_policy_leaves_due_index(self, assistants_table):
        policy = await _make_policy()
        await set_policy_state(ASSISTANT_ID, policy.policy_id, "active", next_sync_at=PAST)

        assert await set_policy_state(
            ASSISTANT_ID, policy.policy_id, "paused_error", state_reason="source no longer accessible"
        )

        # Sparse index: paused policy is physically absent, not filtered
        assert await list_due_policies() == []
        item = assistants_table.get_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"SYNCPOL#{policy.policy_id}"}
        )["Item"]
        assert "GSI4_PK" not in item
        assert "GSI4_SK" not in item
        assert item["stateReason"] == "source no longer accessible"

    async def test_reactivation_rejoins_due_index_and_clears_reason(self, assistants_table):
        policy = await _make_policy()
        await set_policy_state(ASSISTANT_ID, policy.policy_id, "paused_reauth", state_reason="reconnect Google Drive")
        assert await set_policy_state(ASSISTANT_ID, policy.policy_id, "active", next_sync_at=PAST)

        due = await list_due_policies()
        assert [p.policy_id for p in due] == [policy.policy_id]
        assert due[0].state_reason is None

    async def test_activate_without_next_sync_at_raises(self, assistants_table):
        policy = await _make_policy()
        with pytest.raises(ValueError):
            await set_policy_state(ASSISTANT_ID, policy.policy_id, "active")

    async def test_set_state_on_missing_policy_returns_false(self, assistants_table):
        assert not await set_policy_state(ASSISTANT_ID, "syn-missing", "paused_user")


class TestRearm:
    async def test_rearm_wins_with_expected_value(self, assistants_table):
        policy = await _make_policy()

        assert await rearm_policy(ASSISTANT_ID, policy.policy_id, policy.next_sync_at, FUTURE)

        updated = await get_sync_policy(ASSISTANT_ID, policy.policy_id)
        assert updated.next_sync_at == FUTURE
        item = assistants_table.get_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"SYNCPOL#{policy.policy_id}"}
        )["Item"]
        assert item["GSI4_SK"] == f"{FUTURE}#{policy.policy_id}"

    async def test_rearm_loses_on_stale_expected_value(self, assistants_table):
        policy = await _make_policy()
        assert await rearm_policy(ASSISTANT_ID, policy.policy_id, policy.next_sync_at, FUTURE)

        # Second dispatcher with the stale pre-rearm value must lose
        assert not await rearm_policy(ASSISTANT_ID, policy.policy_id, policy.next_sync_at, PAST)
        updated = await get_sync_policy(ASSISTANT_ID, policy.policy_id)
        assert updated.next_sync_at == FUTURE


class TestRunResults:
    async def test_failure_increments_streak(self, assistants_table):
        policy = await _make_policy()

        await record_sync_result(ASSISTANT_ID, policy.policy_id, "failed")
        await record_sync_result(ASSISTANT_ID, policy.policy_id, "failed")

        updated = await get_sync_policy(ASSISTANT_ID, policy.policy_id)
        assert updated.consecutive_failures == 2
        assert updated.consecutive_not_found == 0
        assert updated.last_result == "failed"

    async def test_not_found_failure_increments_both_streaks(self, assistants_table):
        policy = await _make_policy()

        await record_sync_result(ASSISTANT_ID, policy.policy_id, "failed", not_found=True)

        updated = await get_sync_policy(ASSISTANT_ID, policy.policy_id)
        assert updated.consecutive_failures == 1
        assert updated.consecutive_not_found == 1

    async def test_success_resets_streaks(self, assistants_table):
        policy = await _make_policy()
        await record_sync_result(ASSISTANT_ID, policy.policy_id, "failed", not_found=True)

        await record_sync_result(ASSISTANT_ID, policy.policy_id, "unchanged")

        updated = await get_sync_policy(ASSISTANT_ID, policy.policy_id)
        assert updated.consecutive_failures == 0
        assert updated.consecutive_not_found == 0
        assert updated.last_result == "unchanged"
        assert updated.last_sync_at is not None
        assert updated.sync_run_started_at is None


class TestDeleteCascades:
    async def test_delete_for_source_removes_only_matching(self, assistants_table):
        keep = await _make_policy(source_ref="doc-keep")
        await _make_policy(source_ref="doc-gone")

        deleted = await delete_sync_policies_for_source(ASSISTANT_ID, "doc-gone")

        assert deleted == 1
        remaining = await list_sync_policies(ASSISTANT_ID)
        assert [p.policy_id for p in remaining] == [keep.policy_id]

    async def test_delete_for_assistant_removes_all(self, assistants_table):
        await _make_policy(source_ref="doc-1")
        await _make_policy(source_ref="crawl-1", source_type="web_crawl")

        deleted = await delete_sync_policies_for_assistant(ASSISTANT_ID)

        assert deleted == 2
        assert await list_sync_policies(ASSISTANT_ID) == []
        assert await list_due_policies() == []


class TestIntervalChange:
    async def test_active_policy_rearms_and_moves_due_key(self, assistants_table):
        policy = await _make_policy(interval="daily")
        old_next = policy.next_sync_at

        updated = await change_policy_interval(ASSISTANT_ID, policy.policy_id, "monthly")

        assert updated.interval == "monthly"
        assert updated.next_sync_at > old_next  # one *new* interval out
        item = assistants_table.get_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"SYNCPOL#{policy.policy_id}"}
        )["Item"]
        assert item["GSI4_SK"] == f"{updated.next_sync_at}#{policy.policy_id}"

    async def test_paused_policy_remembers_interval_without_due_keys(self, assistants_table):
        policy = await _make_policy(interval="daily")
        await set_policy_state(ASSISTANT_ID, policy.policy_id, "paused_user")

        updated = await change_policy_interval(ASSISTANT_ID, policy.policy_id, "weekly")

        assert updated.interval == "weekly"
        assert updated.state == "paused_user"
        item = assistants_table.get_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"SYNCPOL#{policy.policy_id}"}
        )["Item"]
        assert "GSI4_SK" not in item  # still invisible to the dispatcher

    async def test_missing_policy_returns_none(self, assistants_table):
        assert await change_policy_interval(ASSISTANT_ID, "syn-missing", "weekly") is None


class TestRunNow:
    async def test_makes_policy_due_immediately(self, assistants_table):
        policy = await _make_policy(interval="daily")
        assert await list_due_policies() == []  # created one interval out

        updated = await trigger_run_now(ASSISTANT_ID, policy.policy_id)

        assert updated.last_manual_run_at is not None
        assert updated.next_sync_at == updated.last_manual_run_at
        due = await list_due_policies()
        assert [p.policy_id for p in due] == [policy.policy_id]

    async def test_second_trigger_within_cooldown_rejected(self, assistants_table):
        policy = await _make_policy()
        await trigger_run_now(ASSISTANT_ID, policy.policy_id)

        with pytest.raises(RunNowCooldown):
            await trigger_run_now(ASSISTANT_ID, policy.policy_id)

    async def test_trigger_allowed_after_cooldown_expires(self, assistants_table):
        policy = await _make_policy()
        # Simulate an old manual run: stamp beyond the cooldown window.
        assistants_table.update_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"SYNCPOL#{policy.policy_id}"},
            UpdateExpression="SET lastManualRunAt = :old",
            ExpressionAttributeValues={":old": PAST},
        )

        updated = await trigger_run_now(ASSISTANT_ID, policy.policy_id)

        assert updated.last_manual_run_at > PAST

    async def test_paused_policy_rejected(self, assistants_table):
        policy = await _make_policy()
        await set_policy_state(ASSISTANT_ID, policy.policy_id, "paused_user")

        with pytest.raises(ValueError):
            await trigger_run_now(ASSISTANT_ID, policy.policy_id)

    async def test_missing_policy_raises_key_error(self, assistants_table):
        with pytest.raises(KeyError):
            await trigger_run_now(ASSISTANT_ID, "syn-missing")


class TestReauthMarkers:
    async def test_fresh_consent_resumes_matching_provider_due_now(self, assistants_table):
        policy = await _make_policy()
        await set_policy_state(ASSISTANT_ID, policy.policy_id, "paused_reauth", state_reason="Reconnect")
        await put_reauth_marker(USER_ID, ASSISTANT_ID, policy.policy_id, "google-workspace")

        resumed = await resume_reauth_policies(USER_ID, "google-workspace")

        assert resumed == 1
        updated = await get_sync_policy(ASSISTANT_ID, policy.policy_id)
        assert updated.state == "active"
        assert [p.policy_id for p in await list_due_policies()] == [policy.policy_id]
        # Marker consumed
        marker = assistants_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SYNCREAUTH#{policy.policy_id}"}
        ).get("Item")
        assert marker is None

    async def test_other_providers_markers_left_alone(self, assistants_table):
        policy = await _make_policy()
        await set_policy_state(ASSISTANT_ID, policy.policy_id, "paused_reauth")
        await put_reauth_marker(USER_ID, ASSISTANT_ID, policy.policy_id, "microsoft-365")

        resumed = await resume_reauth_policies(USER_ID, "google-workspace")

        assert resumed == 0
        updated = await get_sync_policy(ASSISTANT_ID, policy.policy_id)
        assert updated.state == "paused_reauth"
        marker = assistants_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SYNCREAUTH#{policy.policy_id}"}
        ).get("Item")
        assert marker is not None  # still waiting on its own provider

    async def test_stale_marker_cleaned_without_resuming(self, assistants_table):
        # Marker outlived its policy's pause (user resumed another way, or
        # the policy was deleted): resume must re-verify, not trust it.
        active = await _make_policy(source_ref="doc-active")
        await put_reauth_marker(USER_ID, ASSISTANT_ID, active.policy_id, "google-workspace")
        await put_reauth_marker(USER_ID, ASSISTANT_ID, "syn-deleted", "google-workspace")

        resumed = await resume_reauth_policies(USER_ID, "google-workspace")

        assert resumed == 0
        for policy_id in (active.policy_id, "syn-deleted"):
            marker = assistants_table.get_item(
                Key={"PK": f"USER#{USER_ID}", "SK": f"SYNCREAUTH#{policy_id}"}
            ).get("Item")
            assert marker is None

    async def test_delete_marker_is_idempotent(self, assistants_table):
        await delete_reauth_marker(USER_ID, "syn-never-existed")  # no raise


class TestResumeInactive:
    async def test_resumes_only_inactivity_pauses(self, assistants_table):
        dormant = await _make_policy(source_ref="doc-dormant")
        user_paused = await _make_policy(source_ref="doc-user")
        await set_policy_state(ASSISTANT_ID, dormant.policy_id, "paused_inactive")
        await set_policy_state(ASSISTANT_ID, user_paused.policy_id, "paused_user")

        resumed = await resume_inactive_policies(ASSISTANT_ID)

        assert resumed == 1
        assert (await get_sync_policy(ASSISTANT_ID, dormant.policy_id)).state == "active"
        assert (await get_sync_policy(ASSISTANT_ID, user_paused.policy_id)).state == "paused_user"
        assert [p.policy_id for p in await list_due_policies()] == [dormant.policy_id]

    async def test_no_inactive_policies_is_noop(self, assistants_table):
        await _make_policy()
        assert await resume_inactive_policies(ASSISTANT_ID) == 0


class TestTimestampFormat:
    """The stored timestamps are surfaced verbatim to the SPA, where
    ``new Date(iso)`` parses them. A ``…+00:00Z`` string (offset AND Z) is
    invalid ISO 8601 → Invalid Date → a blank sync-status line. Guard the
    generators against that regression."""

    async def test_generated_timestamps_are_valid_iso(self, assistants_table):
        from datetime import datetime

        from apis.shared.sync_policies.service import (
            _get_current_timestamp,
            compute_next_sync_at,
        )

        policy = await _make_policy()
        for ts in (
            _get_current_timestamp(),
            compute_next_sync_at("weekly"),
            policy.next_sync_at,
        ):
            assert ts.endswith("Z"), ts
            assert "+00:00" not in ts, ts
            # Round-trips to an aware datetime the way JS `Date` accepts.
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            assert parsed.tzinfo is not None
