"""A user's confirmation of a model's data-handling notice (POST /users/me/settings/model-notices).

The server records the time and the digest of the notice the catalog holds now,
so a reworded notice is asked again; only provider_review models are recorded,
and an ordinary settings save keeps what was recorded.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

import apis.app_api.user_settings.routes as settings_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.models.models import ManagedModel
from apis.shared.user_settings.repository import UserSettingsRepository

TABLE = "test-user-settings"
USER = User(user_id="u-1", email="u1@example.edu", name="U", roles=["default"])
FABLE = "global.anthropic.claude-fable-5-2"
FIND = "apis.shared.models.managed_models.find_managed_model_by_model_id"


def _row(model_id: str, **kw) -> ManagedModel:
    return ManagedModel(
        id=f"row-{model_id}", modelId=model_id, modelName=model_id, provider="bedrock",
        providerName="Anthropic", inputModalities=["TEXT"], outputModalities=["TEXT"],
        maxInputTokens=1_000_000, enabled=True, inputPricePerMillionTokens=8.0,
        outputPricePerMillionTokens=40.0, createdAt=datetime(2026, 11, 16),
        updatedAt=datetime(2026, 11, 16), **kw,
    )


@pytest.fixture()
def repo(monkeypatch) -> UserSettingsRepository:
    for k, v in {
        "AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
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


def _ack(client, row):
    with patch(FIND, new_callable=AsyncMock, return_value=row):
        return client.post("/users/me/settings/model-notices", json={"modelId": row.model_id if row else FABLE})


def test_records_the_digest_the_catalog_holds(client):
    row = _row(FABLE)
    resp = _ack(client, row)
    assert resp.status_code == 200, resp.text
    entry = resp.json()["modelNoticeAcknowledgements"][FABLE]
    assert entry["noticeDigest"] == row.data_handling_notice_digest
    assert entry["acknowledgedAt"]
    assert client.get("/users/me/settings").json()["modelNoticeAcknowledgements"][FABLE] == entry


def test_a_reworded_notice_records_a_new_digest(client):
    first = _ack(client, _row(FABLE)).json()["modelNoticeAcknowledgements"][FABLE]["noticeDigest"]
    second = _ack(client, _row(FABLE, dataHandlingNotice="New wording.")).json()
    assert second["modelNoticeAcknowledgements"][FABLE]["noticeDigest"] != first


def test_an_ordinary_settings_save_keeps_the_acknowledgements(client):
    _ack(client, _row(FABLE))
    resp = client.put("/users/me/settings", json={"memoryMode": "read_only"})
    assert resp.status_code == 200, resp.text
    assert FABLE in client.get("/users/me/settings").json()["modelNoticeAcknowledgements"]


def test_the_put_cannot_write_them(client):
    resp = client.put(
        "/users/me/settings",
        json={"modelNoticeAcknowledgements": {FABLE: {"acknowledgedAt": "2020-01-01", "noticeDigest": "x"}}},
    )
    assert resp.status_code == 200
    assert client.get("/users/me/settings").json()["modelNoticeAcknowledgements"] is None


@pytest.mark.parametrize("row", [None, _row("us.anthropic.claude-opus-5-5")])
def test_a_model_without_a_notice_is_a_404(client, row):
    assert _ack(client, row).status_code == 404
