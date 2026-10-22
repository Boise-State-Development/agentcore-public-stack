"""Tests for the false interrupted-turn marker backfill.

The risk this guards is asymmetric. Leaving a stale marker behind costs a
spurious "Response interrupted" chip and one false `<interruption_note>` on
that session's next prompt; clearing a REAL one destroys the only record that
a turn was cut off, and with it the reload's offer to continue. So the
assertions that matter most are about what the script refuses to touch.

The 900s threshold is the whole safety argument: a genuine interruption does
not always bump `lastMessageAt` (the "marker only, no synthetic write" branch
of `_persist_interruption`), so a modest gap is ambiguous — but no turn is
still running 15 minutes after its last message, because the stream times out
at 600s.
"""

from __future__ import annotations

import importlib.util
import pathlib

import boto3
import pytest
from moto import mock_aws

REGION = "us-east-1"
TABLE = "test-sessions-metadata"

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "backfill_false_interrupted_markers.py"
)
_spec = importlib.util.spec_from_file_location("backfill_interrupt_markers", _SCRIPT)
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


def marked_row(
    session_id: str,
    *,
    reason: str = "navigated_away",
    last_message_at: str = "2026-09-01T12:00:00+00:00",
    marked_at: str = "2026-09-01T13:00:00+00:00",
) -> dict:
    return {
        "PK": "USER#u1",
        "SK": f"S#{session_id}",
        "GSI_PK": f"SESSION#{session_id}",
        "lastTurnInterrupted": True,
        "lastTurnInterruptReason": reason,
        "lastTurnInterruptedAt": marked_at,
        "lastMessageAt": last_message_at,
        "title": "keep me",
    }


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selects_navigated_away_marked_long_after_the_last_message():
    rows = [marked_row("s1")]  # +1h
    selected, _ = backfill_mod._select(rows, backfill_mod.DEFAULT_MIN_GAP_SECONDS)
    assert [r["GSI_PK"] for r in selected] == ["SESSION#s1"]


@pytest.mark.parametrize("reason", ["user_stopped", "connection_lost", "unknown"])
def test_never_selects_a_reason_this_bug_cannot_produce(reason: str):
    """Only the client's `navigated_away` path had the stale-controller bug.

    `user_stopped` is the user's own attested intent and `connection_lost`
    is the server's own backstop — clearing either would erase a real record.
    """
    rows = [marked_row("s1", reason=reason)]
    selected, skipped = backfill_mod._select(rows, backfill_mod.DEFAULT_MIN_GAP_SECONDS)
    assert selected == []
    assert skipped["other_reason"] == 1


def test_leaves_an_ambiguous_gap_alone():
    """A 10-minute gap is inside the 600s stream timeout.

    An interrupted continuation persists no assistant message, so
    `lastMessageAt` stays at the previous turn and a real interruption can
    show a positive gap. Below the threshold we cannot tell, so we don't act.
    """
    rows = [marked_row("s1", marked_at="2026-09-01T12:10:00+00:00")]
    selected, skipped = backfill_mod._select(rows, backfill_mod.DEFAULT_MIN_GAP_SECONDS)
    assert selected == []
    assert skipped["gap_too_small"] == 1


def test_leaves_a_marker_that_predates_the_last_message_alone():
    """The shape of a genuine mid-turn departure: the signal lands first, the
    turn's partial is persisted after it."""
    rows = [
        marked_row(
            "s1",
            last_message_at="2026-09-01T12:00:30+00:00",
            marked_at="2026-09-01T12:00:00+00:00",
        )
    ]
    selected, _ = backfill_mod._select(rows, backfill_mod.DEFAULT_MIN_GAP_SECONDS)
    assert selected == []


def test_leaves_unparseable_timestamps_alone():
    rows = [marked_row("s1", marked_at="not-a-date")]
    selected, skipped = backfill_mod._select(rows, backfill_mod.DEFAULT_MIN_GAP_SECONDS)
    assert selected == []
    assert skipped["unparseable"] == 1


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_clear_removes_only_the_marker_attributes(table):
    row = marked_row("s1")
    table.put_item(Item=row)

    assert backfill_mod._clear(table, row) == "cleared"

    stored = table.get_item(Key={"PK": row["PK"], "SK": row["SK"]})["Item"]
    for attr in backfill_mod.MARKER_ATTRS:
        assert attr not in stored
    # Everything else on the row survives — this is a targeted REMOVE, not a
    # rewrite (a full put_item would drop attributes the script never read).
    assert stored["title"] == "keep me"
    assert stored["lastMessageAt"] == row["lastMessageAt"]


def test_clear_skips_a_row_re_marked_since_the_scan(table):
    """A session that ran a new turn between scan and write may have been
    legitimately re-marked. The write is conditional on the exact timestamp
    the scan read, so it declines rather than clobbering."""
    row = marked_row("s1")
    table.put_item(Item=row)
    table.update_item(
        Key={"PK": row["PK"], "SK": row["SK"]},
        UpdateExpression="SET lastTurnInterruptedAt = :ts",
        ExpressionAttributeValues={":ts": "2026-09-02T09:00:00+00:00"},
    )

    assert backfill_mod._clear(table, row) == "raced"

    stored = table.get_item(Key={"PK": row["PK"], "SK": row["SK"]})["Item"]
    assert stored["lastTurnInterrupted"] is True
    assert stored["lastTurnInterruptedAt"] == "2026-09-02T09:00:00+00:00"


def test_clear_skips_a_row_whose_reason_was_upgraded(table):
    """`set_interrupted_turn` lets a stronger reason overwrite a weaker one.
    If `user_stopped` landed after our scan, this is no longer our row."""
    row = marked_row("s1")
    table.put_item(Item={**row, "lastTurnInterruptReason": "user_stopped"})

    assert backfill_mod._clear(table, row) == "raced"
    stored = table.get_item(Key={"PK": row["PK"], "SK": row["SK"]})["Item"]
    assert stored["lastTurnInterruptReason"] == "user_stopped"


def test_clear_is_idempotent(table):
    row = marked_row("s1")
    table.put_item(Item=row)

    assert backfill_mod._clear(table, row) == "cleared"
    # A second pass finds nothing to do rather than erroring.
    assert backfill_mod._clear(table, row) == "raced"
