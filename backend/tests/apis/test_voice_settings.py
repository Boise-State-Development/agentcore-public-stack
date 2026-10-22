"""The user's voice-mode voice, saved in their settings.

``voiceId`` is the Nova 2 Sonic voice the SPA sends on every voice connect.
A stored id Bedrock rejects would break every later session, so the route
refuses unknown ids; blank clears back to the platform default.
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


def test_default_is_unset(client):
    assert client.get("/users/me/settings").json()["voiceId"] is None


def test_saves_a_catalog_voice_and_normalises_it(client):
    res = client.put("/users/me/settings", json={"voiceId": " Matthew "})
    assert res.status_code == 200, res.text
    assert res.json()["voiceId"] == "matthew"
    assert client.get("/users/me/settings").json()["voiceId"] == "matthew"


def test_refuses_a_voice_bedrock_would_reject(client):
    res = client.put("/users/me/settings", json={"voiceId": "siri"})
    assert res.status_code == 422
    assert client.get("/users/me/settings").json()["voiceId"] is None


def test_blank_clears_back_to_the_default(client):
    client.put("/users/me/settings", json={"voiceId": "amy"})
    res = client.put("/users/me/settings", json={"voiceId": ""})
    assert res.status_code == 200, res.text
    assert res.json()["voiceId"] is None
    assert client.get("/users/me/settings").json()["voiceId"] is None


def test_other_settings_survive_a_voice_change(client):
    client.put("/users/me/settings", json={"personalInstructions": "Be brief."})
    client.put("/users/me/settings", json={"voiceId": "lupe"})
    body = client.get("/users/me/settings").json()
    assert body["personalInstructions"] == "Be brief."
    assert body["voiceId"] == "lupe"
