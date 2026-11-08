"""Tests for the curated ``max_tokens`` default backfill.

What matters most is which rows are left alone: a row an admin edited must
keep the admin's value, whatever it is. A backfill that "fixed" those would
silently undo a deliberate governance decision on every model it touched.
"""

import os
import re
import sys
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backfill_model_output_caps import CHANGES, backfill, plan_row  # noqa: E402

REGION = "us-east-1"
TABLE = "test-managed-models"
CURATED = Path(__file__).resolve().parents[2] / "frontend/ai.client/src/app/admin/manage-models/models/curated-models.ts"


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


def _row(uid, model_id, default=32_000, spec_max=128_000, max_output=128_000, **extra):
    params = {"effort": {"supported": True, "allowed": ["low", "medium"], "default": "medium"}}
    if default is not ...:
        params["max_tokens"] = {"supported": True, "min": 1, "max": spec_max, "default": default}
    item = {
        "PK": f"MODEL#{uid}",
        "SK": f"MODEL#{uid}",
        "id": uid,
        "modelId": model_id,
        "modelName": f"model {uid}",
        "maxOutputTokens": max_output,
        "inputPricePerMillionTokens": Decimal("4.4"),
        "supportedParams": {"params": params},
        "updatedAt": "2026-10-01T00:00:00+00:00",
    }
    item.update(extra)
    return item


def _default(table, uid):
    item = table.get_item(Key={"PK": f"MODEL#{uid}", "SK": f"MODEL#{uid}"})["Item"]
    return item["supportedParams"]["params"].get("max_tokens", {}).get("default"), item


def test_curated_new_defaults_match_the_catalog():
    """The script's literals are the curated rows' current defaults."""
    source = CURATED.read_text()
    for change in CHANGES:
        start = source.index(f"key: '{change.curated_key}'")
        match = re.search(r"max_tokens:\s*\{[^}]*default:\s*([\d_]+)", source[start:])
        assert match, change.curated_key
        assert int(match.group(1).replace("_", "")) == change.new_default


def test_raises_rows_still_on_the_old_default_across_profiles(table):
    table.put_item(Item=_row("a", "us.anthropic.claude-opus-5-5"))
    table.put_item(Item=_row("b", "global.anthropic.claude-opus-5-5"))
    table.put_item(Item=_row("c", "anthropic.claude-opus-5-5"))

    stats = backfill(table, apply=True, now="2026-11-07T00:00:00+00:00")

    assert stats["updated"] == 3 and stats["failed"] == 0
    for uid in "abc":
        value, item = _default(table, uid)
        assert value == 64_000
        assert item["updatedAt"] == "2026-11-07T00:00:00+00:00"
        # Nothing else on the row moved.
        assert item["supportedParams"]["params"]["max_tokens"]["max"] == 128_000
        assert item["supportedParams"]["params"]["effort"]["default"] == "medium"
        assert item["inputPricePerMillionTokens"] == Decimal("4.4")


@pytest.mark.parametrize(
    ("row", "expected_default"),
    [
        (_row("lower", "us.anthropic.claude-opus-5-5", default=16_000), 16_000),
        (_row("higher", "us.anthropic.claude-opus-5-5", default=100_000), 100_000),
        (_row("unset", "us.anthropic.claude-opus-5-5", default=None), None),
        (_row("narrow-max", "us.anthropic.claude-opus-5-5", spec_max=48_000), 32_000),
        (_row("narrow-ceiling", "us.anthropic.claude-opus-5-5", max_output=40_000), 32_000),
        (_row("haiku", "us.anthropic.claude-haiku-5-5"), 32_000),
        (_row("opus-4-7", "us.anthropic.claude-opus-4-7"), 32_000),
        (_row("variant", "us.anthropic.claude-opus-5-5-v2"), 32_000),
    ],
    ids=lambda v: v["id"] if isinstance(v, dict) else None,
)
def test_leaves_admin_edited_and_other_rows_alone(table, row, expected_default):
    table.put_item(Item=row)

    stats = backfill(table, apply=True)

    assert stats["updated"] == 0 and stats["failed"] == 0
    value, _ = _default(table, row["id"])
    assert value == expected_default


def test_row_without_a_max_tokens_spec_is_left_alone(table):
    table.put_item(Item=_row("nospec", "us.anthropic.claude-opus-5-5", default=...))
    assert plan_row(_row("nospec", "us.anthropic.claude-opus-5-5", default=...))["action"] == "skip"
    assert backfill(table, apply=True)["updated"] == 0


def test_dry_run_writes_nothing(table):
    table.put_item(Item=_row("a", "us.anthropic.claude-opus-5-5"))

    stats = backfill(table, apply=False)

    assert stats["updated"] == 1  # reported as "would update"
    value, item = _default(table, "a")
    assert value == 32_000
    assert item["updatedAt"] == "2026-10-01T00:00:00+00:00"


def test_second_run_is_a_no_op(table):
    table.put_item(Item=_row("a", "us.anthropic.claude-opus-5-5"))
    backfill(table, apply=True)

    stats = backfill(table, apply=True)

    assert stats["updated"] == 0 and stats["skipped"] == 1


def test_an_admin_edit_between_scan_and_write_wins(table, monkeypatch):
    import backfill_model_output_caps as script

    table.put_item(Item=_row("a", "us.anthropic.claude-opus-5-5"))
    real_iter = script.iter_model_rows

    def scan_then_admin_edits(t):
        for item in list(real_iter(t)):
            t.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET supportedParams.params.max_tokens.#d = :v",
                ExpressionAttributeNames={"#d": "default"},
                ExpressionAttributeValues={":v": 20_000},
            )
            yield item

    monkeypatch.setattr(script, "iter_model_rows", scan_then_admin_edits)

    stats = backfill(table, apply=True)

    assert stats["raced"] == 1 and stats["updated"] == 0
    value, _ = _default(table, "a")
    assert value == 20_000


def test_non_model_rows_are_ignored(table):
    table.put_item(Item={"PK": "MODEL#x", "SK": "SOMETHING#else", "modelId": "us.anthropic.claude-opus-5-5"})
    table.put_item(Item={"PK": "PROVIDER#y", "SK": "PROVIDER#y"})

    stats = backfill(table, apply=True)

    assert stats["model_rows"] == 0
