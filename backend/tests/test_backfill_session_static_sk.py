"""Tests for the Phase 2 static-SK backfill script (issue #175)."""

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backfill_session_static_sk import (  # noqa: E402
    build_static_item,
    count_remaining_legacy,
    is_ghost,
    maybe_set_marker,
    run,
)

REGION = "us-east-1"
TABLE = "test-sessions-metadata"


def _create_table(ddb):
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                   {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI4_PK", "AttributeType": "S"},
            {"AttributeName": "GSI4_SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "SessionRecencyIndex",
            "KeySchema": [{"AttributeName": "GSI4_PK", "KeyType": "HASH"},
                          {"AttributeName": "GSI4_SK", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _legacy_active(table, sid, la):
    table.put_item(Item={
        "PK": "USER#u1", "SK": f"S#ACTIVE#{la}#{sid}",
        "GSI_PK": f"SESSION#{sid}", "GSI_SK": "META",
        "sessionId": sid, "userId": "u1", "title": "T", "status": "active",
        "createdAt": "2026-01-01T00:00:00Z", "lastMessageAt": la, "messageCount": 1,
    })


def _legacy_deleted(table, sid, da):
    table.put_item(Item={
        "PK": "USER#u1", "SK": f"S#DELETED#{da}#{sid}",
        "GSI_PK": f"SESSION#{sid}", "GSI_SK": "META",
        "sessionId": sid, "userId": "u1", "title": "T", "status": "deleted",
        "createdAt": "2026-01-01T00:00:00Z", "lastMessageAt": "2026-01-01T00:00:00Z",
        "messageCount": 3, "deleted": True, "deletedAt": da,
    })


def _ghost(table, sid, la):
    # bare stub: no GSI_SK=META, no required fields
    table.put_item(Item={"PK": "USER#u1", "SK": f"S#ACTIVE#{la}#{sid}"})


def _static_active(table, sid, la):
    table.put_item(Item={
        "PK": "USER#u1", "SK": f"S#{sid}",
        "GSI_PK": f"SESSION#{sid}", "GSI_SK": "META",
        "GSI4_PK": "USER#u1", "GSI4_SK": f"{la}#{sid}",
        "sessionId": sid, "userId": "u1", "title": "T", "status": "active",
        "createdAt": "2026-01-01T00:00:00Z", "lastMessageAt": la, "messageCount": 1,
    })


def _meta_rows(table):
    return [i for i in table.scan()["Items"] if i.get("GSI_SK") == "META"]


class TestClassification:
    def test_is_ghost_detects_bare_stub(self):
        assert is_ghost({"PK": "USER#u1", "SK": "S#ACTIVE#2026#x"}) is True

    def test_is_ghost_missing_required_field(self):
        assert is_ghost({"GSI_SK": "META", "sessionId": "x"}) is True  # missing title etc.

    def test_valid_row_is_not_ghost(self):
        row = {"GSI_SK": "META", "sessionId": "x", "userId": "u", "title": "t",
               "status": "active", "createdAt": "c", "lastMessageAt": "l", "messageCount": 1}
        assert is_ghost(row) is False

    def test_build_static_item_active_gets_gsi4(self):
        row = {"PK": "USER#u1", "SK": "S#ACTIVE#2026-01-02T00:00:00Z#s1",
               "userId": "u1", "status": "active", "lastMessageAt": "2026-01-02T00:00:00Z"}
        out = build_static_item(row, "s1")
        assert out["SK"] == "S#s1"
        assert out["GSI4_PK"] == "USER#u1"
        assert out["GSI4_SK"] == "2026-01-02T00:00:00Z#s1"

    def test_build_static_item_deleted_no_gsi4(self):
        row = {"PK": "USER#u1", "SK": "S#DELETED#2026-01-02T00:00:00Z#s1",
               "userId": "u1", "status": "deleted", "lastMessageAt": "2026-01-01T00:00:00Z"}
        out = build_static_item(row, "s1")
        assert out["SK"] == "S#s1"
        assert "GSI4_PK" not in out
        assert out["status"] == "deleted" and out["deleted"] is True


class TestBackfillRun:
    @pytest.fixture()
    def table(self):
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name=REGION)
            yield _create_table(ddb)

    def test_dry_run_writes_nothing(self, table):
        _legacy_active(table, "s1", "2026-01-01T00:00:00Z")
        _ghost(table, "g1", "2026-01-02T00:00:00Z")
        before = table.scan()["Items"]

        stats = run(TABLE, REGION, apply=False, sleep=0, limit=None, set_marker=False)

        assert stats["migrated"] == 1 and stats["ghosts"] == 1
        assert table.scan()["Items"] == before  # unchanged

    def test_apply_migrates_active_deletes_ghost(self, table):
        _legacy_active(table, "s1", "2026-01-02T00:00:00Z")
        _legacy_deleted(table, "s2", "2026-01-03T00:00:00Z")
        _ghost(table, "g1", "2026-01-04T00:00:00Z")
        _static_active(table, "s3", "2026-01-05T00:00:00Z")  # already migrated

        stats = run(TABLE, REGION, apply=True, sleep=0, limit=None, set_marker=False)
        assert stats == {"migrated": 2, "ghosts": 1, "already_static": 0, "skipped_live_migrated": 0}

        items = table.scan()["Items"]
        # ghost gone, no legacy rows remain
        assert not any(i["SK"].startswith("S#ACTIVE#") for i in items)
        assert not any(i["SK"].startswith("S#DELETED#") for i in items)
        rows = {i["sessionId"]: i for i in _meta_rows(table)}
        assert rows["s1"]["SK"] == "S#s1" and rows["s1"]["GSI4_PK"] == "USER#u1"   # active → GSI4
        assert rows["s2"]["SK"] == "S#s2" and "GSI4_PK" not in rows["s2"]           # deleted → no GSI4
        assert rows["s2"]["status"] == "deleted"
        assert rows["s3"]["SK"] == "S#s3"                                           # untouched static

    def test_idempotent_second_run_is_noop(self, table):
        _legacy_active(table, "s1", "2026-01-02T00:00:00Z")
        run(TABLE, REGION, apply=True, sleep=0, limit=None, set_marker=False)
        after_first = _meta_rows(table)

        stats2 = run(TABLE, REGION, apply=True, sleep=0, limit=None, set_marker=False)
        assert stats2["migrated"] == 0 and stats2["ghosts"] == 0
        assert _meta_rows(table) == after_first

    def test_conditional_put_skips_live_migrated_row(self, table):
        # legacy row AND a fresher static row already exist for the same session
        _legacy_active(table, "s1", "2026-01-02T00:00:00Z")
        table.put_item(Item={
            "PK": "USER#u1", "SK": "S#s1", "GSI_PK": "SESSION#s1", "GSI_SK": "META",
            "GSI4_PK": "USER#u1", "GSI4_SK": "2026-09-09T00:00:00Z#s1",
            "sessionId": "s1", "userId": "u1", "title": "FRESH", "status": "active",
            "createdAt": "2026-01-01T00:00:00Z", "lastMessageAt": "2026-09-09T00:00:00Z",
            "messageCount": 9,
        })

        stats = run(TABLE, REGION, apply=True, sleep=0, limit=None, set_marker=False)
        assert stats["skipped_live_migrated"] == 1

        rows = _meta_rows(table)
        assert len(rows) == 1                      # legacy orphan dropped
        assert rows[0]["title"] == "FRESH"         # live row NOT clobbered
        assert rows[0]["messageCount"] == 9

    def test_marker_gated_on_zero_legacy(self, table):
        _legacy_active(table, "s1", "2026-01-02T00:00:00Z")

        # legacy still present → marker withheld
        assert maybe_set_marker(table, apply=True) is False
        assert count_remaining_legacy(table) == 1
        marker = table.get_item(Key={"PK": "MIGRATION#session-sk", "SK": "STATE"}).get("Item")
        assert marker is None

        # migrate, then marker sets
        run(TABLE, REGION, apply=True, sleep=0, limit=None, set_marker=True)
        assert count_remaining_legacy(table) == 0
        marker = table.get_item(Key={"PK": "MIGRATION#session-sk", "SK": "STATE"}).get("Item")
        assert marker is not None and marker["complete"] is True
