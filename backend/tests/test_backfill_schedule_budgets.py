"""Tests for the one-time schedule budget backfill (docs/specs/proactive-trust.md §4.4, PR 4.1).

What matters: every personal schedule under 60 minutes without a budget gets the default,
nothing else is touched, an existing budget is never overwritten, and a dry run writes nothing.
"""

import os
import sys
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backfill_schedule_budgets import needs_budget, resolve_budget, run  # noqa: E402

REGION = "us-east-1"
TABLE = "test-sessions-metadata"


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _put(table, schedule_id, **fields):
    item = {
        "PK": "USER#u1",
        "SK": f"SCHEDPROMPT#{schedule_id}",
        "scheduleId": schedule_id,
        "cadence": "interval",
        "intervalValue": 15,
        "intervalUnit": "minutes",
        "state": "active",
        "updatedAt": "2026-11-01T00:00:00Z",
        "nextRunAt": "2026-12-02T19:15:00Z",
    }
    item.update(fields)
    table.put_item(Item=item)


def _get(table, schedule_id):
    return table.get_item(Key={"PK": "USER#u1", "SK": f"SCHEDPROMPT#{schedule_id}"})["Item"]


def _seed(table):
    _put(table, "sched-watch15", mode="watch")
    _put(table, "sched-paused30", intervalValue=30, state="paused")
    _put(table, "sched-hourly", intervalValue=1, intervalUnit="hours")
    _put(table, "sched-daily", cadence="daily", intervalValue=None, intervalUnit=None)
    _put(table, "sched-project", projectId="prj_1")
    _put(table, "sched-has", maxCostPerDay=Decimal("2"))
    table.put_item(Item={"PK": "USER#u1", "SK": "SCHEDRUN#sched-watch15#x#run-1", "cadence": "interval"})


def test_sets_the_default_on_short_personal_schedules_only(table):
    _seed(table)
    counts = run(table, Decimal("0.50"), apply=True)
    assert counts["needs_budget"] == 2 and counts["written"] == 2
    assert _get(table, "sched-watch15")["maxCostPerDay"] == Decimal("0.50")
    assert _get(table, "sched-paused30")["maxCostPerDay"] == Decimal("0.50")
    for untouched in ("sched-hourly", "sched-daily", "sched-project"):
        assert "maxCostPerDay" not in _get(table, untouched)
    assert _get(table, "sched-has")["maxCostPerDay"] == Decimal("2")
    # Nothing else moves.
    watch = _get(table, "sched-watch15")
    assert watch["updatedAt"] == "2026-11-01T00:00:00Z" and watch["nextRunAt"] == "2026-12-02T19:15:00Z"


def test_a_dry_run_writes_nothing(table):
    _seed(table)
    counts = run(table, Decimal("0.50"), apply=False)
    assert counts["needs_budget"] == 2 and counts["written"] == 0
    assert "maxCostPerDay" not in _get(table, "sched-watch15")


def test_a_second_run_finds_nothing(table):
    _seed(table)
    run(table, Decimal("0.50"), apply=True)
    assert run(table, Decimal("0.50"), apply=True)["needs_budget"] == 0


def test_needs_budget_reads_only_schedule_rows():
    assert not needs_budget({"SK": "SCHEDRUN#x", "cadence": "interval", "intervalValue": 15, "intervalUnit": "minutes"})


def test_resolve_budget(monkeypatch):
    monkeypatch.delenv("SCHEDULE_DEFAULT_MAX_COST_PER_DAY", raising=False)
    assert resolve_budget(None) == Decimal("0.50")
    monkeypatch.setenv("SCHEDULE_DEFAULT_MAX_COST_PER_DAY", "0.75")
    assert resolve_budget(None) == Decimal("0.75")
    assert resolve_budget("1.5") == Decimal("1.5")
    with pytest.raises(SystemExit):
        resolve_budget("0")
