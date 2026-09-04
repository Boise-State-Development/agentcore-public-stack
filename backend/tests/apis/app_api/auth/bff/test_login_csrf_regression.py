"""End-to-end regression for the BFF OIDC login CSRF / session fixation
finding (f-8c4f312a, High, Authentication Bypass).

Unlike `test_callback.py`, nothing here seeds the state store by hand — the
attacker mints state through the real `GET /auth/login`, exactly as reported.
That's what makes these tests a faithful guard: they exercise the same code
path an attacker would, so they'd fail if a future change reintroduced an
unbound state anywhere in the login round-trip.

The reported chain was:

  1. Attacker anonymously hits /auth/login and captures `state` from the 302
     Location. The response carried no Set-Cookie, so nothing tied that state
     to the attacker's browser.
  2. Attacker authenticates at the IdP themselves to obtain a real
     authorization code, without completing the callback.
  3. Victim is lured to /auth/callback?code=<attacker code>&state=<captured
     state>. The BFF exchanged the code and issued the victim a sealed session
     for the *attacker's* identity — including, in the reported case, a
     `system_admin` account.

Everything the victim then did (conversations, uploads, memory entries, and
any third-party OAuth connector consent) happened inside an account the
attacker still holds the credentials to.
"""

from __future__ import annotations

import time
import urllib.parse
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from apis.app_api.auth.bff import routes as bff_routes
from apis.app_api.auth.bff.cookies import OAUTH_STATE_COOKIE_NAME
from apis.app_api.auth.bff.token_exchange import ExchangeResult
from apis.shared.sessions_bff.config import SESSION_COOKIE_NAME

from .conftest import make_id_token

# Stands in for the attacker's own IdP account — the identity the victim's
# browser was being handed in the report.
ATTACKER_SUB = "attacker-sub-999"
ATTACKER_CODE = "attacker-authorization-code"


def _authorize_query(response) -> dict[str, str]:
    location = response.headers["location"]
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query))


def _issued_binding_cookie(response) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(OAUTH_STATE_COOKIE_NAME):
            value = header.split(";", 1)[0].split("=", 1)[1]
            return value or None
    return None


def _patch_exchange_to_return_attacker_identity(monkeypatch) -> AsyncMock:
    """Simulate a token endpoint that would happily redeem the attacker's code.

    The point is that the exchange *would* succeed — the fix must stop the
    request before it ever gets here, not rely on the exchange failing.
    """
    mock = AsyncMock(
        return_value=ExchangeResult(
            access_token="attacker.access.token",
            refresh_token="attacker.refresh.token",
            id_token=make_id_token(
                sub=ATTACKER_SUB,
                username="attacker",
                email="attacker@example.com",
                name="Attacker",
                custom_roles='["system_admin"]',
                # The IdP echoes the nonce from the authorize request the
                # attacker drove, so the nonce check alone would not catch
                # this — browser binding is what does.
                nonce=None,
            ),
            access_token_exp=int(time.time()) + 3600,
        )
    )
    monkeypatch.setattr(bff_routes, "exchange_code_for_tokens", mock)
    return mock


def _assert_no_session_issued(response, repository) -> None:
    assert response.status_code == 302
    # No sealed session cookie with a value — only the clearing directive.
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(SESSION_COOKIE_NAME):
            value = header.split(";", 1)[0].split("=", 1)[1].strip('"')
            assert value == "", f"a session cookie was issued: {header}"
    # And no server-side session record was persisted.
    assert repository._table.scan().get("Items", []) == []


def test_full_login_csrf_chain_is_refused(app, monkeypatch, repository):
    """Step 1-3 of the reported chain, end to end."""
    exchange = _patch_exchange_to_return_attacker_identity(monkeypatch)

    # (1) Attacker mints state anonymously. They receive a binding cookie now,
    #     but it lands in *their* cookie jar — which is the whole point.
    attacker = TestClient(app, follow_redirects=False)
    login = attacker.get("/auth/login")
    assert login.status_code == 302
    captured_state = _authorize_query(login)["state"]
    assert captured_state

    # (2) Attacker authenticates at the IdP off-band and holds a real code.
    #     Nothing to simulate on our side.

    # (3) Victim follows the crafted link. Cookie-less, as in the report:
    #     a browser that never visited /auth/login.
    victim = TestClient(app, follow_redirects=False)
    response = victim.get(
        f"/auth/callback?code={ATTACKER_CODE}&state={captured_state}"
    )

    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(response.headers["location"]).query))
    assert qs["auth_error"] == "missing_state_cookie"
    _assert_no_session_issued(response, repository)
    # The attacker's code was never redeemed, so no third party learns whether
    # it was valid.
    exchange.assert_not_awaited()


def test_login_csrf_chain_refused_even_with_cross_site_navigation_headers(
    app, monkeypatch, repository
):
    """The report noted the callback accepted `Sec-Fetch-Site: cross-site` and
    a foreign `Referer`. It still must — a legitimate IdP redirect looks
    identical — so binding, not fetch metadata, has to carry the rejection."""
    _patch_exchange_to_return_attacker_identity(monkeypatch)

    attacker = TestClient(app, follow_redirects=False)
    captured_state = _authorize_query(attacker.get("/auth/login"))["state"]

    victim = TestClient(app, follow_redirects=False)
    response = victim.get(
        f"/auth/callback?code={ATTACKER_CODE}&state={captured_state}",
        headers={
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Referer": "https://evil.example.com/",
            "User-Agent": "victim-browser/1.0",
        },
    )

    _assert_no_session_issued(response, repository)


def test_captured_state_is_useless_without_the_cookie_across_many_retries(
    app, monkeypatch, repository
):
    """The report established that failed attempts don't consume the code, so
    retries are free and the attack is repeatable. Repetition must not help:
    every attempt fails the same way and the state row survives for its
    rightful owner."""
    _patch_exchange_to_return_attacker_identity(monkeypatch)

    attacker = TestClient(app, follow_redirects=False)
    captured_state = _authorize_query(attacker.get("/auth/login"))["state"]

    for attempt in range(8):
        victim = TestClient(app, follow_redirects=False)
        response = victim.get(
            f"/auth/callback?code={ATTACKER_CODE}&state={captured_state}"
        )
        qs = dict(
            urllib.parse.parse_qsl(
                urllib.parse.urlparse(response.headers["location"]).query
            )
        )
        assert qs["auth_error"] == "missing_state_cookie", f"attempt {attempt}"
        _assert_no_session_issued(response, repository)


@pytest.mark.parametrize(
    "forged_cookie",
    [
        "",  # empty
        "guessed-binding-secret",
        "0" * 43,  # right shape, wrong value
    ],
)
def test_guessing_the_binding_cookie_does_not_work(
    app, monkeypatch, repository, forged_cookie
):
    """The binding is 32 bytes of CSPRNG output and only its digest is stored,
    so there is nothing to guess and nothing to steal from the state row."""
    _patch_exchange_to_return_attacker_identity(monkeypatch)

    attacker = TestClient(app, follow_redirects=False)
    captured_state = _authorize_query(attacker.get("/auth/login"))["state"]

    victim = TestClient(app, follow_redirects=False)
    if forged_cookie:
        victim.cookies.set(OAUTH_STATE_COOKIE_NAME, forged_cookie)
    response = victim.get(
        f"/auth/callback?code={ATTACKER_CODE}&state={captured_state}"
    )

    _assert_no_session_issued(response, repository)


def test_the_browser_that_started_the_login_still_completes_it(
    app, monkeypatch, repository
):
    """The other half of the contract: binding must not break real logins.

    Replays the whole round-trip in one client, carrying the cookie forward
    from the /auth/login response the way a browser would, and asserts the
    session is issued to the identity the IdP named.
    """
    exchange = _patch_exchange_to_return_attacker_identity(monkeypatch)

    client = TestClient(app, follow_redirects=False)
    login = client.get("/auth/login")
    params = _authorize_query(login)

    # PKCE and nonce went out on the authorize request...
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"]
    assert params["nonce"]

    # ...and the browser holds the binding. TestClient's jar won't replay a
    # `Secure` cookie over the http test transport, so re-seat it explicitly —
    # a real browser sends it because /auth/login is served over HTTPS.
    binding = _issued_binding_cookie(login)
    assert binding
    client.cookies.set(OAUTH_STATE_COOKIE_NAME, binding)

    # The ID token has to echo the nonce Cognito was given, per
    # docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html.
    exchange.return_value = ExchangeResult(
        access_token="real.access.token",
        refresh_token="real.refresh.token",
        id_token=make_id_token(sub="legit-user", nonce=params["nonce"]),
        access_token_exp=int(time.time()) + 3600,
    )

    response = client.get(
        f"/auth/callback?code=legitimate-code&state={params['state']}"
    )

    assert response.status_code == 302
    assert "auth_error" not in response.headers["location"]
    session_headers = [
        h
        for h in response.headers.get_list("set-cookie")
        if h.startswith(SESSION_COOKIE_NAME)
    ]
    assert session_headers and "=;" not in session_headers[0]

    items = repository._table.scan().get("Items", [])
    assert len(items) == 1
    assert items[0]["user_id"] == "legit-user"
    # The verifier that was withheld from the browser is what redeemed the code.
    assert exchange.await_args.kwargs["code_verifier"]
