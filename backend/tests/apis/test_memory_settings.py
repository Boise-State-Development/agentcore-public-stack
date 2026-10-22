"""The long-term memory switch in user settings (issue #114)."""

from __future__ import annotations

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

import apis.app_api.user_settings.routes as settings_routes
import apis.inference_api.chat.routes as chat_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.user_settings.models import memory_capabilities
from apis.shared.user_settings.repository import UserSettingsRepository

TABLE = "test-user-settings"
USER = User(user_id="u-1", email="u1@example.edu", name="U", roles=["default"])


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


def test_default_is_on(client):
    assert client.get("/users/me/settings").json()["memoryMode"] == "on"


def test_read_only_round_trips(client):
    res = client.put("/users/me/settings", json={"memoryMode": "read_only"})
    assert res.status_code == 200, res.text
    assert res.json()["memoryMode"] == "read_only"
    assert client.get("/users/me/settings").json()["memoryMode"] == "read_only"


def test_the_turns_mode_is_the_users_mode_capped_by_the_conversation():
    assert chat_routes._memory_mode_for_turn(None, {"memoryMode": "read_only"}) == "read_only"
    assert chat_routes._memory_mode_for_turn(None, {"memoryMode": "off"}) == "off"
    assert chat_routes._memory_mode_for_turn(None, {}) == "on"
    assert chat_routes._memory_mode_for_turn("ast-1", {"memoryMode": "on"}) == "off"
    assert memory_capabilities("read_only") == (True, False)
    assert memory_capabilities("off") == (False, False)
