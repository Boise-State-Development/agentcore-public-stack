"""Retention pruning of session rows (docs/specs/conversation-search.md §3, PR-2c).

DynamoDB and SSM are moto. The cascade is a recorder: what it does is the
route's business and is covered by the route tests and
``test_session_delete_cascade.py``; here the question is which sessions reach
it, and when.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import boto3
import pytest
from moto import mock_aws

from apis.app_api.sessions.services import retention_pruner as rp

REGION = "us-east-1"
TABLE = "test-sessions-metadata"
PREFIX = "test-prefix"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=400)).isoformat()
OLDER = (NOW - timedelta(days=500)).isoformat()
RECENT = (NOW - timedelta(days=10)).isoformat()


@pytest.fixture()
def table(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "DYNAMODB_SESSIONS_METADATA_TABLE_NAME": TABLE,
        "PROJECT_PREFIX": PREFIX,
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "CONVERSATION_RETENTION_DAYS",
        "CONVERSATION_RETENTION_PRUNES_SESSIONS",
        "CONVERSATION_RETENTION_PRUNE_ARMED",
    ):
        monkeypatch.delenv(name, raising=False)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI_PK", "GSI_SK")
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "SessionLookupIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _session(table, session_id: str, last: Any = OLD, user_id: str = "user-a", **extra) -> None:
    item: Dict[str, Any] = {
        "PK": f"USER#{user_id}",
        "SK": f"S#{session_id}",
        "GSI_PK": f"SESSION#{session_id}",
        "GSI_SK": "META",
        "userId": user_id,
        "sessionId": session_id,
        "status": "active",
        "title": "A conversation",
        **extra,
    }
    if last is not None:
        item["lastMessageAt"] = last
    table.put_item(Item=item)


class Recorder:
    def __init__(self, table) -> None:
        self.table = table
        self.deleted: List[tuple] = []
        self.cascaded: List[tuple] = []
        self.sleeps: List[float] = []
        self.on_recheck = None

    async def recheck(self, session_id, user_id):
        if self.on_recheck:
            self.on_recheck(session_id)
        return self.table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"S#{session_id}"}).get("Item")

    async def delete(self, user_id, session_id):
        self.deleted.append((user_id, session_id))
        self.table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"S#{session_id}"},
            UpdateExpression="SET #s = :d, deleted = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": "deleted", ":t": True},
        )
        return True

    async def cascade(self, user_id, session_id):
        self.cascaded.append((user_id, session_id))
        return []

    async def sleep(self, seconds):
        self.sleeps.append(seconds)


def _prune(table, rec: Recorder, *, allowed=True, **kwargs):
    return asyncio.run(
        rp.prune(
            table=table,
            recheck=rec.recheck,
            delete=rec.delete,
            cascade=rec.cascade,
            allowed=allowed,
            reason="test",
            now=NOW,
            sleep=rec.sleep,
            **kwargs,
        )
    )


class TestWhoIsPruned:
    def test_past_retention_is_deleted_through_the_cascade(self, table):
        _session(table, "old")
        _session(table, "recent", last=RECENT)
        rec = Recorder(table)

        report = _prune(table, rec)

        assert rec.deleted == [("user-a", "old")]
        assert rec.cascaded == [("user-a", "old")]
        assert report.deleted == 1
        assert report.candidates == 1

    def test_archived_sessions_past_retention_go_too(self, table):
        """Archiving keeps a conversation; it does not keep its expired content."""
        _session(table, "archived", status="archived")
        rec = Recorder(table)

        _prune(table, rec)

        assert rec.deleted == [("user-a", "archived")]

    def test_continuable_turn_is_never_pruned(self, table):
        _session(table, "cont", lastTurnContinuable=True)
        rec = Recorder(table)

        report = _prune(table, rec)

        assert rec.deleted == []
        assert report.held == {"continuable": 1}

    def test_pending_interrupt_is_never_pruned(self, table):
        _session(table, "paused", pendingInterrupts=[{"interruptId": "i-1"}])
        rec = Recorder(table)

        report = _prune(table, rec)

        assert rec.deleted == []
        assert report.held == {"pending_interrupt": 1}

    @pytest.mark.parametrize("last", [None, "", "not-a-date"])
    def test_missing_last_message_at_is_skipped_not_treated_as_old(self, table, last):
        _session(table, "unknown", last=last)
        rec = Recorder(table)

        report = _prune(table, rec)

        assert rec.deleted == []
        assert report.held == {"missing_last_message_at": 1}

    def test_deleted_rows_and_other_row_types_are_not_scanned_as_sessions(self, table):
        _session(table, "gone", status="deleted", deleted=True)
        table.put_item(Item={"PK": "USER#user-a", "SK": "S#DELETED#2025-01-01#legacy", "lastMessageAt": OLD})
        table.put_item(Item={"PK": "USER#user-a", "SK": "C#2025-01-01#call", "lastMessageAt": OLD})
        table.put_item(Item={"PK": "SESSION#x", "SK": "S#x", "lastMessageAt": OLD})
        rec = Recorder(table)

        report = _prune(table, rec)

        assert rec.deleted == []
        assert report.scanned == 0

    def test_retention_setting_moves_the_cutoff(self, table, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "5")
        _session(table, "ten-days", last=RECENT)
        rec = Recorder(table)

        _prune(table, rec)

        assert rec.deleted == [("user-a", "ten-days")]

    def test_a_row_that_moved_since_the_scan_is_left_alone(self, table):
        _session(table, "revived")
        rec = Recorder(table)
        rec.on_recheck = lambda sid: _session(table, sid, last=RECENT)

        report = _prune(table, rec)

        assert rec.deleted == []
        assert report.skipped_on_recheck == 1


class TestThrottle:
    def test_oldest_first_capped_and_paced(self, table):
        _session(table, "a", last=OLD)
        _session(table, "b", last=OLDER)
        _session(table, "c", last=(NOW - timedelta(days=450)).isoformat())
        rec = Recorder(table)

        report = _prune(table, rec, limit=2, sleep_seconds=0.5)

        assert [sid for _, sid in rec.deleted] == ["b", "c"]
        assert rec.sleeps == [0.5]
        assert report.limit_reached is True
        assert report.candidates == 3


class TestDryRun:
    def test_reports_count_and_range_and_deletes_nothing(self, table):
        _session(table, "a", last=OLD)
        _session(table, "b", last=OLDER)
        _session(table, "recent", last=RECENT)
        rec = Recorder(table)

        report = _prune(table, rec, allowed=False)

        assert rec.deleted == [] and rec.cascaded == []
        assert report.mode == "dry-run"
        assert report.candidates == 2
        assert report.oldest_last_message_at == datetime.fromisoformat(OLDER).isoformat()
        assert report.newest_last_message_at == datetime.fromisoformat(OLD).isoformat()


class TestArming:
    def test_unarmed_is_a_dry_run(self, table, monkeypatch):
        assert rp.deletes_allowed(365, {"retentionDays": 365}) == (False, "not armed (CONVERSATION_RETENTION_PRUNE_ARMED)")

    @pytest.mark.parametrize("value", ["", "false", "0", "no"])
    def test_arming_needs_an_affirmative_value(self, table, monkeypatch, value):
        monkeypatch.setenv("CONVERSATION_RETENTION_PRUNE_ARMED", value)
        assert rp.deletes_allowed(365, {"retentionDays": 365})[0] is False

    def test_armed_first_run_is_still_dry(self, table, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RETENTION_PRUNE_ARMED", "true")
        allowed, reason = rp.deletes_allowed(365, None)
        assert allowed is False and "first run" in reason

    def test_armed_after_a_dry_run_deletes(self, table, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RETENTION_PRUNE_ARMED", "true")
        assert rp.deletes_allowed(365, {"retentionDays": 365}) == (True, "armed")

    def test_a_changed_retention_setting_needs_a_new_dry_run(self, table, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RETENTION_PRUNE_ARMED", "true")
        allowed, reason = rp.deletes_allowed(30, {"retentionDays": 365})
        assert allowed is False and "changed" in reason


class TestRun:
    """The production wiring: real SessionService soft delete, SSM record."""

    @pytest.fixture()
    def cascade(self, monkeypatch):
        calls: List[tuple] = []

        class FakeCascade:
            async def run(self, user_id, session_id):
                calls.append((user_id, session_id))
                return []

        from apis.app_api.sessions.services import delete_cascade

        monkeypatch.setattr(delete_cascade, "default_cascade", lambda _service: FakeCascade())
        return calls

    def _record(self):
        value = boto3.client("ssm", region_name=REGION).get_parameter(
            Name=f"/{PREFIX}/conversation-retention/prune-dry-run"
        )["Parameter"]["Value"]
        return json.loads(value)

    def test_first_armed_run_is_dry_and_the_second_deletes(self, table, cascade, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RETENTION_PRUNE_ARMED", "true")
        _session(table, "old")

        first = asyncio.run(rp.run(limit=10, sleep_seconds=0))
        assert first.mode == "dry-run"
        assert cascade == []
        assert table.get_item(Key={"PK": "USER#user-a", "SK": "S#old"})["Item"]["status"] == "active"
        assert self._record()["candidates"] == 1
        assert self._record()["retentionDays"] == 365

        second = asyncio.run(rp.run(limit=10, sleep_seconds=0))
        assert second.mode == "live"
        assert second.deleted == 1
        assert cascade == [("user-a", "old")]
        row = table.get_item(Key={"PK": "USER#user-a", "SK": "S#old"})["Item"]
        assert row["status"] == "deleted"

    def test_unarmed_never_deletes(self, table, cascade):
        _session(table, "old")
        for _ in range(2):
            report = asyncio.run(rp.run(limit=10, sleep_seconds=0))
            assert report.mode == "dry-run"
        assert cascade == []

    def test_opted_out_does_not_scan(self, table, cascade, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RETENTION_PRUNES_SESSIONS", "false")
        monkeypatch.setenv("CONVERSATION_RETENTION_PRUNE_ARMED", "true")
        _session(table, "old")

        report = asyncio.run(rp.run(limit=10, sleep_seconds=0))

        assert report.mode == "off"
        assert report.scanned == 0
        assert cascade == []

    def test_cli_dry_run_overrides_arming(self, table, cascade, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RETENTION_PRUNE_ARMED", "true")
        _session(table, "old")
        asyncio.run(rp.run(limit=10, sleep_seconds=0))  # records the dry run

        report = asyncio.run(rp.run(force_dry_run=True, limit=10, sleep_seconds=0))

        assert report.mode == "dry-run"
        assert cascade == []


@pytest.mark.parametrize(
    "value,expected",
    [(None, True), ("", True), ("true", True), ("false", False), (" FALSE ", False)],
)
def test_prunes_sessions_is_default_on_with_a_kill_switch(monkeypatch, value, expected):
    from apis.shared.feature_flags import conversation_retention_prunes_sessions

    if value is None:
        monkeypatch.delenv("CONVERSATION_RETENTION_PRUNES_SESSIONS", raising=False)
    else:
        monkeypatch.setenv("CONVERSATION_RETENTION_PRUNES_SESSIONS", value)
    assert conversation_retention_prunes_sessions() is expected
