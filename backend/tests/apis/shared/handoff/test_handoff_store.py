"""The ``HANDOFF#`` store and its state machine (handoffs spec §4.1, §10 "Handoff lifecycle")."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from apis.shared.handoff.models import Handoff, TurnsCovered
from apis.shared.handoff.store import HandoffStore, HandoffStoreUnavailable

REGION = "us-west-2"
TABLE = "test-sessions-metadata"


@pytest.fixture()
def table(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
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
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.fixture()
def store(table):
    return HandoffStore(table=table)


def handoff(handoff_id: str = "h1", *, user_id: str = "user-a", session_id: str = "src", created_at: str | None = None):
    now = created_at or datetime.now(timezone.utc).isoformat()
    return Handoff(
        handoff_id=handoff_id,
        user_id=user_id,
        session_id=session_id,
        kind="mention",
        agent_id="agent-1",
        agent_name="Rubric Builder",
        request="draft a rubric for this",
        source_title="Course planning",
        source_message_index=11,
        excerpt_hash="abc",
        excerpt_tokens=812,
        turns_covered=TurnsCovered(first=7, last=12, of=12, omitted=6),
        created_at=now,
        updated_at=now,
    )


def test_round_trips_a_row(store):
    store.create(handoff())

    row = store.get("user-a", "h1")

    assert row == handoff().model_copy(update={"created_at": row.created_at, "updated_at": row.updated_at})
    assert row.turns_covered == TurnsCovered(first=7, last=12, of=12, omitted=6)


def test_another_users_id_reads_as_absent(store):
    store.create(handoff())

    assert store.get("user-b", "h1") is None
    assert store.transition("user-b", "h1", "dismissed") is None
    assert store.get("user-a", "h1").status == "open"


def test_an_id_cannot_be_created_twice(store):
    store.create(handoff())

    with pytest.raises(Exception):
        store.create(handoff())


def test_lists_one_users_handoffs_for_a_source_session_oldest_first(store):
    base = datetime(2026, 10, 22, tzinfo=timezone.utc)
    store.create(handoff("late", created_at=(base + timedelta(minutes=5)).isoformat()))
    store.create(handoff("early", created_at=base.isoformat()))
    store.create(handoff("other-session", session_id="elsewhere"))
    # A second user on the same session id: a session id alone does not name a conversation.
    store.create(handoff("other-user", user_id="user-b"))

    rows = store.list_for_session("user-a", "src")

    assert [r.handoff_id for r in rows] == ["early", "late"]


def accept(store, handoff_id: str = "h1", target: str = "tgt"):
    return store.accept("user-a", handoff_id, target_session_id=target, context_edited=False, context_removed=False)


def test_the_full_happy_path(store):
    store.create(handoff())

    accepted = accept(store)
    assert (accepted.status, accepted.target_session_id, accepted.context_edited) == ("running", "tgt", False)
    assert store.transition("user-a", "h1", "needs_input", waitingOn="oauth:canvas").waiting_on == "oauth:canvas"
    assert store.transition("user-a", "h1", "needs_input", waitingOn="approval:send_email")
    assert store.transition("user-a", "h1", "running")
    done = store.transition("user-a", "h1", "done", statusLine="Finished · Drafted a 4-row rubric")

    assert done.status == "done"
    assert done.target_session_id == "tgt"
    assert done.status_line == "Finished · Drafted a 4-row rubric"


def test_needs_input_can_end_without_an_intermediate_running_write(store):
    store.create(handoff())
    accept(store)
    store.transition("user-a", "h1", "needs_input")

    assert store.transition("user-a", "h1", "done").status == "done"


def test_stopped_follows_the_targets_next_turn(store):
    store.create(handoff())
    accept(store)
    stopped = store.transition("user-a", "h1", "stopped", stoppedReason="connection_lost")
    assert stopped.stopped_reason == "connection_lost"

    assert store.transition("user-a", "h1", "done").status == "done"


def test_done_is_frozen(store):
    store.create(handoff())
    accept(store)
    store.transition("user-a", "h1", "done")

    for status in ("running", "needs_input", "stopped", "done", "dismissed"):
        assert store.transition("user-a", "h1", status) is None
    assert accept(store) is None
    assert store.get("user-a", "h1").status == "done"


def test_an_open_handoff_can_be_dismissed_but_a_sent_one_cannot(store):
    store.create(handoff("a"))
    store.create(handoff("b"))
    accept(store, "b")

    assert store.transition("user-a", "a", "dismissed").status == "dismissed"
    assert store.transition("user-a", "b", "dismissed") is None
    assert accept(store, "a") is None


def test_leaving_open_for_running_only_goes_through_accept(store):
    store.create(handoff())

    assert store.transition("user-a", "h1", "running") is None
    assert store.transition("user-a", "h1", "needs_input") is None
    assert store.transition("user-a", "h1", "done") is None
    assert store.get("user-a", "h1").status == "open"


def test_a_handoff_is_accepted_once(store):
    store.create(handoff())

    assert accept(store, target="tgt-1") is not None
    assert accept(store, target="tgt-2") is None
    assert store.get("user-a", "h1").target_session_id == "tgt-1"


def test_acceptance_is_guarded_by_the_writes_own_condition():
    """Two sends racing are settled by DynamoDB evaluating the condition atomically with the write.

    moto does not serialize writes across threads, so a two-thread race can't be driven
    faithfully here; ``test_a_handoff_is_accepted_once`` covers the refusal. What this pins is that
    the guard lives in the write (one ``update_item`` conditioned on ``open``), never in a read
    before it, which is what makes the real race safe.
    """
    calls = []

    class _Table:
        def update_item(self, **kwargs):
            calls.append(kwargs)
            return {"Attributes": {**handoff().model_dump(by_alias=True, exclude_none=True), "status": "running"}}

        def get_item(self, **kwargs):  # pragma: no cover - a read here would be the bug
            raise AssertionError("acceptance must not read before writing")

    accept(HandoffStore(table=_Table()))

    assert len(calls) == 1
    condition = calls[0]["ConditionExpression"]
    assert condition == "attribute_exists(SK) AND #status IN (:from0)"
    assert calls[0]["ExpressionAttributeValues"][":from0"] == "open"


def test_display_fields_are_clamped(store):
    store.create(handoff())
    accept(store)

    row = store.transition("user-a", "h1", "done", statusLine="x" * 1_000)

    assert len(row.status_line) == 240


def test_rejects_fields_a_transition_may_not_set(store):
    store.create(handoff())

    with pytest.raises(ValueError):
        store.transition("user-a", "h1", "done", request="rewritten")


def test_delete_for_session_removes_only_that_users_rows(store):
    store.create(handoff("a"))
    store.create(handoff("b"))
    store.create(handoff("keep", session_id="elsewhere"))
    store.create(handoff("theirs", user_id="user-b"))

    assert store.delete_for_session("user-a", "src") == 2
    assert store.list_for_session("user-a", "src") == []
    assert store.get("user-a", "keep") is not None
    assert store.get("user-b", "theirs") is not None


def test_an_open_row_older_than_a_day_reads_as_abandoned():
    old = handoff(created_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    fresh = handoff()

    assert old.effective_status() == "abandoned"
    assert fresh.effective_status() == "open"
    assert old.model_copy(update={"status": "running"}).effective_status() == "running"


def test_without_a_table_writes_refuse_and_reads_are_empty(monkeypatch):
    monkeypatch.delenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", raising=False)
    store = HandoffStore()

    assert not store.enabled
    assert store.list_for_session("user-a", "src") == []
    assert store.delete_for_session("user-a", "src") == 0
    with pytest.raises(HandoffStoreUnavailable):
        store.create(handoff())
