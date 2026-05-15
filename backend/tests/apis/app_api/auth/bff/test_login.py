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


# ─── identity_provider passthrough (Phase 6c) ──────────────────────────


def _authorize_params(response) -> dict[str, str]:
    return dict(
        urllib.parse.parse_qsl(
            urllib.parse.urlparse(response.headers["location"]).query
        )
    )


def test_login_forwards_provider_to_cognito_as_identity_provider(app_for_login):
    """Phase 6c: SPA's federated-login buttons pass `?provider=<idp>` so
    Cognito skips the Hosted UI chooser and lands on the right IdP."""
    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get("/auth/login?provider=GoogleSSO")

    assert response.status_code == 302
    params = _authorize_params(response)
    assert params["identity_provider"] == "GoogleSSO"
    # Other authorize params still present.
    assert params["response_type"] == "code"
    assert params["state"]


def test_login_omits_identity_provider_when_provider_param_absent(app_for_login):
    """No `?provider=` → Cognito Hosted UI shows its provider chooser."""
    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get("/auth/login")

    params = _authorize_params(response)
    assert "identity_provider" not in params


@pytest.mark.parametrize(
    "bad_provider",
    [
        "Google\r\nSet-Cookie: x=y",  # CRLF injection — would split the URL
        "evil%20<script>",            # angle brackets / spaces
        "google&extra=injected",      # `&` would split out a forged param
        "x" * 200,                    # over the length cap
        "",                           # empty — distinct from "absent"
    ],
)
def test_login_silently_drops_malformed_provider(app_for_login, bad_provider):
    """Reject silently rather than 4xx — an old SPA bundle that sends an
    invalid provider should still complete login through the chooser
    instead of dead-ending on a 400."""
    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get(
        "/auth/login", params={"provider": bad_provider}
    )

    assert response.status_code == 302
    params = _authorize_params(response)
    assert "identity_provider" not in params


# ─── return_to deep-link plumbing (Phase 7) ────────────────────────────


def test_login_stores_same_origin_return_to_in_state(app_for_login):
    """Allowlisted same-origin path makes it onto the OIDCStateData so
    the callback can redirect there post-cookie-set."""
    from apis.app_api.auth.bff import routes as bff_routes

    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get(
        "/auth/login", params={"return_to": "/files/abc?tab=details"}
    )
    state = _authorize_params(response)["state"]

    store = bff_routes._get_state_store()
    ok, data = store.get_and_delete_state(state)
    assert ok is True
    assert data is not None
    assert data.return_to == "/files/abc?tab=details"


@pytest.mark.parametrize(
    "bad_return_to",
    [
        "//evil.com/x",            # protocol-relative — different host
        "https://evil.com/x",      # absolute URL — different origin
        "http://evil.com/x",
        "/\\evil.com/x",           # back-slash bypass past the // check
        "no-leading-slash",        # not a path
        "",                        # empty
        "/x" + "y" * 3000,         # length cap
        "/multi\nline",            # CRLF injection into Location
        "/multi\rline",
        # WHATWG URL parsers strip TAB/CR/LF from URL inputs *before*
        # parsing — `/\t/evil.com` would resolve as `//evil.com` and
        # bypass the protocol-relative check when the post-login URL
        # is a relative path. Rejecting all C0 controls slams the door
        # on the same trick via any other quirky control byte.
        "/\t/evil.com",
        "/\x00/evil.com",
        "/\x0b/evil.com",
    ],
)
def test_login_rejects_unsafe_return_to(app_for_login, bad_return_to):
    """Anything that fails the same-origin allowlist drops silently —
    the state row's `return_to` stays None and the callback uses the
    configured post-login URL."""
    from apis.app_api.auth.bff import routes as bff_routes

    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get(
        "/auth/login", params={"return_to": bad_return_to}
    )

    assert response.status_code == 302
    state = _authorize_params(response)["state"]
    ok, data = bff_routes._get_state_store().get_and_delete_state(state)
    assert ok is True
    assert data is not None
    assert data.return_to is None
