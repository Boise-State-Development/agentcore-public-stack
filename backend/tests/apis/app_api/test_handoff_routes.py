"""Route-level tests for ``/sessions/{id}/handoffs`` (handoffs spec §4.2, PR-0.1).

The store is real, over a moto table, so owner scoping and the state machine are the store's own.
The session, Agent and message lookups are stubbed at the route module: these tests prove the
gating, what is rendered and recorded, and that nothing creates a target session.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.handoffs import routes
from apis.shared.auth.models import User
from apis.shared.handoff.context import build_handoff_message, context_hash, wrap_handoff_context
from apis.shared.handoff.store import HandoffStore
from apis.shared.sessions.models import MessageResponse, MessagesListResponse

REGION = "us-west-2"
TABLE = "test-sessions-metadata"

ALICE = User(email="alice@example.edu", user_id="alice", name="Alice", roles=["User"])
BOB = User(email="bob@example.edu", user_id="bob", name="Bob", roles=["User"])


def _message(index: int, role: str, text: str, display: Optional[str] = None) -> MessageResponse:
    return MessageResponse(
        id=f"msg-src-{index}",
        role=role,
        content=[{"type": "text", "text": text}],
        created_at="2026-10-22T00:00:00Z",
        metadata={"displayText": display} if display else None,
    )


CONVERSATION = [
    _message(0, "user", "augmented with chunks\n\nplan BIO 101", display="plan BIO 101"),
    _message(1, "assistant", "Here is a week-by-week plan."),
    _message(2, "user", "add a lab each week"),
    _message(3, "assistant", "Added labs."),
]


class _World:
    def __init__(self) -> None:
        self.sessions: Dict[tuple, Any] = {("src", "alice"): SimpleNamespace(title="Course planning")}
        self.agents: Dict[str, Any] = {"rubric": SimpleNamespace(name="Rubric Builder")}
        self.agent_users = {("rubric", "alice")}
        self.messages: List[MessageResponse] = list(CONVERSATION)
        self.compaction = (0, None)


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
def world(monkeypatch) -> _World:
    w = _World()

    async def get_session_metadata(session_id: str, user_id: str):
        return w.sessions.get((session_id, user_id))

    async def get_assistant_with_access_check(assistant_id: str, user_id: str, user_email: str = None):
        if (assistant_id, user_id) in w.agent_users:
            return w.agents[assistant_id], "viewer"
        return None, None

    async def get_messages(session_id: str, user_id: str, limit=None, next_token=None):
        return MessagesListResponse(messages=w.messages)

    async def get_session_compaction(session_id: str, user_id: str):
        return w.compaction

    monkeypatch.setattr(routes, "get_session_metadata", get_session_metadata)
    monkeypatch.setattr(routes, "get_assistant_with_access_check", get_assistant_with_access_check)
    monkeypatch.setattr(routes, "get_messages", get_messages)
    monkeypatch.setattr(routes, "get_session_compaction", get_session_compaction)
    return w


@pytest.fixture()
def store(table) -> HandoffStore:
    return HandoffStore(table=table)


@pytest.fixture()
def make_client(monkeypatch, store, world):
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")

    def build(user: User = ALICE) -> TestClient:
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.get_current_user_from_session] = lambda: user
        app.dependency_overrides[routes.get_handoff_store] = lambda: store
        return TestClient(app)

    return build


def _create(client: TestClient, **body: Any):
    payload = {"kind": "mention", "agentId": "rubric", "request": " draft a rubric for this "}
    payload.update(body)
    return client.post("/sessions/src/handoffs", json=payload)


def test_create_returns_the_card_and_the_exact_prompt(make_client, store):
    resp = _create(make_client())

    assert resp.status_code == 201
    data = resp.json()
    excerpt = data["excerpt"]
    assert excerpt.startswith('<conversation_excerpt turns="1-2" of="2">')
    assert "User: plan BIO 101" in excerpt
    assert "augmented with chunks" not in excerpt
    assert data["contextBlock"] == wrap_handoff_context(excerpt, "Course planning")
    assert data["prompt"] == build_handoff_message(data["contextBlock"], "draft a rubric for this")
    assert data["request"] == "draft a rubric for this"
    assert data["sourceTitle"] == "Course planning"
    assert data["sourceMessageIndex"] == 3
    assert data["excerptHash"] == context_hash(excerpt)
    assert data["turnsCovered"] == {"first": 1, "last": 2, "of": 2, "omitted": 0}
    assert data["excerptTokens"] > 0

    row = store.get("alice", data["handoffId"])
    assert (row.status, row.kind, row.session_id) == ("open", "mention", "src")
    assert (row.agent_id, row.agent_name, row.source_title) == ("rubric", "Rubric Builder", "Course planning")
    assert row.source_message_index == 3
    assert row.target_session_id is None
    assert row.excerpt_hash == data["excerptHash"]


def test_create_uses_the_compaction_summary(make_client, world):
    world.compaction = (2, "We planned BIO 101 week by week.")

    excerpt = _create(make_client()).json()["excerpt"]

    assert "[Summary of turns 1]\nWe planned BIO 101 week by week." in excerpt
    assert "plan BIO 101" not in excerpt
    assert "add a lab each week" in excerpt


def test_an_empty_conversation_hands_off_with_no_context(make_client, world):
    world.messages = []

    data = _create(make_client()).json()

    assert (data["excerpt"], data["contextBlock"], data["turnsCovered"]) == ("", "", None)
    assert data["prompt"] == "draft a rubric for this"


def test_another_users_conversation_is_a_404(make_client):
    assert _create(make_client(BOB)).status_code == 404


def test_an_agent_the_user_cannot_use_is_a_404(make_client, world, store):
    world.agents["private"] = SimpleNamespace(name="Someone else's")

    assert _create(make_client(), agentId="private").status_code == 404
    assert _create(make_client(), agentId="missing").status_code == 404
    assert store.list_for_session("alice", "src") == []


def test_only_mention_handoffs_are_created_here(make_client):
    assert _create(make_client(), kind="send_back").status_code == 422


def test_get_returns_the_row_and_hides_storage_fields(make_client):
    client = make_client()
    handoff_id = _create(client).json()["handoffId"]

    data = client.get(f"/sessions/src/handoffs/{handoff_id}").json()

    assert data["status"] == "open"
    assert data["agentName"] == "Rubric Builder"
    assert data["request"] == "draft a rubric for this"
    for hidden in ("userId", "PK", "SK", "excerptHash", "sourceTitle"):
        assert hidden not in data


def test_an_old_open_handoff_reads_as_abandoned(make_client, store, table):
    client = make_client()
    handoff_id = _create(client).json()["handoffId"]
    table.update_item(
        Key={"PK": "USER#alice", "SK": f"HANDOFF#{handoff_id}"},
        UpdateExpression="SET createdAt = :old",
        ExpressionAttributeValues={":old": "2026-01-01T00:00:00+00:00"},
    )

    assert client.get(f"/sessions/src/handoffs/{handoff_id}").json()["status"] == "abandoned"


def test_another_users_handoff_id_is_a_404_on_every_route(make_client):
    handoff_id = _create(make_client()).json()["handoffId"]
    bob = make_client(BOB)

    assert bob.get(f"/sessions/src/handoffs/{handoff_id}").status_code == 404
    assert bob.post(f"/sessions/src/handoffs/{handoff_id}/dismiss").status_code == 404


def test_a_handoff_under_the_wrong_conversation_is_a_404(make_client):
    client = make_client()
    handoff_id = _create(client).json()["handoffId"]

    assert client.get(f"/sessions/other/handoffs/{handoff_id}").status_code == 404
    assert client.post(f"/sessions/other/handoffs/{handoff_id}/dismiss").status_code == 404


def test_dismiss_cancels_an_open_handoff_and_is_idempotent(make_client, store):
    client = make_client()
    handoff_id = _create(client).json()["handoffId"]

    assert client.post(f"/sessions/src/handoffs/{handoff_id}/dismiss").status_code == 204
    assert client.post(f"/sessions/src/handoffs/{handoff_id}/dismiss").status_code == 204
    assert store.get("alice", handoff_id).status == "dismissed"


def test_dismissing_a_sent_handoff_is_a_409(make_client, store):
    client = make_client()
    handoff_id = _create(client).json()["handoffId"]
    store.accept("alice", handoff_id, target_session_id="tgt", context_edited=False, context_removed=False)

    assert client.post(f"/sessions/src/handoffs/{handoff_id}/dismiss").status_code == 409
    assert store.get("alice", handoff_id).status == "running"


@pytest.mark.parametrize("value", ["", "false", "1", "yes"])
def test_every_route_404s_while_the_flag_is_off(make_client, monkeypatch, value):
    client = make_client()
    handoff_id = _create(client).json()["handoffId"]
    monkeypatch.setenv("HANDOFFS_ENABLED", value)

    assert _create(client).status_code == 404
    assert client.get(f"/sessions/src/handoffs/{handoff_id}").status_code == 404
    assert client.post(f"/sessions/src/handoffs/{handoff_id}/dismiss").status_code == 404


def test_authentication_runs_before_the_flag(monkeypatch, store, world):
    """Unauthenticated is a 401 whatever the flag, so the surface only reads as absent to a user."""
    from fastapi import HTTPException

    def unauthenticated():
        raise HTTPException(status_code=401, detail="Not authenticated")

    monkeypatch.setenv("HANDOFFS_ENABLED", "false")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user_from_session] = unauthenticated
    app.dependency_overrides[routes.get_handoff_store] = lambda: store

    resp = TestClient(app).post("/sessions/src/handoffs", json={"agentId": "rubric", "request": "x"})

    assert resp.status_code == 401
