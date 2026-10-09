"""The user's sidebar layout, saved in their settings.

``sidebarItems`` is the order and visibility of the sidebar's navigation
entries. The entries themselves are defined by the SPA, so the route checks
the layout's shape (id format, uniqueness, size) rather than a fixed list;
null or an empty list clears it back to the SPA's default.
"""

from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

import apis.app_api.user_settings.routes as settings_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.user_settings.models import MAX_SIDEBAR_ITEMS
from apis.shared.user_settings.repository import UserSettingsRepository

TABLE = "test-user-settings"
USER = User(user_id="u-1", email="u1@example.edu", name="U", roles=["default"])

LAYOUT = [
    {"id": "artifacts", "visible": True},
    {"id": "agents", "visible": False},
    {"id": "customize", "visible": True},
]


@pytest.fixture()
def repo(monkeypatch) -> UserSettingsRepository:
    for k, v in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "DYNAMODB_USER_SETTINGS_TABLE_NAME": TABLE,
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK")],
            BillingMode="PAY_PER_REQUEST",
        )
        yield UserSettingsRepository(table_name=TABLE)


@pytest.fixture()
def client(repo) -> TestClient:
    app = FastAPI()
    app.include_router(settings_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: USER
    app.dependency_overrides[settings_routes.get_user_settings_repository] = lambda: repo
    return TestClient(app)


def test_default_is_unset(client):
    assert client.get("/users/me/settings").json()["sidebarItems"] is None


def test_saves_and_reads_back_order_and_visibility(client):
    res = client.put("/users/me/settings", json={"sidebarItems": LAYOUT})
    assert res.status_code == 200, res.text
    assert res.json()["sidebarItems"] == LAYOUT
    assert client.get("/users/me/settings").json()["sidebarItems"] == LAYOUT


@pytest.mark.parametrize("clear", [None, []])
def test_null_or_empty_clears_back_to_the_default(client, clear):
    client.put("/users/me/settings", json={"sidebarItems": LAYOUT})
    res = client.put("/users/me/settings", json={"sidebarItems": clear})
    assert res.status_code == 200, res.text
    assert res.json()["sidebarItems"] is None
    assert client.get("/users/me/settings").json()["sidebarItems"] is None


@pytest.mark.parametrize(
    "items",
    [
        [{"id": "agents", "visible": True}, {"id": "agents", "visible": False}],
        [{"id": "Agents", "visible": True}],
        [{"id": "../admin", "visible": True}],
        [{"id": "a" * 41, "visible": True}],
        [{"id": "agents"}],
        [{"id": f"item-{i}", "visible": True} for i in range(MAX_SIDEBAR_ITEMS + 1)],
    ],
    ids=["duplicate", "uppercase", "path", "too-long", "no-visible", "too-many"],
)
def test_refuses_a_malformed_layout(client, items):
    res = client.put("/users/me/settings", json={"sidebarItems": items})
    assert res.status_code == 422
    assert client.get("/users/me/settings").json()["sidebarItems"] is None


def test_other_settings_survive_a_layout_change(client):
    client.put("/users/me/settings", json={"personalInstructions": "Be brief.", "voiceId": "amy"})
    client.put("/users/me/settings", json={"sidebarItems": LAYOUT})
    body = client.get("/users/me/settings").json()
    assert body["personalInstructions"] == "Be brief."
    assert body["voiceId"] == "amy"


def test_layout_survives_a_write_of_another_setting(client):
    # The repository writes with read-modify-put_item, so a key `get_settings`
    # does not read back is dropped by the next write of any other setting —
    # including the agent's `set_default_model` tool, which goes through the
    # repository directly.
    client.put("/users/me/settings", json={"sidebarItems": LAYOUT})
    client.put("/users/me/settings", json={"voiceId": "amy"})
    assert client.get("/users/me/settings").json()["sidebarItems"] == LAYOUT
