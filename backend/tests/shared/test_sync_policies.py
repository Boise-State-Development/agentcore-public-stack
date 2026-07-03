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
    SyncPolicyLimitExceeded,
    create_sync_policy,
    delete_sync_policies_for_assistant,
    delete_sync_policies_for_source,
    get_sync_policy,
    list_due_policies,
    list_sync_policies,
    rearm_policy,
    record_sync_result,
    set_policy_state,
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
