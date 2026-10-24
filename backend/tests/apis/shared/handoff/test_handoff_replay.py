"""What ``GET /messages`` replays for handoffs (handoffs spec §4.3, PR-0.3)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from apis.shared.handoff.models import Handoff
from apis.shared.handoff.replay import load_handoff_replay
from apis.shared.handoff.store import HandoffStore

REGION = "us-west-2"
TABLE = "test-sessions-metadata"


@pytest.fixture()
def store(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "HANDOFFS_ENABLED": "true",
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
        yield HandoffStore(table=boto3.resource("dynamodb", region_name=REGION).Table(TABLE))


def add(store: HandoffStore, handoff_id: str, *, user_id: str = "alice", session_id: str = "src", kind: str = "mention") -> None:
    now = datetime.now(timezone.utc).isoformat()
    store.create(
        Handoff(
            handoff_id=handoff_id,
            user_id=user_id,
            session_id=session_id,
            kind=kind,
            agent_id="rubric",
            agent_name="Rubric Builder",
            request="draft a rubric for this",
            source_title="Course planning",
            source_message_index=3,
            excerpt_hash="h",
            created_at=now,
            updated_at=now,
        )
    )


def send(store: HandoffStore, handoff_id: str, target: str = "tgt") -> None:
    store.accept("alice", handoff_id, target_session_id=target, context_edited=False, context_removed=False)


def replay(store, session_id: str = "src", row=None, user_id: str = "alice"):
    return asyncio.run(load_handoff_replay(session_id, user_id, row, store=store))


def test_a_source_replays_only_the_handoffs_it_sent(store):
    add(store, "sent")
    add(store, "still-open")
    add(store, "cancelled")
    add(store, "back", kind="send_back")
    send(store, "sent")
    send(store, "back", target="src-other")
    store.transition("alice", "cancelled", "dismissed")

    handoffs, spawned = replay(store)

    assert [h["handoffId"] for h in handoffs] == ["sent"]
    view = handoffs[0]
    assert (view["status"], view["targetSessionId"], view["sourceMessageIndex"], view["agentName"]) == (
        "running", "tgt", 3, "Rubric Builder",
    )
    for hidden in ("userId", "excerptHash", "sourceTitle", "PK"):
        assert hidden not in view
    assert spawned is None


def test_another_users_handoffs_on_the_same_session_id_are_not_replayed(store):
    add(store, "theirs", user_id="bob")
    store.accept("bob", "theirs", target_session_id="t", context_edited=False, context_removed=False)

    assert replay(store) == ([], None)


def test_a_target_replays_where_it_came_from(store):
    add(store, "h1")
    send(store, "h1")

    _, spawned = replay(store, session_id="tgt", row={"spawnedFrom": {"sessionId": "src", "handoffId": "h1", "settled": True}})

    assert spawned == {"sessionId": "src", "handoffId": "h1", "sourceTitle": "Course planning", "agentName": "Rubric Builder"}


def test_a_link_whose_handoff_row_is_gone_still_replays_without_a_title(store):
    _, spawned = replay(store, session_id="tgt", row={"spawnedFrom": {"sessionId": "src", "handoffId": "gone"}})

    assert spawned == {"sessionId": "src", "handoffId": "gone", "sourceTitle": "", "agentName": ""}


def test_nothing_while_the_flag_is_off(store, monkeypatch):
    add(store, "h1")
    send(store, "h1")
    monkeypatch.setenv("HANDOFFS_ENABLED", "false")

    assert replay(store) == ([], None)


def test_a_failing_store_replays_nothing_rather_than_failing_the_thread(monkeypatch):
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")

    class _Broken:
        def list_for_session(self, *a):
            raise RuntimeError("throttled")

        def get(self, *a):
            raise RuntimeError("throttled")

    assert replay(_Broken()) == ([], None)
