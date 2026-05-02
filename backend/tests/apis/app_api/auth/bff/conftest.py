"""Shared fixtures for the BFF auth route tests.

The four routes share most setup: env vars wired so `BFFAuthConfig.from_env()`
reports ready, a moto-backed sessions table, an in-memory state store, and
a `CookieCodec` with a pre-injected AES key (real KMS would require a
network mock per test).
"""

from __future__ import annotations

import secrets
from typing import Optional

import boto3
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI
from moto import mock_aws

from apis.app_api.auth.bff import routes as bff_routes
from apis.app_api.auth.bff.routes import router as bff_router
from apis.shared.sessions_bff.cache import _reset_default_cache_for_tests
from apis.shared.sessions_bff.cookie import CookieCodec
from apis.shared.sessions_bff.refresh import _reset_secret_cache_for_tests
from apis.shared.sessions_bff.repository import SessionRepository

BFF_SESSIONS_TABLE = "test-bff-sessions"
COGNITO_DOMAIN_URL = "https://test-prefix.auth.us-east-1.amazoncognito.com"
CALLBACK_URL = "http://localhost:8000/auth/callback"
POST_LOGIN_URL = "http://localhost:4200/"
BFF_CLIENT_ID = "test-bff-client-id"
BFF_CLIENT_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:0:secret:bff-test"
COOKIE_KMS_ARN = "arn:aws:kms:us-east-1:0:key/test"


def _make_codec() -> CookieCodec:
    """Real CookieCodec with a deterministic in-memory AES key — no KMS."""
    codec = CookieCodec(kms_key_arn=COOKIE_KMS_ARN)
    codec._cipher = AESGCM(secrets.token_bytes(32))
    return codec


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Drop process-wide singletons between tests so fixture order can't
    leak a state-store, repository, or codec from a prior case."""
    bff_routes._reset_for_tests()
    _reset_default_cache_for_tests()
    _reset_secret_cache_for_tests()
    yield
    bff_routes._reset_for_tests()
    _reset_default_cache_for_tests()
    _reset_secret_cache_for_tests()


@pytest.fixture
def bff_env(monkeypatch):
    """Wire all the env vars `BFFAuthConfig.from_env()` and `BFFConfig` need.

    Also unsets env vars from a developer's local `.env` (loaded by other
    tests' conftests) that would otherwise pull `create_state_store()`
    onto the real DynamoDB code path mid-test.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("BFF_SESSIONS_TABLE_NAME", BFF_SESSIONS_TABLE)
    monkeypatch.setenv("BFF_COOKIE_SIGNING_KEY_ARN", COOKIE_KMS_ARN)
    monkeypatch.setenv("BFF_SESSION_TTL_SECONDS", "28800")
    monkeypatch.setenv("BFF_SESSION_REFRESH_LEEWAY_SECONDS", "60")
    monkeypatch.setenv("COGNITO_BFF_APP_CLIENT_ID", BFF_CLIENT_ID)
    monkeypatch.setenv("COGNITO_BFF_APP_CLIENT_SECRET_ARN", BFF_CLIENT_SECRET_ARN)
    monkeypatch.setenv("COGNITO_DOMAIN_URL", COGNITO_DOMAIN_URL)
    monkeypatch.setenv("BFF_AUTH_CALLBACK_URL", CALLBACK_URL)
    monkeypatch.setenv("BFF_POST_LOGIN_REDIRECT_URL", POST_LOGIN_URL)
    # Force the in-memory state store regardless of the developer's .env.
    monkeypatch.delenv("DYNAMODB_OIDC_STATE_TABLE_NAME", raising=False)
    yield


def _create_bff_sessions_table(dynamodb) -> None:
    dynamodb.create_table(
        TableName=BFF_SESSIONS_TABLE,
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


@pytest.fixture
def moto_aws(bff_env):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_bff_sessions_table(dynamodb)
        yield dynamodb


@pytest.fixture
def repository(moto_aws) -> SessionRepository:
    return SessionRepository(table_name=BFF_SESSIONS_TABLE)


@pytest.fixture
def codec() -> CookieCodec:
    return _make_codec()


@pytest.fixture
def app(monkeypatch, moto_aws, codec, repository) -> FastAPI:
    """A minimal FastAPI app with the BFF router mounted and singletons
    pre-injected so requests don't try to talk to real AWS."""
    bff_routes._repository = repository
    bff_routes._cookie_codec = codec
    # Default secret resolver short-circuit so we don't need a Secrets
    # Manager mock per test that doesn't care about it.
    monkeypatch.setattr(
        bff_routes,
        "resolve_bff_client_secret",
        lambda **_: "test-client-secret",
    )

    fastapi_app = FastAPI()
    fastapi_app.include_router(bff_router)
    return fastapi_app


@pytest.fixture
def app_for_login(monkeypatch, bff_env) -> FastAPI:
    """Lighter app fixture for /auth/login, which doesn't touch DDB or KMS."""
    fastapi_app = FastAPI()
    fastapi_app.include_router(bff_router)
    return fastapi_app


def make_id_token(
    *,
    sub: str = "user-sub-001",
    username: str = "alice",
    email: Optional[str] = "alice@example.com",
    name: Optional[str] = "Alice Example",
    picture: Optional[str] = None,
) -> str:
    """Unsigned JWT good enough for `decode_id_token_claims`."""
    import json
    import base64

    def _b64(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = _b64({"alg": "none", "typ": "JWT"})
    claims: dict = {"sub": sub, "cognito:username": username}
    if email:
        claims["email"] = email
    if name:
        claims["name"] = name
    if picture:
        claims["picture"] = picture
    body = _b64(claims)
    return f"{header}.{body}."
