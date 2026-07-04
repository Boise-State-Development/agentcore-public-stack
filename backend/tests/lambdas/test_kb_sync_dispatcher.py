"""KB sync dispatcher tests (moto DynamoDB, stubbed worker invoke).

Sources are created through the REAL assistants/documents services so the
dispatcher's raw adjacency-list key reads are cross-checked against the
actual storage schema — if a key pattern drifts, these tests break loudly.
"""

from datetime import datetime, timedelta, timezone

import pytest

from apis.app_api.documents.services.document_service import create_document
from apis.app_api.kb_sync import dispatcher
from apis.shared.assistants.service import create_assistant
from apis.shared.sync_policies.service import (
    create_sync_policy,
    get_sync_policy,
    list_sync_policies,
    set_policy_state,
)

pytestmark = pytest.mark.asyncio

USER_ID = "user-1"
PAST = "2000-01-01T00:00:00+00:00Z"


@pytest.fixture(autouse=True)
def kb_sync_env(monkeypatch):
    monkeypatch.setenv("KB_SYNC_ENABLED", "true")
    monkeypatch.setenv("KB_SYNC_WORKER_FUNCTION_NAME", "test-kb-sync-worker")


@pytest.fixture()
def invoked_workers(monkeypatch):
    """Capture worker invocations instead of calling Lambda."""
    payloads = []
    monkeypatch.setattr(dispatcher, "_invoke_worker", payloads.append)
    return payloads


@pytest.fixture(autouse=True)
def no_metrics(monkeypatch):
    monkeypatch.setattr(dispatcher, "_emit_metrics", lambda counts: None)


async def _make_assistant():
    assistant = await create_assistant(
        owner_id=USER_ID,
        owner_name="Test User",
        name="Synced Assistant",
        description="d",
        instructions="i",
        vector_index_id="assistants-index",
    )
    return assistant.assistant_id


async def _make_document(assistant_id, document_id="doc-1"):
    doc = await create_document(
        assistant_id=assistant_id,
        filename="report.pdf",
        content_type="application/pdf",
        size_bytes=10,
        s3_key=f"assistants/{assistant_id}/documents/{document_id}/report.pdf",
        document_id=document_id,
    )
    return doc.document_id


async def _make_due_policy(assistant_id, source_ref="doc-1"):
    policy = await create_sync_policy(
        assistant_id=assistant_id,
        source_type="drive_file",
        source_ref=source_ref,
        interval="daily",
        created_by_user_id=USER_ID,
    )
    await set_policy_state(assistant_id, policy.policy_id, "active", next_sync_at=PAST)
    return await get_sync_policy(assistant_id, policy.policy_id)


class TestKillSwitch:
    async def test_disabled_tick_is_noop(self, assistants_table, invoked_workers, monkeypatch):
        monkeypatch.setenv("KB_SYNC_ENABLED", "false")
        assistant_id = await _make_assistant()
        await _make_document(assistant_id)
        await _make_due_policy(assistant_id)

        counts = await dispatcher.dispatch_once()

        assert counts["PoliciesDue"] == 0
        assert invoked_workers == []


class TestLiveness:
    async def test_missing_assistant_deletes_policy(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        await _make_document(assistant_id)
        await _make_due_policy(assistant_id)
        # Simulate a delete path that missed the cascade
        assistants_table.delete_item(Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"})

        counts = await dispatcher.dispatch_once()

        assert counts["OrphansDeleted"] == 1
        assert invoked_workers == []
        assert await list_sync_policies(assistant_id) == []

    async def test_missing_source_deletes_policy(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        await _make_due_policy(assistant_id, source_ref="doc-never-created")

        counts = await dispatcher.dispatch_once()

        assert counts["OrphansDeleted"] == 1
        assert await list_sync_policies(assistant_id) == []

    async def test_deleting_source_deletes_policy(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        doc_id = await _make_document(assistant_id)
        await _make_due_policy(assistant_id, source_ref=doc_id)
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{doc_id}"},
            UpdateExpression="SET #s = :deleting",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":deleting": "deleting"},
        )

        counts = await dispatcher.dispatch_once()

        assert counts["OrphansDeleted"] == 1
        assert invoked_workers == []


class TestCircuitBreaker:
    async def _set_counter(self, table, assistant_id, policy_id, field, value):
        table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"SYNCPOL#{policy_id}"},
            UpdateExpression=f"SET {field} = :v",
            ExpressionAttributeValues={":v": value},
        )

    async def test_not_found_streak_pauses(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        await _make_document(assistant_id)
        policy = await _make_due_policy(assistant_id)
        await self._set_counter(assistants_table, assistant_id, policy.policy_id, "consecutiveNotFound", 2)

        counts = await dispatcher.dispatch_once()

        assert counts["PausedBreaker"] == 1
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.state == "paused_error"
        assert "accessible" in updated.state_reason
        assert invoked_workers == []

    async def test_failure_streak_pauses(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        await _make_document(assistant_id)
        policy = await _make_due_policy(assistant_id)
        await self._set_counter(assistants_table, assistant_id, policy.policy_id, "consecutiveFailures", 5)

        counts = await dispatcher.dispatch_once()

        assert counts["PausedBreaker"] == 1
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.state == "paused_error"
        assert invoked_workers == []

    async def test_backoff_scales_with_failures(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        await _make_document(assistant_id)
        policy = await _make_due_policy(assistant_id)
        await self._set_counter(assistants_table, assistant_id, policy.policy_id, "consecutiveFailures", 2)

        await dispatcher.dispatch_once()

        # daily * 2^2 = 4 days out
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        next_dt = datetime.fromisoformat(updated.next_sync_at.rstrip("Z"))
        expected = datetime.now(timezone.utc) + timedelta(days=4)
        assert abs((next_dt - expected).total_seconds()) < 300


class TestInactivityPause:
    async def test_stale_assistant_pauses_policy(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        await _make_document(assistant_id)
        policy = await _make_due_policy(assistant_id)
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"},
            UpdateExpression="SET createdAt = :old, updatedAt = :old",
            ExpressionAttributeValues={":old": "2020-01-01T00:00:00+00:00Z"},
        )

        counts = await dispatcher.dispatch_once()

        assert counts["PausedInactive"] == 1
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.state == "paused_inactive"
        assert invoked_workers == []

    async def test_recent_last_used_at_keeps_policy_active(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        await _make_document(assistant_id)
        await _make_due_policy(assistant_id)
        now_ts = datetime.now(timezone.utc).isoformat() + "Z"
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"},
            UpdateExpression="SET createdAt = :old, updatedAt = :old, lastUsedAt = :now",
            ExpressionAttributeValues={":old": "2020-01-01T00:00:00+00:00Z", ":now": now_ts},
        )

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1


class TestDispatch:
    async def test_happy_path_dispatches_and_rearms(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        doc_id = await _make_document(assistant_id)
        policy = await _make_due_policy(assistant_id, source_ref=doc_id)

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1
        assert invoked_workers == [
            {
                "policyId": policy.policy_id,
                "assistantId": assistant_id,
                "sourceType": "drive_file",
                "sourceRef": doc_id,
            }
        ]
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.next_sync_at > datetime.now(timezone.utc).isoformat()
        assert updated.sync_run_started_at is not None

    async def test_dispatched_policy_not_redispatched_same_day(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        doc_id = await _make_document(assistant_id)
        await _make_due_policy(assistant_id, source_ref=doc_id)

        await dispatcher.dispatch_once()
        counts = await dispatcher.dispatch_once()

        # Re-armed a day out — second tick sees nothing due
        assert counts["PoliciesDue"] == 0
        assert len(invoked_workers) == 1

    async def test_fresh_run_stamp_skips_without_rearm(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        doc_id = await _make_document(assistant_id)
        policy = await _make_due_policy(assistant_id, source_ref=doc_id)
        now_ts = datetime.now(timezone.utc).isoformat() + "Z"
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"SYNCPOL#{policy.policy_id}"},
            UpdateExpression="SET syncRunStartedAt = :now",
            ExpressionAttributeValues={":now": now_ts},
        )

        counts = await dispatcher.dispatch_once()

        assert counts["InFlightSkipped"] == 1
        assert invoked_workers == []
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.next_sync_at == PAST  # not re-armed; retried next tick

    async def test_stale_run_stamp_is_overwritten_and_dispatched(self, assistants_table, invoked_workers):
        assistant_id = await _make_assistant()
        doc_id = await _make_document(assistant_id)
        policy = await _make_due_policy(assistant_id, source_ref=doc_id)
        assistants_table.update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"SYNCPOL#{policy.policy_id}"},
            UpdateExpression="SET syncRunStartedAt = :old",
            ExpressionAttributeValues={":old": "2020-01-01T00:00:00+00:00Z"},
        )

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.sync_run_started_at > "2020-01-01"

    async def test_broken_policy_does_not_starve_sweep(self, assistants_table, invoked_workers, monkeypatch):
        assistant_id = await _make_assistant()
        doc_a = await _make_document(assistant_id, document_id="doc-a")
        doc_b = await _make_document(assistant_id, document_id="doc-b")
        await _make_due_policy(assistant_id, source_ref=doc_a)
        await _make_due_policy(assistant_id, source_ref=doc_b)

        real_get_source = dispatcher._get_source_item
        calls = {"n": 0}

        def flaky_get_source(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real_get_source(*args, **kwargs)

        monkeypatch.setattr(dispatcher, "_get_source_item", flaky_get_source)

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1


class TestWorkerStub:
    async def test_stub_records_skipped_and_clears_stamp(self, assistants_table):
        from apis.app_api.kb_sync import worker

        assistant_id = await _make_assistant()
        doc_id = await _make_document(assistant_id)
        policy = await _make_due_policy(assistant_id, source_ref=doc_id)

        result = await worker.run_sync(
            {"policyId": policy.policy_id, "assistantId": assistant_id, "sourceType": "drive_file", "sourceRef": doc_id}
        )

        assert result["result"] == "skipped"
        updated = await get_sync_policy(assistant_id, policy.policy_id)
        assert updated.last_result == "skipped"
        assert updated.sync_run_started_at is None
