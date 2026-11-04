"""Inbox entries for a handoff's state changes (handoffs spec §4.3 "The in-app inbox", §10 "Inbox writes")."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
import pytest
from moto import mock_aws

from apis.shared.handoff import notify as notify_module
from apis.shared.handoff import turn as turn_module
from apis.shared.handoff.context import context_hash
from apis.shared.handoff.models import Handoff
from apis.shared.handoff.store import HandoffStore
from apis.shared.handoff.turn import HandoffContextRequest, HandoffTurn
from apis.shared.notifications import NotificationService

REGION = "us-west-2"
SESSIONS_TABLE = "test-sessions-metadata"
PROJECTS_TABLE = "test-projects"
REQUEST = "draft a rubric for this"
CARD = "<conversation_excerpt>plan BIO 101 week by week</conversation_excerpt>"
EMAIL = "Alice@Example.edu"


def frame(event: str, data: Dict[str, Any] | None = None) -> str:
    return f"event: {event}\ndata: {json.dumps(data or {})}\n\n"


def _key_table(client, name: str, gsi: bool) -> None:
    attrs = ("PK", "SK", "GSI_PK", "GSI_SK") if gsi else ("PK", "SK")
    kwargs: Dict[str, Any] = dict(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in attrs],
        BillingMode="PAY_PER_REQUEST",
    )
    if gsi:
        kwargs["GlobalSecondaryIndexes"] = [
            {
                "IndexName": "SessionLookupIndex",
                "KeySchema": [
                    {"AttributeName": "GSI_PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI_SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ]
    client.create_table(**kwargs)


@pytest.fixture()
def tables(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "HANDOFFS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        _key_table(client, SESSIONS_TABLE, gsi=True)
        _key_table(client, PROJECTS_TABLE, gsi=False)
        resource = boto3.resource("dynamodb", region_name=REGION)
        inbox = NotificationService(table_name=PROJECTS_TABLE)
        monkeypatch.setattr(notify_module, "_service", inbox)
        yield resource.Table(SESSIONS_TABLE), resource.Table(PROJECTS_TABLE), inbox


@pytest.fixture()
def store(tables) -> HandoffStore:
    s = HandoffStore(table=tables[0])
    now = datetime.now(timezone.utc).isoformat()
    s.create(
        Handoff(
            handoff_id="h1",
            user_id="alice",
            session_id="src",
            kind="mention",
            agent_id="rubric",
            agent_name="Rubric Builder",
            request=REQUEST,
            source_title="Course planning",
            source_message_index=1,
            excerpt_hash=context_hash(CARD),
            excerpt_tokens=40,
            created_at=now,
            updated_at=now,
        )
    )
    return s


@pytest.fixture(autouse=True)
def meta(monkeypatch) -> None:
    import apis.shared.sessions.metadata as metadata

    monkeypatch.setattr(metadata, "set_session_spawned_from", lambda *a: True)
    monkeypatch.setattr(metadata, "settle_session_spawned_from", lambda *a: None)


@pytest.fixture()
def metrics(monkeypatch) -> List[tuple]:
    calls: List[tuple] = []
    monkeypatch.setattr(notify_module, "emit_handoff_metric", lambda name, kind, **kw: calls.append((name, kw)))
    return calls


def inbox_items(tables) -> List[Dict[str, Any]]:
    return tables[1].scan()["Items"]


def first_turn(store: HandoffStore, email: str | None = EMAIL) -> HandoffTurn:
    turn = HandoffTurn.for_turn(
        user_id="alice",
        session_id="tgt",
        agent_id="rubric",
        handoff_context=HandoffContextRequest(handoff_id="h1", text=CARD),
        session_row=None,
        is_resume=False,
        is_continuation=False,
        user_email=email,
        store=store,
    )
    assert turn is not None
    return turn


def later_turn(store: HandoffStore) -> HandoffTurn:
    turn = HandoffTurn.for_turn(
        user_id="alice",
        session_id="tgt",
        agent_id="rubric",
        handoff_context=None,
        session_row={"spawnedFrom": {"sessionId": "src", "handoffId": "h1"}},
        is_resume=True,
        is_continuation=False,
        user_email=EMAIL,
        store=store,
    )
    assert turn is not None
    return turn


def run_turn(turn: HandoffTurn, *chunks: str, disconnected: bool = False) -> None:
    async def run():
        turn.start()
        await turn.context_block()
        for chunk in chunks:
            turn.observe(chunk)
        turn.schedule_status_write(disconnected=disconnected)
        pending = list(turn_module._background)
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(run())


PAUSE = frame("oauth_required", {"providerId": "canvas", "interruptId": "i1", "authorizationUrl": "u"})


class TestInboxWrites:
    def test_a_pause_writes_one_needs_input_entry_with_ids_and_names_only(self, tables, store, metrics):
        run_turn(first_turn(store), PAUSE, frame("done"))

        [item] = inbox_items(tables)
        assert item["PK"] == "INBOX#alice@example.edu"
        assert item["SK"].startswith("NOTIF#")
        assert item["kind"] == "handoff_needs_input"
        assert item["payload"] == {
            "handoffId": "h1",
            "sourceSessionId": "src",
            "targetSessionId": "tgt",
            "agentId": "rubric",
            "agentName": "Rubric Builder",
            "sourceTitle": "Course planning",
        }
        assert "actorEmail" not in item and "readAt" not in item
        assert int(item["ttl"]) > 0
        assert ("HandoffNotified", {"session_id": "src", "detail": "inbox"}) in metrics

    def test_no_request_or_conversation_text_reaches_the_entry(self, tables, store):
        run_turn(first_turn(store), frame("tool_group_summary", {"summary": "Drafted a 4-row rubric"}), frame("done"))

        dumped = json.dumps(inbox_items(tables), default=str)
        for text in (REQUEST, "plan BIO 101", "Drafted a 4-row rubric", "conversation_excerpt"):
            assert text not in dumped

    def test_a_finished_turn_writes_done_and_a_turn_after_it_writes_nothing(self, tables, store):
        run_turn(first_turn(store), frame("done"))
        run_turn(later_turn(store), frame("done"))

        assert [i["kind"] for i in inbox_items(tables)] == ["handoff_done"]

    def test_a_dropped_stream_writes_stopped(self, tables, store):
        run_turn(first_turn(store), frame("message_start"), disconnected=True)

        [item] = inbox_items(tables)
        assert item["kind"] == "handoff_stopped"

    def test_an_errored_turn_writes_stopped(self, tables, store):
        run_turn(first_turn(store), frame("stream_error", {"message": "x"}), frame("done"))
        assert [i["kind"] for i in inbox_items(tables)] == ["handoff_stopped"]

    def test_a_stop_the_user_pressed_writes_nothing(self, tables, store):
        run_turn(first_turn(store), frame("message_stop", {"stopReason": "stopped"}), frame("done"))

        assert store.get("alice", "h1").status == "stopped"
        assert inbox_items(tables) == []

    def test_each_state_change_that_needs_the_user_gets_its_own_entry(self, tables, store):
        run_turn(first_turn(store), PAUSE, frame("done"))
        run_turn(later_turn(store), frame("tool_approval_required", {"interruptId": "i2", "toolName": "t"}), frame("done"))
        run_turn(later_turn(store), frame("done"))

        kinds = sorted(i["kind"] for i in inbox_items(tables))
        assert kinds == ["handoff_done", "handoff_needs_input", "handoff_needs_input"]

    def test_a_pre_flight_consent_is_not_needs_input(self, tables, store):
        run_turn(first_turn(store), frame("oauth_required", {"providerId": "canvas"}), frame("done"))
        assert [i["kind"] for i in inbox_items(tables)] == ["handoff_done"]

    def test_without_an_email_the_row_still_moves_and_nothing_is_written(self, tables, store):
        run_turn(first_turn(store, email=None), frame("done"))

        assert store.get("alice", "h1").status == "done"
        assert inbox_items(tables) == []

    def test_a_turn_that_did_not_take_the_handoff_writes_nothing(self, tables, store):
        turn = HandoffTurn.for_turn(
            user_id="alice",
            session_id="tgt",
            agent_id="someone-else",
            handoff_context=HandoffContextRequest(handoff_id="h1", text=CARD),
            session_row=None,
            is_resume=False,
            is_continuation=False,
            user_email=EMAIL,
            store=store,
        )
        run_turn(turn, frame("done"))
        assert inbox_items(tables) == []

    def test_nothing_is_written_while_the_flag_is_off(self, tables, store, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "false")
        turn = HandoffTurn.for_turn(
            user_id="alice",
            session_id="tgt",
            agent_id="rubric",
            handoff_context=HandoffContextRequest(handoff_id="h1", text=CARD),
            session_row={"spawnedFrom": {"sessionId": "src", "handoffId": "h1"}},
            is_resume=False,
            is_continuation=False,
            user_email=EMAIL,
            store=store,
        )
        assert turn is None
        assert inbox_items(tables) == []

    def test_a_failed_inbox_write_never_raises_and_the_row_still_moves(self, tables, store, monkeypatch):
        class _Broken:
            def notify_own(self, **kwargs):
                raise RuntimeError("throttled")

        monkeypatch.setattr(notify_module, "_service", _Broken())
        run_turn(first_turn(store), frame("done"))
        assert store.get("alice", "h1").status == "done"


class TestMarkReadMatching:
    def write(self, inbox: NotificationService, kind: str, handoff_id: str) -> str:
        n = inbox.notify_own(recipient_email=EMAIL, kind=kind, payload={"handoffId": handoff_id})  # type: ignore[arg-type]
        assert n is not None
        return n.notification_id

    def test_marks_only_that_handoffs_entries_of_those_kinds(self, tables):
        inbox = tables[2]
        needs = self.write(inbox, "handoff_needs_input", "h1")
        done = self.write(inbox, "handoff_done", "h1")
        other = self.write(inbox, "handoff_needs_input", "h2")

        marked = inbox.mark_read_matching(EMAIL, kinds=["handoff_needs_input", "handoff_stopped"], payload={"handoffId": "h1"})

        assert marked == 1
        unread = {n.notification_id for n in inbox.list(EMAIL, unread_only=True)[0]}
        assert unread == {done, other}
        assert needs not in unread

    def test_is_idempotent_and_scoped_to_the_callers_inbox(self, tables):
        inbox = tables[2]
        self.write(inbox, "handoff_done", "h1")

        assert inbox.mark_read_matching("someone@else.edu", kinds=["handoff_done"], payload={"handoffId": "h1"}) == 0
        assert inbox.mark_read_matching(EMAIL, kinds=["handoff_done"], payload={"handoffId": "h1"}) == 1
        assert inbox.mark_read_matching(EMAIL, kinds=["handoff_done"], payload={"handoffId": "h1"}) == 0
        assert inbox.unread_count(EMAIL) == 0

    def test_notify_own_reaches_the_user_who_caused_it(self, tables):
        """``notify`` skips the actor; a handoff's recipient is the one who started it."""
        inbox = tables[2]
        assert inbox.notify_own(recipient_email=EMAIL, kind="handoff_done", payload={}) is not None
        assert inbox.unread_count(EMAIL) == 1
