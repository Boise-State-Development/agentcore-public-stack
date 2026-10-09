"""The ``titleLower`` backfill: dry-run by default, touches only what it must."""

from __future__ import annotations

import importlib.util
import pathlib

import boto3
import pytest
from moto import mock_aws

REGION = "us-east-1"
TABLE = "test-sessions-metadata"

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backfill_session_search_attributes.py"
_spec = importlib.util.spec_from_file_location("backfill_session_search_attributes", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(backfill)


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield t


def _put(table, sid, title, **extra):
    item = {"PK": "USER#u1", "SK": f"S#{sid}", "GSI_SK": "META", "sessionId": sid, "title": title, "status": "active", **extra}
    table.put_item(Item=item)


def _seed(table):
    _put(table, "needs", "Window Functions")
    _put(table, "stale", "Renamed", titleLower="old name")
    _put(table, "done", "Fine", titleLower="fine")
    _put(table, "gone", "Deleted One", status="deleted", deleted=True)
    _put(table, "blank", "")
    table.put_item(Item={"PK": "USER#u1", "SK": "C#2026#x", "title": "Cost Row"})


def _lower(table, sid):
    return table.get_item(Key={"PK": "USER#u1", "SK": f"S#{sid}"})["Item"].get("titleLower")


def test_dry_run_writes_nothing(table):
    _seed(table)
    stats = backfill.run(TABLE, REGION, apply=False, sleep=0, limit=None)
    assert stats["updated"] == 2
    assert _lower(table, "needs") is None and _lower(table, "stale") == "old name"


def test_apply_writes_only_missing_or_stale_rows(table):
    _seed(table)
    stats = backfill.run(TABLE, REGION, apply=True, sleep=0, limit=None)
    assert stats == {"updated": 2, "unchanged": 3, "changed_since_scan": 0}
    assert _lower(table, "needs") == "window functions"
    assert _lower(table, "stale") == "renamed"
    assert _lower(table, "gone") is None
    assert "titleLower" not in table.get_item(Key={"PK": "USER#u1", "SK": "C#2026#x"})["Item"]
    # Re-runnable: nothing left to do.
    assert backfill.run(TABLE, REGION, apply=True, sleep=0, limit=None)["updated"] == 0


def test_a_rename_after_the_scan_is_not_overwritten(table):
    _put(table, "s1", "Before")
    row = {"PK": "USER#u1", "SK": "S#s1", "title": "Before", "status": "active"}
    table.update_item(Key={"PK": "USER#u1", "SK": "S#s1"}, UpdateExpression="SET title = :t, titleLower = :tl",
                      ExpressionAttributeValues={":t": "After", ":tl": "after"})
    stats = {"updated": 0, "unchanged": 0, "changed_since_scan": 0}
    backfill.process_row(table, row, apply=True, stats=stats)
    assert stats["changed_since_scan"] == 1
    assert _lower(table, "s1") == "after"
