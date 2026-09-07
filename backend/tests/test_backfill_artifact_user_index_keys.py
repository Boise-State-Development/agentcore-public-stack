"""Tests for the UserArtifactsIndex key backfill.

The failure this guards against is silent: `UserArtifactsIndex` is
sparse, so a HEAD row left without `GSI2PK`/`GSI2SK` is not stale in the
index — it is absent from it forever, and the library page simply stops
listing that artifact with no error anywhere.

So the assertions that matter are about what the script *refuses* to do
(fabricate a sort key, touch `updated_at`, resurrect a deleted row) as
much as what it writes.
"""

from __future__ import annotations

import importlib.util
import pathlib

import boto3
import pytest
from moto import mock_aws

REGION = "us-east-1"
TABLE = "test-user-artifacts"

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "backfill_artifact_user_index_keys.py"
)
_spec = importlib.util.spec_from_file_location("backfill_user_index", _SCRIPT)
backfill_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(backfill_mod)


@pytest.fixture()
def table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table(TABLE)


def put_head(
    table,
    *,
    user: str = "u1",
    artifact: str = "a1",
    updated_at: str | None = "2026-08-20T19:41:29.618536+00:00",
    stamped: bool = False,
    **extra,
) -> None:
    item = {
        "PK": f"USER#{user}",
        "SK": f"ARTIFACT#{artifact}#HEAD",
        "artifact_id": artifact,
        "user_id": user,
        "version": 1,
        "title": "Deck",
        "content_type": "text/html; charset=utf-8",
        "session_id": "s1",
    }
    if updated_at is not None:
        item["updated_at"] = updated_at
    if stamped:
        item["GSI2PK"] = f"USER#{user}"
        item["GSI2SK"] = f"ARTIFACT#{updated_at}#{artifact}"
    item.update(extra)
    table.put_item(Item=item)


def put_version(table, *, user: str = "u1", artifact: str = "a1", version: int = 1):
    table.put_item(
        Item={
            "PK": f"USER#{user}",
            "SK": f"ARTIFACT#{artifact}#V#{version:05d}",
            "artifact_id": artifact,
            "user_id": user,
            "version": version,
            "updated_at": "2026-08-20T19:41:29.618536+00:00",
        }
    )


def row(table, user="u1", artifact="a1") -> dict:
    return table.get_item(
        Key={"PK": f"USER#{user}", "SK": f"ARTIFACT#{artifact}#HEAD"}
    )["Item"]


# ------------------------------------------------------------------
# What it writes
# ------------------------------------------------------------------


def test_stamps_keys_matching_what_the_writer_would_have_written(table):
    put_head(table, updated_at="2026-08-20T19:41:29.618536+00:00")

    stats = backfill_mod.backfill(table, apply=True)

    item = row(table)
    assert item["GSI2PK"] == "USER#u1"
    # Byte-for-byte the writer's format: ARTIFACT#{updated_at}#{aid}.
    # A different shape here would order the index inconsistently with
    # every row the writer stamps from now on.
    assert item["GSI2SK"] == "ARTIFACT#2026-08-20T19:41:29.618536+00:00#a1"
    assert stats["stamped"] == 1


def test_never_touches_updated_at(table):
    # updated_at is embedded in both GSI sort keys and is writer-owned.
    # Bumping it here would reorder the library and desync GSI1SK.
    put_head(table, updated_at="2026-08-20T19:41:29.618536+00:00")

    backfill_mod.backfill(table, apply=True)

    assert row(table)["updated_at"] == "2026-08-20T19:41:29.618536+00:00"


def test_leaves_version_rows_alone(table):
    # The keys belong on HEAD only, so the index holds one row per
    # artifact rather than one per version.
    put_head(table)
    put_version(table, version=1)
    put_version(table, version=2)

    stats = backfill_mod.backfill(table, apply=True)

    assert stats["head_rows"] == 1
    for v in (1, 2):
        item = table.get_item(
            Key={"PK": "USER#u1", "SK": f"ARTIFACT#a1#V#{v:05d}"}
        )["Item"]
        assert "GSI2PK" not in item


def test_spans_every_user(table):
    # Whole-table migration: the base table is partitioned by user, so a
    # Query would only ever see one of them.
    put_head(table, user="u1", artifact="a1")
    put_head(table, user="u2", artifact="a2")

    stats = backfill_mod.backfill(table, apply=True)

    assert stats["stamped"] == 2
    assert row(table, "u2", "a2")["GSI2PK"] == "USER#u2"


# ------------------------------------------------------------------
# What it refuses to do
# ------------------------------------------------------------------


def test_encodes_a_missing_timestamp_instead_of_inventing_one(table):
    """A HEAD row with no `updated_at` is still indexed, with an EMPTY
    timestamp segment.

    Leaving it unstamped would drop the artifact out of a sparse index —
    and out of its owner's library — silently. An empty segment sorts
    below every real timestamp, so descending it reads last, exactly
    where the previous in-memory sort put undated rows."""
    put_head(table, updated_at=None)

    stats = backfill_mod.backfill(table, apply=True)

    assert stats["stamped"] == 1
    assert stats["skipped"] == 0
    item = row(table)
    assert item["GSI2SK"] == "ARTIFACT##a1"
    # "#" is below every digit, so this sorts under any real timestamp.
    assert item["GSI2SK"] < "ARTIFACT#2026-01-01T00:00:00+00:00#a1"


def test_dry_run_writes_nothing(table):
    put_head(table)

    stats = backfill_mod.backfill(table, apply=False)

    assert stats["stamped"] == 1  # counted as *would* stamp
    assert "GSI2PK" not in row(table)


def test_is_idempotent(table):
    put_head(table)

    first = backfill_mod.backfill(table, apply=True)
    second = backfill_mod.backfill(table, apply=True)

    assert first["stamped"] == 1
    assert second["stamped"] == 0
    assert second["already"] == 1


def test_yields_to_a_row_the_writer_already_stamped(table):
    # The writer's value wins; the script must not overwrite it.
    put_head(table, stamped=True)

    stats = backfill_mod.backfill(table, apply=True)

    assert stats["already"] == 1
    assert stats["stamped"] == 0


def test_does_not_resurrect_a_row_deleted_mid_run(table):
    """The scan and the writes are not one transaction, so a row can be
    deleted in between. `attribute_exists(SK)` makes that a no-op instead
    of a resurrection — the same rule the writer's own write-backs use."""
    put_head(table)
    rows = list(backfill_mod.iter_head_rows(table))
    assert len(rows) == 1

    table.delete_item(Key={"PK": "USER#u1", "SK": "ARTIFACT#a1#HEAD"})

    # Replay the write for the row we scanned before the delete.
    plan = backfill_mod.plan_row(rows[0])
    assert plan is not None and "skip" not in plan
    stats = backfill_mod.backfill(table, apply=True)

    assert stats["head_rows"] == 0
    assert (
        table.get_item(
            Key={"PK": "USER#u1", "SK": "ARTIFACT#a1#HEAD"}
        ).get("Item")
        is None
    )


def test_reports_an_unexpected_partition_key(table):
    put_head(table)
    table.put_item(
        Item={
            "PK": "SHARE#abc",
            "SK": "ARTIFACT#weird#HEAD",
            "artifact_id": "weird",
            "updated_at": "2026-08-20T00:00:00+00:00",
        }
    )

    stats = backfill_mod.backfill(table, apply=True)

    assert stats["stamped"] == 1
    assert stats["skipped"] == 1
