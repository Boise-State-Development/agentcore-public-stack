"""Route tests for send-back and deliveries (handoffs spec §4.3, §4.8, PR-0.4).

Real stores over one moto table; session ``META`` reads, the message list and the artifact index
are stubbed. These prove the two send-back paths, exactly-once delivery, owner scoping and the
refusals, and that nothing navigates or runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.handoffs import routes
from apis.shared.auth.models import User
from apis.shared.handoff import send_back
from apis.shared.handoff.delivery import DeliveryStore
from apis.shared.handoff.store import HandoffStore
from apis.shared.sessions.models import MessageResponse, MessagesListResponse

REGION = "us-west-2"
TABLE = "test-sessions-metadata"

ALICE = User(email="alice@example.edu", user_id="alice", name="Alice", roles=["User"])
BOB = User(email="bob@example.edu", user_id="bob", name="Bob", roles=["User"])


def _message(index: int, role: str, content: List[Dict[str, Any]]) -> MessageResponse:
    return MessageResponse(id=f"msg-tgt-{index}", role=role, content=content, created_at="2026-11-12T00:00:00Z")


TARGET_MESSAGES = [
    _message(0, "user", [{"type": "text", "text": "draft a rubric"}]),
    _message(1, "assistant", [{"type": "text", "text": "Here is a first rubric."}]),
    _message(2, "user", [{"type": "text", "text": "make it four rows"}]),
    _message(
        3,
        "assistant",
        [
            {"type": "reasoningContent", "text": "private thinking"},
            {"type": "toolUse", "toolUse": {"name": "create_artifact"}},
            {"type": "text", "text": "Four rows, done."},
        ],
    ),
]


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


class _World:
    def __init__(self) -> None:
        self.meta: Dict[tuple, Dict[str, Any]] = {
            ("src", "alice"): {"title": "Course planning"},
            ("tgt", "alice"): {
                "title": "Rubric for BIO 101",
                "assistantId": "rubric",
                "spawnedFrom": {"sessionId": "src", "handoffId": "h1"},
            },
            ("plain", "alice"): {"title": "Not a target"},
        }
        self.messages = list(TARGET_MESSAGES)
        self.heads: Dict[tuple, List[Dict[str, Any]]] = {
            ("alice", "tgt"): [
                {"artifact_id": "rubric-doc", "version": 2, "title": "Rubric", "content_type": "text/html",
                 "produced_by_message_index": 3},
            ]
        }


@pytest.fixture()
def world(monkeypatch) -> _World:
    w = _World()

    async def load_session_meta(session_id: str, user_id: str):
        return SimpleNamespace(row=w.meta.get((session_id, user_id)), owned_by_other=False)

    async def get_messages(session_id: str, user_id: str, limit=None, next_token=None):
        return MessagesListResponse(messages=w.messages if (session_id, user_id) == ("tgt", "alice") else [])

    monkeypatch.setattr("apis.shared.sessions.metadata.load_session_meta", load_session_meta)
    monkeypatch.setattr(routes, "get_messages", get_messages)
    monkeypatch.setattr(send_back, "_artifact_heads", lambda user, session: w.heads.get((user, session), []))
    return w


@pytest.fixture()
def stores(table, monkeypatch):
    handoffs, deliveries = HandoffStore(table=table), DeliveryStore(table=table)
    monkeypatch.setattr(send_back, "get_delivery_store", lambda: deliveries)
    return SimpleNamespace(handoffs=handoffs, deliveries=deliveries)


@pytest.fixture()
def client(monkeypatch, stores, world):
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")

    def build(user: User = ALICE) -> TestClient:
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.get_current_user_from_session] = lambda: user
        app.dependency_overrides[routes.get_handoff_store] = lambda: stores.handoffs
        app.dependency_overrides[routes.get_delivery_store] = lambda: stores.deliveries
        return TestClient(app)

    return build


def test_bring_back_result_lands_a_card_on_the_source_at_once(client, stores):
    resp = client().post("/sessions/tgt/handoffs/send-back", json={"deliver": True})

    assert resp.status_code == 201
    body = resp.json()
    assert body["sourceSessionId"] == "src" and body["sourceTitle"] == "Course planning"
    assert body["messageIndex"] == 3 and body["excerpt"] == "Four rows, done."
    assert [a["artifactId"] for a in body["artifacts"]] == ["rubric-doc"]

    [card] = stores.deliveries.list_pending("alice", "src")
    assert card.delivery_id == body["deliveryId"]
    assert (card.from_title, card.text, card.handoff_id) == ("Rubric for BIO 101", "Four rows, done.", body["handoffId"])
    assert [a.artifact_id for a in card.artifacts] == ["rubric-doc"]
    row = stores.handoffs.get("alice", body["handoffId"])
    assert (row.kind, row.status, row.session_id, row.target_session_id) == ("send_back", "done", "tgt", "src")


def test_send_to_title_reviews_first_then_sends_what_the_user_kept(client, stores):
    c = client()
    opened = c.post("/sessions/tgt/handoffs/send-back", json={"messageIndex": 1}).json()
    assert opened["excerpt"] == "Here is a first rubric." and opened["artifacts"] == []
    assert "deliveryId" not in opened
    assert stores.deliveries.list_pending("alice", "src") == []

    sent = c.post(f"/sessions/tgt/handoffs/{opened['handoffId']}/deliver", json={"text": "Edited: first rubric."})

    assert sent.status_code == 200 and sent.json()["sourceSessionId"] == "src"
    [card] = stores.deliveries.list_pending("alice", "src")
    assert card.text == "Edited: first rubric."
    # A second press sends nothing more.
    again = c.post(f"/sessions/tgt/handoffs/{opened['handoffId']}/deliver", json={"text": "again"})
    assert again.status_code == 409
    assert len(stores.deliveries.list_pending("alice", "src")) == 1


def test_a_refused_send_leaves_the_review_open_to_try_again(client, stores):
    c = client()
    opened = c.post("/sessions/tgt/handoffs/send-back", json={}).json()
    url = f"/sessions/tgt/handoffs/{opened['handoffId']}/deliver"

    assert c.post(url, json={"text": "", "artifactIds": []}).status_code == 422
    assert c.post(url, json={"text": "x", "artifactIds": ["not-from-here"]}).status_code == 404
    assert c.post(url, json={"text": "ok"}).status_code == 200


def test_a_send_back_needs_a_target_of_the_callers(client):
    assert client().post("/sessions/plain/handoffs/send-back", json={"deliver": True}).status_code == 404
    assert client(BOB).post("/sessions/tgt/handoffs/send-back", json={"deliver": True}).status_code == 404
    assert client().post("/sessions/tgt/handoffs/send-back", json={"messageIndex": 2}).status_code == 404


def test_a_project_task_cannot_send_back(client, world):
    world.meta[("src", "alice")]["projectId"] = "p1"

    assert client().post("/sessions/tgt/handoffs/send-back", json={"deliver": True}).status_code == 409


def test_deliver_is_only_for_the_callers_send_back_rows(client, stores):
    opened = client().post("/sessions/tgt/handoffs/send-back", json={}).json()

    assert client(BOB).post(f"/sessions/tgt/handoffs/{opened['handoffId']}/deliver", json={"text": "x"}).status_code == 404
    assert client().post(f"/sessions/src/handoffs/{opened['handoffId']}/deliver", json={"text": "x"}).status_code == 404


def test_removing_a_card_dismisses_it_once(client, stores):
    delivery_id = client().post("/sessions/tgt/handoffs/send-back", json={"deliver": True}).json()["deliveryId"]
    url = f"/sessions/src/deliveries/{delivery_id}/dismiss"

    assert client(BOB).post(url).status_code == 404
    assert client().post(f"/sessions/tgt/deliveries/{delivery_id}/dismiss").status_code == 404
    assert client().post(url).status_code == 204
    assert client().post(url).status_code == 204
    assert stores.deliveries.list_pending("alice", "src") == []


def test_a_card_already_sent_cannot_be_removed(client, stores):
    delivery_id = client().post("/sessions/tgt/handoffs/send-back", json={"deliver": True}).json()["deliveryId"]
    stores.deliveries.attach("alice", delivery_id, "src")

    assert client().post(f"/sessions/src/deliveries/{delivery_id}/dismiss").status_code == 409


def test_everything_is_404_while_the_flag_is_off(client, monkeypatch):
    c = client()
    monkeypatch.setenv("HANDOFFS_ENABLED", "false")

    assert c.post("/sessions/tgt/handoffs/send-back", json={"deliver": True}).status_code == 404
    assert c.post("/sessions/src/deliveries/d1/dismiss").status_code == 404
