"""``WFCHK#`` rows: a checklist step's display rows (workflow-designer §22.3, PR-D.7a).

What these pin: the key shape the Runtime writes and the run read finds, the step's authored order
on read, the owner check, the clamps, quotes dropped rather than a failed write past the item
limit, a refused status, and the delete cascade.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from apis.shared.handoff import checklist_rows
from apis.shared.handoff.checklist_rows import (
    ChecklistField,
    ChecklistRowStore,
    put_checklist_set,
    read_checklist_sets,
    rows_by_step,
)

REGION = "us-west-2"
TABLE = "sessions-metadata"


@pytest.fixture()
def store(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI_PK", "GSI_SK")],
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
        table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
        yield ChecklistRowStore(table=table)


def _put(store, **kw):
    args = dict(
        user_id="alice",
        session_id="src",
        run_id="run-1",
        step_id="step-a",
        field_set="budget-caps",
        set_index=0,
        field_set_version="2",
        status="done",
        fields=[{"name": "cap", "title": "Cap", "type": "currency", "value": "$5", "state": "supported", "method": "exact"}],
        model_id="us.haiku",
        store=store,
    )
    args.update(kw)
    return put_checklist_set(**args)


def test_the_key_is_the_run_the_step_and_the_set(store):
    assert _put(store)
    item = store._table.get_item(Key={"PK": "USER#alice", "SK": "WFCHK#run-1#step-a#budget-caps"})["Item"]

    assert item["GSI_PK"] == "SESSION#src"
    assert item["GSI_SK"] == "WFCHK#run-1#step-a#budget-caps"
    assert (item["runId"], item["stepId"], item["fieldSet"], item["status"]) == ("run-1", "step-a", "budget-caps", "done")
    assert item["ttl"] > 0


def test_a_rerun_of_one_set_overwrites_its_row(store):
    _put(store)
    _put(store, status="error", error="no_tool_call", fields=[])

    rows = read_checklist_sets(user_id="alice", session_id="src", store=store)
    assert [(r.field_set, r.status, r.error) for r in rows] == [("budget-caps", "error", "no_tool_call")]


def test_reads_come_back_in_each_steps_authored_order_and_only_the_owners(store):
    _put(store, field_set="zeta", set_index=0)
    _put(store, field_set="alpha", set_index=1)
    _put(store, step_id="step-b", field_set="mid", set_index=0)
    _put(store, run_id="run-2", field_set="other", set_index=0)
    _put(store, user_id="mallory", field_set="theirs", set_index=0)

    rows = read_checklist_sets(user_id="alice", session_id="src", run_id="run-1", store=store)

    assert [(r.step_id, r.field_set) for r in rows] == [("step-a", "zeta"), ("step-a", "alpha"), ("step-b", "mid")]
    assert {k: [r.field_set for r in v] for k, v in rows_by_step(rows).items()} == {
        "step-a": ["zeta", "alpha"],
        "step-b": ["mid"],
    }
    assert len(read_checklist_sets(user_id="alice", session_id="src", store=store)) == 4


def test_a_field_round_trips_with_its_quote_locator_and_a_null_value(store):
    _put(
        store,
        fields=[
            ChecklistField(
                name="cap", title="Cap", type="currency", value="$5", state="supported", method="exact",
                quote="not to exceed $5", locator={"kind": "page", "value": 3, "precision": "estimated"},
            ),
            {"name": "pi", "title": "PI", "type": "string", "value": None, "state": "unverified", "method": ""},
        ],
    )

    row = read_checklist_sets(user_id="alice", session_id="src", store=store)[0]

    assert row.fields[0].quote == "not to exceed $5"
    assert row.fields[0].locator.model_dump() == {"kind": "page", "value": 3, "precision": "estimated"}
    assert row.fields[1].value is None and row.fields[1].quote is None
    wire = row.model_dump(by_alias=True, exclude_none=True)
    assert set(wire) >= {"runId", "stepId", "fieldSet", "setIndex", "fieldSetVersion", "modelId", "status", "fields"}


def test_values_and_quotes_are_clamped(store):
    _put(store, fields=[{"name": "x", "value": "v" * 5000, "quote": "q" * 5000, "state": "supported"}])
    field = read_checklist_sets(user_id="alice", session_id="src", store=store)[0].fields[0]

    assert len(field.value) == checklist_rows.MAX_VALUE_CHARS
    assert len(field.quote) == checklist_rows.MAX_QUOTE_CHARS


def test_quotes_are_dropped_rather_than_fail_the_write_past_the_item_limit(store, monkeypatch):
    monkeypatch.setattr(checklist_rows, "MAX_ITEM_CHARS", 500)
    _put(store, fields=[{"name": f"f{i}", "value": "v", "quote": "q" * 200, "state": "supported"} for i in range(5)])

    row = read_checklist_sets(user_id="alice", session_id="src", store=store)[0]
    assert row.quotes_omitted is True
    assert all(f.quote is None for f in row.fields)
    assert [f.state for f in row.fields] == ["supported"] * 5


def test_an_unknown_status_is_refused_and_writes_nothing(store):
    assert _put(store, status="running") is False
    assert read_checklist_sets(user_id="alice", session_id="src", store=store) == []


def test_without_a_table_nothing_is_written_or_read():
    empty = ChecklistRowStore(table=None)
    empty._table = None
    assert _put(empty) is False
    assert read_checklist_sets(user_id="alice", session_id="src", store=empty) == []


def test_the_delete_cascade_removes_the_owners_rows_only(store):
    _put(store, field_set="a")
    _put(store, field_set="b", set_index=1)
    _put(store, user_id="mallory", field_set="theirs")

    assert store.delete_for_session("alice", "src") == 2
    assert read_checklist_sets(user_id="alice", session_id="src", store=store) == []
    assert len(read_checklist_sets(user_id="mallory", session_id="src", store=store)) == 1
