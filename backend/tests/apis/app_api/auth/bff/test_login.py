"""Tests for `GET /auth/login`."""

from __future__ import annotations

import base64
import hashlib
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from apis.app_api.auth.bff import routes as bff_routes
from apis.app_api.auth.bff.cookies import OAUTH_STATE_COOKIE_NAME

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


# ─── Browser binding, PKCE, nonce (f-8c4f312a) ─────────────────────────


def _set_cookie_header(response, name: str) -> str:
    matches = [
        h for h in response.headers.get_list("set-cookie") if h.startswith(name)
    ]
    assert matches, f"{name} was not set. Set-Cookie: {response.headers.get_list('set-cookie')}"
    return matches[0]


def _binding_secret_from(response) -> str:
    header = _set_cookie_header(response, OAUTH_STATE_COOKIE_NAME)
    return header.split(";", 1)[0].split("=", 1)[1]


def test_login_sets_browser_binding_cookie(app_for_login):
    """The 302 must carry the binding cookie. The reported finding was that
    /auth/login emitted no Set-Cookie at all, leaving `state` — a value that
    travels in a redirect URL — as the only thing the callback checked."""
    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get("/auth/login")

    header = _set_cookie_header(response, OAUTH_STATE_COOKIE_NAME)
    assert "HttpOnly" in header  # script must not be able to forge a binding
    assert "Secure" in header  # required by the __Host- prefix
    assert "Path=/" in header
    assert "Domain=" not in header  # __Host- forbids it; sibling hosts can't plant one
    # `lax`, not `strict`: the IdP returns the user via a top-level cross-site
    # GET, and `strict` would withhold the cookie and break every login.
    assert "SameSite=lax" in header
    assert f"Max-Age={bff_routes._STATE_TTL_SECONDS}" in header


def test_login_stores_only_the_digest_of_the_binding_secret(app_for_login):
    """A read-only leak of the state table must not yield a replayable
    binding, so the row holds SHA-256(secret) and never the secret."""
    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get("/auth/login")

    secret = _binding_secret_from(response)
    state = _authorize_params(response)["state"]
    ok, data = bff_routes._get_state_store().get_and_delete_state(state)

    assert ok is True
    assert data.browser_binding_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in (data.browser_binding_hash or "")


def test_login_binding_secret_is_unique_per_request(app_for_login):
    """Two logins must not share a binding, or one user's state would be
    redeemable using another's cookie."""
    client = TestClient(app_for_login, follow_redirects=False)
    first = _binding_secret_from(client.get("/auth/login"))
    second = _binding_secret_from(client.get("/auth/login"))
    assert first != second


def test_login_binding_secret_is_not_leaked_in_the_redirect_url(app_for_login):
    """The secret's whole value is that it lives only in the cookie jar. If it
    also appeared in the authorize URL, anyone who could read the URL (Referer,
    proxy log, browser history) could rebuild the binding."""
    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get("/auth/login")

    assert _binding_secret_from(response) not in response.headers["location"]


def test_login_sends_pkce_s256_challenge_derived_from_stored_verifier(app_for_login):
    """Cognito supports S256 only. The verifier stays in the state row; only
    its challenge goes on the wire."""
    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get("/auth/login")
    params = _authorize_params(response)

    assert params["code_challenge_method"] == "S256"

    ok, data = bff_routes._get_state_store().get_and_delete_state(params["state"])
    assert ok is True
    assert data.code_verifier
    # RFC 7636 §4.1: 43..128 chars.
    assert 43 <= len(data.code_verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(data.code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert params["code_challenge"] == expected
    # Unpadded base64url — Cognito rejects the `=`-suffixed form.
    assert "=" not in params["code_challenge"]
    # The verifier itself must never reach the browser.
    assert data.code_verifier not in response.headers["location"]


def test_login_sends_nonce_matching_the_stored_state(app_for_login):
    """Cognito echoes `nonce` into the ID token; the callback compares it, so
    the two must agree."""
    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get("/auth/login")
    params = _authorize_params(response)

    ok, data = bff_routes._get_state_store().get_and_delete_state(params["state"])
    assert ok is True
    assert data.nonce
    assert params["nonce"] == data.nonce


def test_login_secrets_are_independent_of_each_other(app_for_login):
    """state, verifier, nonce and binding must be four separate draws —
    deriving any from another would let a leak of the public `state` or
    `nonce` reconstruct a server-side secret."""
    client = TestClient(app_for_login, follow_redirects=False)
    response = client.get("/auth/login")
    params = _authorize_params(response)
    secret = _binding_secret_from(response)

    ok, data = bff_routes._get_state_store().get_and_delete_state(params["state"])
    assert ok is True
    values = {params["state"], data.code_verifier, data.nonce, secret}
    assert len(values) == 4
