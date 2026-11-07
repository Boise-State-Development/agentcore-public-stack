"""scripts/release_ledger_backfill.py (docs/specs/release-ledger.md §7)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "release_ledger_backfill.py"
_spec = importlib.util.spec_from_file_location("release_ledger_backfill", SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = backfill  # dataclasses resolve their module by name
_spec.loader.exec_module(backfill)

PREFIX = "test-ledger"
REGION = "us-west-2"


def _cost_row(ts: str, cost: str, **extra):
    return {
        "PK": "USER#u-1",
        "SK": f"C#{ts}#x{ts[-8:]}",
        "userId": "u-1",
        "sessionId": "s-1",
        "messageId": 3,
        "timestamp": ts,
        "modelInfo": {"modelId": "m-1", "modelName": "M", "pricingSnapshot": {"inputPricePerMtok": Decimal("1")}},
        "tokenUsage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        "cost": {"total": Decimal(cost)},
        "turnDurationMs": 1000,
        "contextBreakdown": {"items": [{"label": "secret skill"}]},
        **extra,
    }


DEPLOYS = [
    {"at": "2026-10-01T12:00:00Z", "appVersion": "1.31.0", "build": "git-aaaaaaaaaaaa"},
    {"at": "2026-10-02T12:00:00Z", "appVersion": "1.32.0", "build": "git-bbbbbbbbbbbb"},
]


def test_unstamped_rows_take_the_deploy_live_at_their_timestamp():
    deploys = [backfill.Deploy(backfill.parse_instant(d["at"]), d["appVersion"], d["build"]) for d in DEPLOYS]
    keys = [d.at for d in deploys]

    early = {"timestamp": "2026-09-30T00:00:00+00:00"}
    assert backfill.attribute(early, deploys, keys) == "unknown"
    assert (early["appVersion"], early["build"]) == ("unknown", "unknown")

    first = {"timestamp": "2026-10-02T11:59:59+00:00"}
    assert backfill.attribute(first, deploys, keys) == "mapped"
    assert first["appVersion"] == "1.31.0"

    exact = {"timestamp": "2026-10-02T12:00:00+00:00"}
    backfill.attribute(exact, deploys, keys)
    assert exact["build"] == "git-bbbbbbbbbbbb"

    stamped = {"timestamp": "2026-10-01T13:00:00+00:00", "appVersion": "1.33.0", "build": "abc"}
    assert backfill.attribute(stamped, deploys, keys) == "stamped"
    assert stamped["appVersion"] == "1.33.0"


def test_plan_keeps_the_range_half_open_and_reconciles_full_days_only():
    deploys = [backfill.Deploy(backfill.parse_instant(d["at"]), d["appVersion"], d["build"]) for d in DEPLOYS]
    rows = [
        _cost_row("2026-10-01T13:00:00+00:00", "0.10"),
        _cost_row("2026-10-01T23:00:00+00:00", "0.20"),
        _cost_row("2026-10-02T01:00:00+00:00", "0.40"),  # outside [start, end)
    ]
    start = backfill.parse_instant("2026-10-01")
    end = backfill.parse_instant("2026-10-02")
    updates, summary = backfill.plan(rows, deploys, start, end)
    assert summary["rows"] == 2
    assert summary["byDay"] == {"2026-10-01": {"cost": Decimal("0.30"), "calls": Decimal(2)}}
    version = [u for u in updates if u.pk == "LEDGER#VERSION"]
    assert len(version) == 1 and version[0].add["calls"] == 2 and version[0].add["turns"] == 2
    assert "secret skill" not in json.dumps(summary, default=str)

    rec = backfill.reconcile(
        summary["byDay"],
        {"2026-10-01": {"cost": Decimal("0.30"), "calls": Decimal(2)},
         "2026-10-02": {"cost": Decimal("9"), "calls": Decimal(9)}},
        start, end,
    )
    assert rec["fullDays"] == 1
    assert rec["costDiff"] == 0
    assert rec["daysWithCallDiff"] == {}


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        db = boto3.resource("dynamodb", region_name=REGION)
        tables = {}
        for suffix in ("sessions-metadata", "system-cost-rollup"):
            tables[suffix] = db.create_table(
                TableName=f"{PREFIX}-{suffix}",
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                      {"AttributeName": "SK", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        yield tables


def _args(tmp_path, **overrides):
    version_map = tmp_path / "versions.json"
    version_map.write_text(json.dumps(DEPLOYS))
    values = dict(profile=None, region=REGION, prefix=PREFIX, version_map=str(version_map),
                  from_="2026-10-01", to="2026-10-03", segments=2, report=None, yes=False)
    values.update(overrides)
    return argparse.Namespace(**values)


def test_a_dry_run_writes_nothing_and_a_write_adds_up_and_blocks_a_rerun(aws, tmp_path, capsys):
    meta, rollup = aws["sessions-metadata"], aws["system-cost-rollup"]
    meta.put_item(Item=_cost_row("2026-10-01T13:00:00+00:00", "0.10"))
    meta.put_item(Item=_cost_row("2026-10-02T13:00:00+00:00", "0.20", appVersion="1.33.0", build="cafe"))
    meta.put_item(Item={"PK": "USER#u-1", "SK": "S#ACTIVE#x", "title": "not a cost row"})
    rollup.put_item(Item={"PK": "ROLLUP#DAILY", "SK": "2026-10-01", "totalCost": Decimal("0.10"), "totalRequests": 1})
    rollup.put_item(Item={"PK": "ROLLUP#DAILY", "SK": "2026-10-02", "totalCost": Decimal("0.20"), "totalRequests": 1})

    assert backfill.run_backfill(_args(tmp_path)) == 0
    assert "dry run" in capsys.readouterr().out
    assert rollup.scan()["Count"] == 2

    assert backfill.run_backfill(_args(tmp_path, yes=True)) == 0
    items = rollup.scan()["Items"]
    versions = sorted(i["SK"] for i in items if i["PK"] == "LEDGER#VERSION")
    assert versions == ["2026-10-01#1.31.0#git-aaaaaaaaaaaa", "2026-10-02#1.33.0#cafe"]
    assert all("backfilledAt" in i for i in items if i["PK"].startswith("LEDGER#") and i["PK"] != "LEDGER#BACKFILL")
    (marker,) = [i for i in items if i["PK"] == "LEDGER#BACKFILL"]
    assert marker["status"] == "complete" and marker["rows"] == 2

    assert backfill.run_backfill(_args(tmp_path, yes=True)) == 2
    assert "overlaps an earlier backfill" in capsys.readouterr().err


def test_a_range_past_the_first_live_write_is_refused(aws, tmp_path, capsys):
    aws["system-cost-rollup"].put_item(Item={
        "PK": "LEDGER#VERSION", "SK": "2026-10-02#1.33.0#cafe", "calls": 1,
        "firstLiveAt": "2026-10-02T09:00:00+00:00",
    })
    assert backfill.run_backfill(_args(tmp_path)) == 2
    assert "first live ledger write" in capsys.readouterr().err
    # With no --to it stops exactly at the first live write.
    assert backfill.run_backfill(_args(tmp_path, to=None)) == 0
    assert "→ 2026-10-02T09:00:00+00:00" in capsys.readouterr().out
