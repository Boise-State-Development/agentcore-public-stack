"""Relayed prompts on the source's ``GET /messages`` (handoffs spec §4.3, PR-0.3c)."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import boto3
import pytest
from moto import mock_aws

from agents.main_agent.session.hooks.prefix_fingerprint import compute_prefix_fingerprint
from apis.shared.handoff.models import Handoff, HandoffView, WaitingOn
from apis.shared.handoff.relay import load_relayed_prompts, relayed_prompts_from_row
from apis.shared.handoff.replay import load_handoff_replay
from apis.shared.handoff.store import HandoffStore
from apis.shared.sessions.metadata import (
    SessionMetaSnapshot,
    clear_paused_turn,
    remove_pending_interrupts,
)

REGION = "us-west-2"
TABLE = "test-sessions-metadata"
NOW = datetime.now(timezone.utc)
LIVE = (NOW + timedelta(hours=1)).isoformat()
EXPIRED = (NOW - timedelta(minutes=1)).isoformat()


@pytest.fixture()
def table(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "HANDOFFS_ENABLED": "true",
        "DYNAMODB_SESSIONS_METADATA_TABLE_NAME": TABLE,
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
def store(table) -> HandoffStore:
    return HandoffStore(table=table)


def oauth(interrupt_id: str, provider: str = "canvas") -> Dict[str, Any]:
    return {"interruptId": interrupt_id, "kind": "oauth", "providerId": provider, "createdAt": NOW.isoformat()}


def approval(interrupt_id: str = "a1") -> Dict[str, Any]:
    return {
        "interruptId": interrupt_id,
        "kind": "tool_approval",
        "toolUseId": "tu1",
        "toolName": "create_assignment",
        "toolInput": json.dumps({"course": 101, "points": 4.5}),
        "message": "Creates an assignment in Canvas",
        "triggeringMessageId": "msg-tgt-3",
        "createdAt": NOW.isoformat(),
    }


def question(interrupt_id: str = "q1") -> Dict[str, Any]:
    return {
        "interruptId": interrupt_id,
        "kind": "user_question",
        "questions": json.dumps([{"header": "Scale", "question": "Which scale?", "multiSelect": False, "options": [{"label": "4-point"}]}]),
        "createdAt": NOW.isoformat(),
    }


def sign_in(interrupt_id: str = "b1") -> Dict[str, Any]:
    return {
        "interruptId": interrupt_id,
        "kind": "browser_login",
        "browserSession": json.dumps({"browserSessionId": "bs1", "browserId": "br1"}),
        "reason": "Sign in to the course site",
        "createdAt": NOW.isoformat(),
    }


def meta_row(
    table,
    session_id: str,
    *,
    user_id: str = "alice",
    interrupts: Optional[List[Dict[str, Any]]] = None,
    expires_at: Optional[str] = LIVE,
    **extra: Any,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "PK": f"USER#{user_id}",
        "SK": f"S#ACTIVE#{session_id}",
        "GSI_PK": f"SESSION#{session_id}",
        "GSI_SK": "META",
        "userId": user_id,
        "sessionId": session_id,
        **extra,
    }
    if interrupts is not None:
        item["pendingInterrupts"] = interrupts
    if expires_at is not None:
        item["pausedTurn"] = {"expiresAt": expires_at, "modelId": "m"}
    table.put_item(Item=item)
    return item


def send(store: HandoffStore, handoff_id: str = "h1", *, target: str = "tgt", user_id: str = "alice") -> None:
    now = NOW.isoformat()
    store.create(
        Handoff(
            handoff_id=handoff_id,
            user_id=user_id,
            session_id="src",
            kind="mention",
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
    store.accept(user_id, handoff_id, target_session_id=target, context_edited=False, context_removed=False)


def pause(store: HandoffStore, handoff_id: str = "h1", kind: str = "oauth_consent", name: str = "canvas") -> None:
    store.transition("alice", handoff_id, "needs_input", waitingOn=WaitingOn(kind=kind, name=name).model_dump())


def replay(store, user_id: str = "alice"):
    handoffs, _ = asyncio.run(load_handoff_replay("src", user_id, None, store=store))
    return handoffs


class TestOnlyForNeedsInput:
    def test_a_waiting_handoff_relays_what_its_target_waits_on(self, table, store):
        send(store)
        pause(store)
        meta_row(table, "tgt", interrupts=[oauth("i1")])

        [view] = replay(store)

        assert view["relayedPrompts"] == [{"interruptId": "i1", "kind": "oauth", "providerId": "canvas"}]

    @pytest.mark.parametrize("status", ["running", "done", "stopped"])
    def test_any_other_status_relays_nothing_and_reads_nothing(self, store, status):
        send(store)
        if status != "running":
            store.transition("alice", "h1", status)
        reads: List[str] = []

        async def loader(session_id: str, user_id: str) -> SessionMetaSnapshot:
            reads.append(session_id)
            return SessionMetaSnapshot(row={"pendingInterrupts": [oauth("i1")], "pausedTurn": {"expiresAt": LIVE}}, owned_by_other=False)

        handoffs, _ = asyncio.run(load_handoff_replay("src", "alice", None, store=store, meta_loader=loader))

        assert "relayedPrompts" not in handoffs[0]
        assert reads == []

    def test_a_superseded_pause_relays_nothing(self, table, store):
        # A fresh turn in the target clears the breadcrumbs and the snapshot (#874).
        send(store)
        pause(store)
        meta_row(table, "tgt", interrupts=None, expires_at=None)

        [view] = replay(store)

        assert view["relayedPrompts"] == []

    def test_an_expired_pause_relays_nothing(self, table, store):
        send(store)
        pause(store)
        meta_row(table, "tgt", interrupts=[oauth("i1")], expires_at=EXPIRED)

        assert replay(store)[0]["relayedPrompts"] == []

    def test_breadcrumbs_without_a_snapshot_relay_nothing(self, table, store):
        send(store)
        pause(store)
        meta_row(table, "tgt", interrupts=[oauth("i1")], expires_at=None)

        assert replay(store)[0]["relayedPrompts"] == []


class TestOwnerScoping:
    def test_another_users_target_gives_no_relay_and_no_error(self, table, store):
        # Alice's row names a target that is Bob's conversation: Alice has no META row there.
        send(store)
        pause(store)
        meta_row(table, "tgt", user_id="bob", interrupts=[oauth("i1")])

        [view] = replay(store)

        assert view["status"] == "needs_input"
        assert view["relayedPrompts"] == []

    def test_the_target_is_read_for_the_caller_when_two_users_share_its_id(self, table, store):
        send(store)
        pause(store)
        meta_row(table, "tgt", user_id="bob", interrupts=[oauth("bobs", provider="github")])
        meta_row(table, "tgt", user_id="alice", interrupts=[oauth("alices")])

        assert replay(store)[0]["relayedPrompts"] == [{"interruptId": "alices", "kind": "oauth", "providerId": "canvas"}]

    def test_another_users_source_handoffs_are_never_listed(self, table, store):
        send(store, user_id="bob")
        store.transition("bob", "h1", "needs_input", waitingOn={"kind": "oauth_consent", "name": "canvas"})
        meta_row(table, "tgt", user_id="bob", interrupts=[oauth("i1")])

        assert replay(store) == []

    def test_a_failed_target_read_leaves_the_row_without_a_relay(self, store):
        send(store)
        pause(store)

        async def broken(session_id: str, user_id: str) -> SessionMetaSnapshot:
            raise RuntimeError("throttled")

        handoffs, _ = asyncio.run(load_handoff_replay("src", "alice", None, store=store, meta_loader=broken))

        assert handoffs[0]["status"] == "needs_input"
        assert "relayedPrompts" not in handoffs[0]


class TestWhatIsRelayed:
    def test_each_kind_keeps_what_its_prompt_needs_and_nothing_of_the_target(self):
        prompts = relayed_prompts_from_row(
            {"pendingInterrupts": [approval(), question(), sign_in()], "pausedTurn": {"expiresAt": LIVE}}
        )
        dumped = [p.model_dump(by_alias=True, exclude_none=True) for p in prompts]

        assert dumped[0] == {
            "interruptId": "a1",
            "kind": "tool_approval",
            "toolUseId": "tu1",
            "toolName": "create_assignment",
            "toolInput": json.dumps({"course": 101, "points": 4.5}),
            "message": "Creates an assignment in Canvas",
        }
        assert dumped[1]["questions"] == question()["questions"]
        # The live view is the target's screen: the relay links there and carries none of it.
        assert dumped[2] == {"interruptId": "b1", "kind": "browser_login"}

    def test_consents_from_one_pause_are_deduplicated_by_provider(self):
        prompts = relayed_prompts_from_row(
            {
                "pendingInterrupts": [oauth("i1"), oauth("i2"), oauth("i3", provider="github")],
                "pausedTurn": {"expiresAt": LIVE},
            }
        )

        assert [(p.interrupt_id, p.provider_id) for p in prompts] == [("i1", "canvas"), ("i3", "github")]

    def test_client_tools_are_never_relayed(self):
        crumb = {"interruptId": "c1", "kind": "client_tool", "toolName": "shell", "createdAt": NOW.isoformat()}

        assert relayed_prompts_from_row({"pendingInterrupts": [crumb], "pausedTurn": {"expiresAt": LIVE}}) == []

    def test_a_legacy_breadcrumb_with_no_kind_is_a_consent(self):
        crumb = {"interruptId": "old", "providerId": "canvas", "createdAt": NOW.isoformat()}

        [prompt] = relayed_prompts_from_row({"pendingInterrupts": [crumb], "pausedTurn": {"expiresAt": LIVE}})

        assert (prompt.kind, prompt.provider_id) == ("oauth", "canvas")

    def test_views_load_concurrently_one_read_per_waiting_handoff(self):
        views = [
            HandoffView(
                handoff_id=f"h{i}", session_id="src", kind="mention", status=status, agent_id="a", agent_name="A",
                request="r", source_message_index=0, excerpt_tokens=0, created_at="t", updated_at="t",
                target_session_id=f"t{i}",
            )
            for i, status in enumerate(["needs_input", "running", "needs_input"])
        ]
        reads: List[str] = []

        async def loader(session_id: str, user_id: str) -> SessionMetaSnapshot:
            reads.append(session_id)
            return SessionMetaSnapshot(row=None, owned_by_other=False)

        asyncio.run(load_relayed_prompts(views, "alice", meta_loader=loader))

        assert sorted(reads) == ["t0", "t2"]
        assert [v.relayed_prompts for v in views] == [[], None, []]


TOOLS = [{"toolSpec": {"name": "search", "inputSchema": {"json": {"type": "object"}}}}]
SYSTEM = [{"text": "You are a course-planning assistant."}, {"cachePoint": {"type": "default"}}]
SOURCE_HISTORY = [
    {"role": "user", "content": [{"text": "plan BIO 101"}]},
    {"role": "assistant", "content": [{"text": "Here is a plan."}]},
    {"role": "user", "content": [{"text": "and the lab reports?"}]},
]


def test_a_relayed_and_answered_pause_leaves_the_source_prefix_unchanged(table, store):
    """Nothing about the relay reaches the source's model (§4.3 "It never enters the source's model history").

    The source's next turn is built from its ``META`` row and its stored history, with its tool list
    and system prompt. The relay reads the target and the answer resumes the target, so none of
    those move: its three prefix hashes are what they were before the target paused.
    """
    source_meta = meta_row(table, "src", title="Course planning", compactionCheckpoint=0)
    send(store)
    meta_row(table, "tgt", interrupts=[oauth("i1"), approval()])
    before = compute_prefix_fingerprint(TOOLS, SYSTEM, copy.deepcopy(SOURCE_HISTORY))
    source_items_before = _items(table, "USER#alice", exclude_prefix="HANDOFF#")

    # The target pauses; the source reads its relay (GET /messages, and the row read at the turn's end).
    pause(store)
    [view] = replay(store)
    assert [p["kind"] for p in view["relayedPrompts"]] == ["oauth", "tool_approval"]
    # The relay is the target's, by construction: none of the source's text is in it.
    assert all(text not in json.dumps(view["relayedPrompts"]) for text in ("plan BIO 101", "Here is a plan."))

    # The user answers from the source: the resume goes to the target, which clears its own pause
    # and, when its turn ends, moves the handoff row.
    asyncio.run(remove_pending_interrupts("tgt", "alice", {"i1", "a1"}))
    asyncio.run(clear_paused_turn("tgt", "alice"))
    store.transition("alice", "h1", "done", statusLine="Drafted a rubric")
    assert replay(store)[0].get("relayedPrompts") is None

    # The source's row is exactly what it was, and the request its next turn sends hashes the same.
    assert _items(table, "USER#alice", exclude_prefix="HANDOFF#", only_session="src") == [
        i for i in source_items_before if i.get("sessionId") == "src"
    ]
    assert _items(table, "USER#alice", exclude_prefix="HANDOFF#", only_session="src")[0] == source_meta
    after = compute_prefix_fingerprint(TOOLS, SYSTEM, copy.deepcopy(SOURCE_HISTORY))
    for key in ("toolConfigHash", "systemPromptHash", "historyHash"):
        assert after[key] == before[key]


def _items(table, pk: str, *, exclude_prefix: str, only_session: Optional[str] = None) -> List[Dict[str, Any]]:
    items = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(pk))["Items"]
    kept = [i for i in items if not str(i["SK"]).startswith(exclude_prefix)]
    if only_session is not None:
        kept = [i for i in kept if i.get("sessionId") == only_session]
    return sorted(kept, key=lambda i: i["SK"])
