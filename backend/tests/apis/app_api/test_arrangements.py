"""List arrangements over a moto user-settings table.

docs/specs/agent-project-organization.md §5 and §7: the flag (404 after auth),
the defaults, a whole-item replace per surface, the caps and id checks, ids the
caller cannot reach kept as sent (D3), and storage on its own item so the
``SETTINGS`` item the turn path reads every turn does not grow.
"""

from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.app_api.arrangements import routes as arrangement_routes
from apis.shared.arrangements.models import MAX_ARRANGEMENT_IDS
from apis.shared.arrangements.repository import ArrangementRepository
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import organize_lists_enabled

TABLE = "test-user-settings"
USER = User(user_id="u-1", email="u1@example.edu", name="U", roles=["default"])
OTHER = User(user_id="u-2", email="u2@example.edu", name="O", roles=["default"])

ARRANGED = {
    "sort": "custom",
    "order": ["ast-0000000000aa", "ast-0000000000bb", "ast-0000000000cc"],
    "hidden": ["ast-0000000000dd"],
}


@pytest.fixture()
def repo(monkeypatch) -> ArrangementRepository:
    for k, v in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "ORGANIZE_LISTS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK")],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ArrangementRepository(table_name=TABLE)


def _client(repo: ArrangementRepository, user: User = USER) -> TestClient:
    app = FastAPI()
    app.include_router(arrangement_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    app.dependency_overrides[arrangement_routes.get_arrangement_repository] = lambda: repo
    return TestClient(app)


@pytest.fixture()
def client(repo) -> TestClient:
    return _client(repo)


def _raw_items(user_id: str) -> dict:
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
    items = table.query(
        KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": f"USER#{user_id}"}
    )["Items"]
    return {item["SK"]: item for item in items}


# --------------------------------------------------------------------- flag


def test_flag_reader_only_true_enables(monkeypatch):
    for value, expected in [("", False), ("false", False), ("yes", False), ("true", True), (" TRUE ", True)]:
        monkeypatch.setenv("ORGANIZE_LISTS_ENABLED", value)
        assert organize_lists_enabled() is expected, value
    monkeypatch.delenv("ORGANIZE_LISTS_ENABLED")
    assert organize_lists_enabled() is False


def test_flag_off_is_404_after_auth(client, monkeypatch):
    monkeypatch.setenv("ORGANIZE_LISTS_ENABLED", "")
    assert client.get("/users/me/arrangements/agents").status_code == 404
    assert client.put("/users/me/arrangements/agents", json=ARRANGED).status_code == 404

    del client.app.dependency_overrides[get_current_user_from_session]
    assert client.get("/users/me/arrangements/agents").status_code == 401


# --------------------------------------------------------------- read/write


def test_no_item_reads_as_the_defaults(client):
    res = client.get("/users/me/arrangements/projects")
    assert res.status_code == 200, res.text
    assert res.json() == {"sort": "recent", "order": [], "hidden": [], "updatedAt": None}


def test_put_replaces_and_reads_back(client):
    res = client.put("/users/me/arrangements/agents", json=ARRANGED)
    assert res.status_code == 200, res.text
    body = res.json()
    assert {k: body[k] for k in ARRANGED} == ARRANGED
    assert body["updatedAt"]

    again = {"sort": "name", "order": ["ast-0000000000cc"], "hidden": []}
    client.put("/users/me/arrangements/agents", json=again)
    read = client.get("/users/me/arrangements/agents").json()
    assert {k: read[k] for k in again} == again


def test_surfaces_and_users_are_separate(repo, client):
    client.put("/users/me/arrangements/agents", json=ARRANGED)
    assert client.get("/users/me/arrangements/projects").json()["order"] == []
    assert _client(repo, OTHER).get("/users/me/arrangements/agents").json()["order"] == []


def test_stored_on_its_own_item_not_settings(client):
    client.put("/users/me/arrangements/agents", json=ARRANGED)
    client.put("/users/me/arrangements/projects", json={"sort": "recent", "order": [], "hidden": ["prj_abc"]})
    assert set(_raw_items(USER.user_id)) == {"ARRANGEMENT#agents", "ARRANGEMENT#projects"}


def test_unreachable_ids_are_kept_as_sent(client):
    """D3: the route checks shape, never access, so an id the caller cannot reach survives."""
    sent = {"sort": "custom", "order": ["ast-gone00000000", "prj_" + "f" * 32], "hidden": ["ast-gone00000000"]}
    client.put("/users/me/arrangements/agents", json=sent)
    read = client.get("/users/me/arrangements/agents").json()
    assert read["order"] == sent["order"]
    assert read["hidden"] == sent["hidden"]


# --------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "body",
    [
        {"sort": "custom", "order": ["a"] * 2, "hidden": []},
        {"sort": "custom", "order": [], "hidden": ["b", "b"]},
        {"sort": "custom", "order": ["has space"], "hidden": []},
        {"sort": "custom", "order": ["x" * 65], "hidden": []},
        {"sort": "custom", "order": [""], "hidden": []},
        {"sort": "popular", "order": [], "hidden": []},
        {"order": [], "hidden": []},
        {"sort": "recent", "order": [], "hidden": [], "pinned": []},
        {"sort": "custom", "order": [f"id-{i}" for i in range(MAX_ARRANGEMENT_IDS + 1)], "hidden": []},
    ],
)
def test_invalid_bodies_are_422(client, body):
    assert client.put("/users/me/arrangements/agents", json=body).status_code == 422


def test_cap_is_inclusive(client):
    body = {"sort": "custom", "order": [f"id-{i}" for i in range(MAX_ARRANGEMENT_IDS)], "hidden": []}
    assert client.put("/users/me/arrangements/agents", json=body).status_code == 200


def test_unknown_surface_is_422(client):
    assert client.get("/users/me/arrangements/skills").status_code == 422


# ------------------------------------------------------------ unconfigured


def test_without_a_table_reads_defaults_and_refuses_writes(monkeypatch):
    monkeypatch.setenv("ORGANIZE_LISTS_ENABLED", "true")
    client = _client(ArrangementRepository(table_name=""))
    assert client.get("/users/me/arrangements/agents").json()["sort"] == "recent"
    assert client.put("/users/me/arrangements/agents", json=ARRANGED).status_code == 503
