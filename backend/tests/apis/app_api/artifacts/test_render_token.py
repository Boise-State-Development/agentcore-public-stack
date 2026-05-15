"""Tests for the app-api render-token minter.

The headline test (`test_token_verifies_against_render_lambda`) mints a
token and feeds it straight through #309's Lambda verifier with a shared
signing key — that is the real cross-PR contract guarantee.
"""

from __future__ import annotations

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws
import boto3

from apis.app_api.artifacts import service as token_service
from apis.app_api.artifacts.routes import router as artifacts_router
from apis.shared.auth import User, get_current_user_from_session
from lambdas.artifact_render import handler as render_lambda

KEY = "test-render-key-44-chars-of-entropy-aaaaaaaa"
SECRET_NAME = "test-artifact-render-token-key"
TABLE = "test-user-artifacts"
ORIGIN = "https://artifacts.test.example.com"
REGION = "us-east-1"
USER_ID = "user-123"


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    token_service._reset_caches_for_tests()
    # The verifier caches its own signing key separately.
    monkeypatch.setattr(render_lambda, "_cached_signing_key", None)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        monkeypatch.setenv("AWS_REGION", REGION)
        sm = boto3.client("secretsmanager", region_name=REGION)
        arn = sm.create_secret(Name=SECRET_NAME, SecretString=KEY)["ARN"]

        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        monkeypatch.setenv("ARTIFACTS_RENDER_TOKEN_SECRET_ARN", arn)
        monkeypatch.setenv("DYNAMODB_ARTIFACTS_TABLE_NAME", TABLE)
        monkeypatch.setenv("ARTIFACTS_ORIGIN", ORIGIN)

        app = FastAPI()
        app.include_router(artifacts_router)
        app.dependency_overrides[get_current_user_from_session] = (
            lambda: User(email="u@x.com", user_id=USER_ID, name="U", roles=[])
        )
        yield TestClient(app), boto3.resource("dynamodb", region_name=REGION)


def _put_version(ddb, *, user_id: str = USER_ID, artifact="art-1", version=1) -> None:
    ddb.Table(TABLE).put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"ARTIFACT#{artifact}#V#{version:05d}",
            "storage": "s3",
            "content_key": f"{user_id}/{artifact}/v{version}/index.html",
            "content_type": "text/html; charset=utf-8",
        }
    )


def _token_from_url(url: str) -> str:
    assert url.startswith(f"{ORIGIN}/?t=")
    return url.split("?t=", 1)[1]


def test_happy_path_mints_valid_token(client) -> None:
    tc, ddb = client
    _put_version(ddb)
    resp = tc.post(
        "/artifacts/art-1/render-token", json={"version": 1, "sessionId": "sess-9"}
    )
    assert resp.status_code == 200
    body = resp.json()
    claims = jwt.decode(
        _token_from_url(body["url"]),
        KEY,
        algorithms=["HS256"],
        audience="artifact-render",
    )
    assert claims["iss"] == "app-api"
    assert claims["sub"] == USER_ID
    assert claims["aid"] == "art-1"
    assert claims["ver"] == 1
    assert claims["sid"] == "sess-9"
    assert claims["exp"] - claims["iat"] == 120
    assert body["expires_at"].endswith("+00:00")


def test_token_verifies_against_render_lambda(client, monkeypatch) -> None:
    """The cross-PR contract: a freshly minted token must pass the
    actual #309 verifier byte-for-byte with the same signing key."""
    tc, ddb = client
    _put_version(ddb)
    monkeypatch.setattr(render_lambda, "_cached_signing_key", KEY)

    resp = tc.post("/artifacts/art-1/render-token", json={"version": 1})
    token = _token_from_url(resp.json()["url"])

    verified = render_lambda._verify_token(token)
    assert verified["sub"] == USER_ID
    assert verified["aid"] == "art-1"
    assert verified["ver"] == 1


def test_unknown_version_is_404(client) -> None:
    tc, _ = client
    resp = tc.post("/artifacts/art-1/render-token", json={"version": 1})
    assert resp.status_code == 404


def test_other_users_artifact_is_404(client) -> None:
    """Ownership scoping: a record owned by someone else is invisible
    because the PK is built from the authenticated user's id."""
    tc, ddb = client
    _put_version(ddb, user_id="someone-else")
    resp = tc.post("/artifacts/art-1/render-token", json={"version": 1})
    assert resp.status_code == 404


def test_version_must_be_positive(client) -> None:
    tc, ddb = client
    _put_version(ddb)
    resp = tc.post("/artifacts/art-1/render-token", json={"version": 0})
    assert resp.status_code == 422


def test_session_id_optional(client) -> None:
    tc, ddb = client
    _put_version(ddb)
    resp = tc.post("/artifacts/art-1/render-token", json={"version": 1})
    assert resp.status_code == 200
    claims = jwt.decode(
        _token_from_url(resp.json()["url"]),
        KEY,
        algorithms=["HS256"],
        audience="artifact-render",
    )
    assert claims["sid"] == ""


def test_missing_origin_is_500(client, monkeypatch) -> None:
    """Fail-closed config: with ARTIFACTS_ORIGIN unset the service must
    500 before minting — never hand back a usable token embedded in a
    relative, unloadable URL. The artifact row exists, so a 500 (not a
    404) proves the origin check fires first."""
    tc, ddb = client
    _put_version(ddb)
    monkeypatch.delenv("ARTIFACTS_ORIGIN", raising=False)
    resp = tc.post("/artifacts/art-1/render-token", json={"version": 1})
    assert resp.status_code == 500


def test_requires_authentication() -> None:
    """No dependency override and no session cookie → the route is
    blocked by the session dependency, never reaching mint logic."""
    app = FastAPI()
    app.include_router(artifacts_router)
    resp = TestClient(app).post(
        "/artifacts/art-1/render-token", json={"version": 1}
    )
    assert resp.status_code == 401
