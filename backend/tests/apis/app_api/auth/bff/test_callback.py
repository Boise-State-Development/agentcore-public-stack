"""Tests for `GET /auth/callback`."""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from apis.app_api.auth.bff import routes as bff_routes
from apis.app_api.auth.bff.cookies import OAUTH_STATE_COOKIE_NAME
from apis.app_api.auth.bff.token_exchange import ExchangeResult, TokenExchangeError
from apis.shared.sessions_bff.config import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)

from .conftest import (
    POST_LOGIN_URL,
    SEEDED_BINDING_SECRET,
    SEEDED_CODE_VERIFIER,
    SEEDED_NONCE,
    make_id_token,
)


def _seed_state(
    state: str = "valid-state",
    *,
    redirect_uri: str | None = None,
    return_to: str | None = None,
    binding_secret: str | None = SEEDED_BINDING_SECRET,
    code_verifier: str | None = SEEDED_CODE_VERIFIER,
    nonce: str | None = SEEDED_NONCE,
) -> str:
    """Push a state token through the same store the route reads from.

    Mirrors what `/auth/login` writes, including the digest of the
    browser-binding secret. Pass `binding_secret=None` to seed a row with no
    binding — that's what a pre-fix revision produced, and the callback must
    reject it.
    """
    from .conftest import CALLBACK_URL

    store = bff_routes._get_state_store()
    from apis.shared.auth.state_store import OIDCStateData

    store.store_state(
        state,
        OIDCStateData(
            redirect_uri=redirect_uri or CALLBACK_URL,
            provider_id="cognito-bff",
            return_to=return_to,
            code_verifier=code_verifier,
            nonce=nonce,
            browser_binding_hash=(
                hashlib.sha256(binding_secret.encode()).hexdigest()
                if binding_secret
                else None
            ),
        ),
        ttl_seconds=600,
    )
    return state


def _bound_client(app, *, binding_secret: str | None = SEEDED_BINDING_SECRET):
    """TestClient carrying the binding cookie for a `_seed_state()` row.

    Stands in for the browser that started the login. `binding_secret=None`
    yields a cookie-less client — the "victim" in the login-CSRF scenario.
    """
    client = TestClient(app, follow_redirects=False)
    if binding_secret is not None:
        client.cookies.set(OAUTH_STATE_COOKIE_NAME, binding_secret)
    return client


def _auth_error(response) -> str | None:
    qs = urllib.parse.urlparse(response.headers["location"]).query
    return dict(urllib.parse.parse_qsl(qs)).get("auth_error")


def _patch_token_exchange(monkeypatch, result: ExchangeResult | Exception) -> MagicMock:
    """Replace the async token-exchange helper with a mock."""
    if isinstance(result, Exception):
        mock = AsyncMock(side_effect=result)
    else:
        mock = AsyncMock(return_value=result)
    monkeypatch.setattr(bff_routes, "exchange_code_for_tokens", mock)
    return mock


def test_callback_happy_path_writes_row_and_cookies(app, monkeypatch, repository):
    state = _seed_state()
    id_token = make_id_token()
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="access.tok",
            refresh_token="refresh.tok",
            id_token=id_token,
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=auth-code-xyz&state={state}")

    assert response.status_code == 302
    assert response.headers["location"] == POST_LOGIN_URL

    # Both cookies set (TestClient surfaces them via Set-Cookie headers).
    set_cookie_blob = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in set_cookie_blob
    assert CSRF_COOKIE_NAME in set_cookie_blob
    assert "Secure" in set_cookie_blob
    assert "HttpOnly" in set_cookie_blob  # session cookie is httponly

    # And a session row got persisted under some session_id.
    scanned = repository._table.scan().get("Items", [])
    assert len(scanned) == 1
    item = scanned[0]
    assert item["user_id"] == "user-sub-001"
    assert item["username"] == "alice"
    assert item["cognito_access_token"] == "access.tok"
    assert item["cognito_refresh_token"] == "refresh.tok"


def test_callback_consumes_state_one_time(app, monkeypatch):
    state = _seed_state("once-only")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = _bound_client(app)

    first = client.get(f"/auth/callback?code=c&state={state}")
    assert first.status_code == 302
    assert "auth_error" not in first.headers["location"]

    # Replay with the same state must fail (state was deleted on first use).
    second = client.get(f"/auth/callback?code=c2&state={state}")
    assert second.status_code == 302
    parsed = urllib.parse.urlparse(second.headers["location"])
    assert dict(urllib.parse.parse_qsl(parsed.query)).get("auth_error") == "bad_state"


def test_callback_missing_code_redirects_with_error(app, monkeypatch):
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = _bound_client(app)
    response = client.get("/auth/callback?state=whatever")
    assert response.status_code == 302
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).query))
    assert qs["auth_error"] == "missing_params"


def test_callback_oauth_error_param_redirects_with_error(app):
    client = _bound_client(app)
    response = client.get(
        "/auth/callback?error=access_denied&error_description=user+cancelled"
    )
    assert response.status_code == 302
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).query))
    assert qs["auth_error"] == "oauth_error"
    # And cookies should be cleared on a failure path so a stale cookie
    # from a partial prior session is dropped.
    set_cookie_blob = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in set_cookie_blob


def test_callback_token_exchange_failure_redirects_with_error(app, monkeypatch):
    state = _seed_state("ex-fail")
    _patch_token_exchange(monkeypatch, TokenExchangeError("boom"))

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).query))
    assert qs["auth_error"] == "exchange_failed"


def test_callback_missing_id_token_redirects_with_error(app, monkeypatch):
    state = _seed_state("no-id")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a",
            refresh_token="r",
            id_token=None,
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")
    assert response.status_code == 302
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).query))
    assert qs["auth_error"] == "no_id_token"


def test_callback_session_id_is_unique_across_logins(app, monkeypatch, repository):
    """Two callback successes write two distinct session_ids."""
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = _bound_client(app)

    s1 = _seed_state("first")
    s2 = _seed_state("second")
    client.get(f"/auth/callback?code=c1&state={s1}")
    client.get(f"/auth/callback?code=c2&state={s2}")

    items = repository._table.scan().get("Items", [])
    assert len({item["session_id"] for item in items}) == 2


# ─── return_to deep-link round-trip (Phase 7) ─────────────────────────


def test_callback_redirects_to_return_to_path_when_set(app, monkeypatch):
    """Successful callback honours the same-origin path the SPA stashed
    at /auth/login, grafted onto the SPA origin from
    BFF_POST_LOGIN_REDIRECT_URL so cross-origin dev (BFF on :8000, SPA on
    :4200) lands on the SPA host instead of the BFF host."""
    state = _seed_state("with-return-to", return_to="/files/abc?tab=details")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    # POST_LOGIN_URL = "http://localhost:4200/" — origin spliced onto path.
    assert (
        response.headers["location"]
        == "http://localhost:4200/files/abc?tab=details"
    )


def test_callback_falls_back_to_post_login_when_no_return_to(app, monkeypatch):
    state = _seed_state("no-return-to")  # return_to omitted → None
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    assert response.headers["location"] == POST_LOGIN_URL


# ─── Users-table upsert from ID-token claims (Phase 7 follow-up) ──────


def _patch_user_sync(monkeypatch) -> AsyncMock:
    """Replace the lazy `_get_user_sync_service` with a stub that captures
    the kwargs the callback passes to `sync_from_user`.

    The real service skips when the Users table env var isn't configured;
    we stub it so the test can assert the BFF callback actually calls
    sync with the email/name/roles parsed from the ID token — that's the
    fix for the "first-login user gets email=None and Cognito provider
    group instead of IdP roles" regression."""
    sync_mock = MagicMock()
    sync_mock.enabled = True
    sync_mock.sync_from_user = AsyncMock(return_value=(None, True))
    monkeypatch.setattr(bff_routes, "_get_user_sync_service", lambda: sync_mock)
    return sync_mock.sync_from_user


def test_callback_upserts_user_with_id_token_claims(app, monkeypatch):
    """The Users row must be seeded from the *ID token* — the access token
    has no email/name/picture and only carries Cognito's internal provider
    group in `cognito:groups`, never the IdP-mapped role list."""
    state = _seed_state("sync-claims")
    id_token = make_id_token(
        sub="user-sub-001",
        username="alice",
        email="Alice@Example.com",
        name="Alice Example",
        picture="https://example.com/a.png",
        custom_roles='["Admin","Editor"]',
    )
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=id_token,
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    sync_call = _patch_user_sync(monkeypatch)

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    sync_call.assert_awaited_once()
    kwargs = sync_call.await_args.kwargs
    assert kwargs["user_id"] == "user-sub-001"
    # email is normalized to lowercase by `decode_id_token_claims`
    assert kwargs["email"] == "alice@example.com"
    assert kwargs["name"] == "Alice Example"
    assert kwargs["picture"] == "https://example.com/a.png"
    # `custom:roles` is preferred over `cognito:groups`; JSON-array form
    # parses out cleanly.
    assert kwargs["roles"] == ["Admin", "Editor"]


def test_callback_falls_back_to_cognito_groups_when_custom_roles_absent(
    app, monkeypatch
):
    """No `custom:roles` claim → use `cognito:groups`. This matches the
    access-token validator's behavior so RBAC is consistent between the
    Bearer (legacy) and cookie (BFF) paths."""
    state = _seed_state("sync-groups")
    id_token = make_id_token(
        custom_roles=None,
        cognito_groups=["Admin", "Beta"],
    )
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=id_token,
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    sync_call = _patch_user_sync(monkeypatch)

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    assert sync_call.await_args.kwargs["roles"] == ["Admin", "Beta"]


def test_callback_user_sync_failure_does_not_break_login(app, monkeypatch):
    """A DDB hiccup on the Users-table upsert must not prevent the user
    from logging in — they get a valid session, the Users row just lags."""
    state = _seed_state("sync-failure")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    failing_sync = MagicMock()
    failing_sync.enabled = True
    failing_sync.sync_from_user = AsyncMock(side_effect=RuntimeError("ddb down"))
    monkeypatch.setattr(bff_routes, "_get_user_sync_service", lambda: failing_sync)

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    assert response.headers["location"] == POST_LOGIN_URL


# ─── Browser binding: OIDC login CSRF / session fixation (f-8c4f312a) ──
#
# `state` is a public value — it rides in a 302 Location and anyone can mint
# one anonymously. These tests pin the property that makes it insufficient on
# its own: a callback is honoured only when it also presents the binding
# cookie whose digest was recorded with that state.


def test_callback_rejects_valid_state_from_a_different_browser(
    app, monkeypatch, repository
):
    """The core of the finding: a state minted for browser A must not be
    redeemable by browser B.

    Browser B here is cookie-less, exactly like the reported victim client —
    it never visited /auth/login. Before the fix this returned a live session
    for whatever identity the code named."""
    state = _seed_state("minted-for-browser-a")
    exchange = _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    victim = _bound_client(app, binding_secret=None)
    response = victim.get(f"/auth/callback?code=attacker-code&state={state}")

    assert response.status_code == 302
    assert _auth_error(response) == "missing_state_cookie"
    # No session cookie was issued...
    set_cookie_blob = " ".join(response.headers.get_list("set-cookie"))
    assert f"{SESSION_COOKIE_NAME}=;" in set_cookie_blob.replace('""', "")
    # ...no session row was persisted...
    assert repository._table.scan().get("Items", []) == []
    # ...and the code never reached the token endpoint, so it stays unspent.
    exchange.assert_not_awaited()


def test_callback_rejects_state_when_binding_cookie_belongs_to_another_flow(
    app, monkeypatch, repository
):
    """A browser that has *a* binding cookie, just not the one this state was
    minted with, is still refused. Covers the attacker who lures a victim that
    has their own login in flight."""
    state = _seed_state("bound-to-someone-else")
    exchange = _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    client = _bound_client(app, binding_secret="some-other-browsers-secret")
    response = client.get(f"/auth/callback?code=attacker-code&state={state}")

    assert response.status_code == 302
    assert _auth_error(response) == "state_not_bound"
    assert repository._table.scan().get("Items", []) == []
    exchange.assert_not_awaited()


def test_callback_rejects_state_row_with_no_binding_digest(
    app, monkeypatch, repository
):
    """Fail closed on a state row minted before browser binding existed.

    Such rows only appear mid-deploy. Honouring them would keep the hole open
    for the whole rollout window — precisely when an attacker holding a
    pre-minted state would strike."""
    state = _seed_state("legacy-unbound-row", binding_secret=None)
    exchange = _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    assert _auth_error(response) == "state_not_bound"
    assert repository._table.scan().get("Items", []) == []
    exchange.assert_not_awaited()


def test_callback_binding_is_checked_before_the_state_is_consumed(app, monkeypatch):
    """A cookie-less request must not burn the state row.

    Otherwise anyone could DoS a user's in-flight login by replaying the state
    from a crafted link before the user finishes at the IdP."""
    state = _seed_state("must-survive-probe")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    attacker = _bound_client(app, binding_secret=None)
    assert (
        _auth_error(attacker.get(f"/auth/callback?code=c&state={state}"))
        == "missing_state_cookie"
    )

    # The legitimate browser can still complete the same flow.
    victim = _bound_client(app)
    response = victim.get(f"/auth/callback?code=c&state={state}")
    assert _auth_error(response) is None
    assert response.headers["location"] == POST_LOGIN_URL


def test_callback_clears_binding_cookie_on_success(app, monkeypatch):
    """Binding is single-use: once its state row is consumed the cookie is
    dropped so it can't be paired with a later state."""
    state = _seed_state("clears-on-success")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert _auth_error(response) is None
    expiry = [
        h
        for h in response.headers.get_list("set-cookie")
        if h.startswith(OAUTH_STATE_COOKIE_NAME)
    ]
    assert expiry, f"{OAUTH_STATE_COOKIE_NAME} was not cleared"
    assert "Max-Age=0" in expiry[0] or 'expires=Thu, 01 Jan 1970' in expiry[0].lower()


@pytest.mark.parametrize(
    ("query", "expected_error"),
    [
        ("?code=c&state=never-minted", "bad_state"),
        ("?error=access_denied", "oauth_error"),
        ("?state=only-state", "missing_params"),
    ],
)
def test_callback_clears_binding_cookie_on_failure_paths(
    app, query, expected_error
):
    """Every terminal path drops the binding so a failed attempt can't leave a
    reusable one behind."""
    client = _bound_client(app)
    response = client.get(f"/auth/callback{query}")

    assert _auth_error(response) == expected_error
    assert any(
        h.startswith(OAUTH_STATE_COOKIE_NAME)
        for h in response.headers.get_list("set-cookie")
    )


# ─── PKCE ──────────────────────────────────────────────────────────────


def test_callback_sends_stored_code_verifier_to_token_endpoint(app, monkeypatch):
    """The verifier never touches the browser — it comes off the state row, so
    a code lifted from the redirect can't be redeemed without it."""
    state = _seed_state("pkce-state")
    exchange = _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a", refresh_token="r", id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert _auth_error(response) is None
    assert exchange.await_args.kwargs["code_verifier"] == SEEDED_CODE_VERIFIER


# ─── OIDC nonce ────────────────────────────────────────────────────────


def test_callback_rejects_id_token_with_mismatched_nonce(
    app, monkeypatch, repository
):
    """An ID token minted for a different authorization request can't be
    substituted into this one."""
    state = _seed_state("nonce-state")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a",
            refresh_token="r",
            id_token=make_id_token(nonce="nonce-from-another-flow"),
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert response.status_code == 302
    assert _auth_error(response) == "bad_nonce"
    assert repository._table.scan().get("Items", []) == []


def test_callback_rejects_id_token_with_no_nonce_when_one_was_requested(
    app, monkeypatch, repository
):
    """A missing claim is a mismatch — otherwise stripping the claim would be
    a trivial bypass."""
    state = _seed_state("nonce-absent")
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a",
            refresh_token="r",
            id_token=make_id_token(nonce=None),
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert _auth_error(response) == "bad_nonce"
    assert repository._table.scan().get("Items", []) == []


def test_callback_accepts_missing_nonce_when_state_predates_the_check(
    app, monkeypatch
):
    """A state row with no stored nonce doesn't fail the nonce check — binding
    already gates those rows, and attributing the failure twice just muddies
    the diagnostics."""
    state = _seed_state("no-stored-nonce", nonce=None)
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="a",
            refresh_token="r",
            id_token=make_id_token(nonce=None),
            access_token_exp=int(time.time()) + 3600,
        ),
    )

    client = _bound_client(app)
    response = client.get(f"/auth/callback?code=c&state={state}")

    assert _auth_error(response) is None
