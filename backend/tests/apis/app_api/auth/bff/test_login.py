"""Tests for `GET /auth/login`."""

from __future__ import annotations

import urllib.parse

import pytest
from fastapi.testclient import TestClient

from .conftest import (
    BFF_CLIENT_ID,
    CALLBACK_URL,
    COGNITO_DOMAIN_URL,
)


def test_login_redirects_to_cognito_authorize(app_for_login):
    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get("/auth/login")

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{COGNITO_DOMAIN_URL}/oauth2/authorize?")

    parsed = urllib.parse.urlparse(location)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    assert params["response_type"] == "code"
    assert params["client_id"] == BFF_CLIENT_ID
    assert params["scope"] == "openid email profile"
    assert params["redirect_uri"] == CALLBACK_URL
    assert params["state"]  # non-empty


def test_login_persists_state_in_store(app_for_login):
    """The state token written here is what /auth/callback validates."""
    from apis.app_api.auth.bff import routes as bff_routes

    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get("/auth/login")
    state = dict(
        urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).query)
    )["state"]

    # The lazy state store was instantiated by the request — pull the same
    # instance and confirm we can retrieve the state.
    store = bff_routes._get_state_store()
    ok, data = store.get_and_delete_state(state)
    assert ok is True
    assert data is not None
    assert data.redirect_uri == CALLBACK_URL


def test_login_503_when_config_unready(monkeypatch):
    """No env vars → /auth/login surfaces 503 instead of crashing."""
    # Wipe everything BFFAuthConfig depends on.
    for var in (
        "BFF_SESSIONS_TABLE_NAME",
        "BFF_COOKIE_SIGNING_KEY_ARN",
        "COGNITO_BFF_APP_CLIENT_ID",
        "COGNITO_BFF_APP_CLIENT_SECRET_ARN",
        "COGNITO_DOMAIN_URL",
        "BFF_AUTH_CALLBACK_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    from fastapi import FastAPI

    from apis.app_api.auth.bff.routes import router as bff_router

    fastapi_app = FastAPI()
    fastapi_app.include_router(bff_router)

    client = TestClient(fastapi_app, follow_redirects=False)
    response = client.get("/auth/login")
    assert response.status_code == 503


def test_login_states_are_unique_per_request(app_for_login):
    """Two consecutive logins produce different states (no caching slip-up)."""
    client = TestClient(app_for_login, follow_redirects=False)
    s1 = dict(
        urllib.parse.parse_qsl(
            urllib.parse.urlparse(client.get("/auth/login").headers["location"]).query
        )
    )["state"]
    s2 = dict(
        urllib.parse.parse_qsl(
            urllib.parse.urlparse(client.get("/auth/login").headers["location"]).query
        )
    )["state"]
    assert s1 != s2
