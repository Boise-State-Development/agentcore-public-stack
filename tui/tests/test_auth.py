"""Tests for the OIDC/PKCE login machinery.

No network and no browser: the token endpoint is an httpx.MockTransport, and the
loopback receiver is driven by a real HTTP request to the port it bound, which
is exactly what a browser redirect does.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from agentcore_tui.auth import (
    CognitoOidcClient,
    LoopbackReceiver,
    OidcConfig,
    PkceChallenge,
    TokenSet,
    build_authorize_url,
    challenge_for,
    decode_claims,
    find_free_port,
    generate_state,
    generate_verifier,
)
from agentcore_tui.auth.flow import StateMismatchError, perform_login
from agentcore_tui.auth.loopback import LoopbackError
from agentcore_tui.auth.oidc import AuthorizationError, SessionExpiredError
from agentcore_tui.errors import ConfigError

DOMAIN = "https://example.auth.us-west-2.amazoncognito.com"
CLIENT_ID = "test-cli-client"
REDIRECT = "http://localhost:8976/callback"


def oidc_config(**kwargs: object) -> OidcConfig:
    params = {"domain_url": DOMAIN, "client_id": CLIENT_ID, "redirect_uri": REDIRECT}
    params.update(kwargs)  # type: ignore[arg-type]
    return OidcConfig(**params)  # type: ignore[arg-type]


class TestPkce:
    def test_verifier_length_is_within_rfc_bounds(self) -> None:
        # RFC 7636 §4.1 requires 43-128 characters.
        verifier = generate_verifier()
        assert 43 <= len(verifier) <= 128

    def test_verifier_is_url_safe_and_unpadded(self) -> None:
        verifier = generate_verifier()
        assert "=" not in verifier
        assert "+" not in verifier and "/" not in verifier

    def test_verifiers_are_unique(self) -> None:
        assert len({generate_verifier() for _ in range(50)}) == 50

    def test_challenge_matches_the_rfc_definition(self) -> None:
        """challenge = BASE64URL(SHA256(ASCII(verifier))), unpadded."""
        verifier = generate_verifier()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        assert challenge_for(verifier) == expected

    def test_challenge_is_deterministic(self) -> None:
        verifier = generate_verifier()
        assert challenge_for(verifier) == challenge_for(verifier)

    def test_state_values_are_unique(self) -> None:
        assert len({generate_state() for _ in range(50)}) == 50

    def test_verifier_is_absent_from_repr(self) -> None:
        """It is the credential that redeems the code; keep it out of logs."""
        challenge = PkceChallenge.create()
        assert challenge.verifier not in repr(challenge)

    def test_method_is_s256(self) -> None:
        assert PkceChallenge.create().method == "S256"


class TestAuthorizeUrl:
    def test_contains_every_required_parameter(self) -> None:
        challenge = PkceChallenge.create()
        params = parse_qs(urlparse(build_authorize_url(oidc_config(), challenge)).query)

        assert params["response_type"] == ["code"]
        assert params["client_id"] == [CLIENT_ID]
        assert params["redirect_uri"] == [REDIRECT]
        assert params["code_challenge"] == [challenge.challenge]
        assert params["code_challenge_method"] == ["S256"]
        assert params["state"] == [challenge.state]
        assert params["scope"] == ["openid profile email"]

    def test_never_includes_the_verifier(self) -> None:
        """The verifier goes in the token exchange, never the browser URL."""
        challenge = PkceChallenge.create()
        assert challenge.verifier not in build_authorize_url(oidc_config(), challenge)

    def test_identity_provider_is_forwarded_when_given(self) -> None:
        url = build_authorize_url(oidc_config(), PkceChallenge.create(), identity_provider="ms-entra-id")
        assert parse_qs(urlparse(url).query)["identity_provider"] == ["ms-entra-id"]

    def test_identity_provider_omitted_when_absent(self) -> None:
        url = build_authorize_url(oidc_config(), PkceChallenge.create())
        assert "identity_provider" not in parse_qs(urlparse(url).query)

    def test_points_at_the_hosted_ui_authorize_endpoint(self) -> None:
        url = build_authorize_url(oidc_config(), PkceChallenge.create())
        assert url.startswith(f"{DOMAIN}/oauth2/authorize?")


class TestOidcConfigValidation:
    def test_missing_domain_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="Cognito domain"):
            oidc_config(domain_url="")

    def test_missing_client_id_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="client id"):
            oidc_config(client_id="")

    def test_trailing_slash_on_domain_is_tolerated(self) -> None:
        config = oidc_config(domain_url=f"{DOMAIN}/")
        assert config.token_endpoint == f"{DOMAIN}/oauth2/token"


class TestTokenSet:
    def test_parses_a_token_response(self) -> None:
        tokens = TokenSet.from_token_response(
            {"access_token": "at", "expires_in": 3600, "refresh_token": "rt", "token_type": "Bearer"},
            now=1000.0,
        )
        assert tokens.access_token == "at"
        assert tokens.refresh_token == "rt"
        assert tokens.expires_at == 4600.0

    def test_defaults_expiry_when_absent(self) -> None:
        tokens = TokenSet.from_token_response({"access_token": "at"}, now=0.0)
        assert tokens.expires_at == 3600.0

    def test_missing_access_token_is_an_error(self) -> None:
        with pytest.raises(ConfigError, match="no access_token"):
            TokenSet.from_token_response({"expires_in": 60})

    def test_expired_accounts_for_skew(self) -> None:
        """Expiring in 30s counts as expired, so a request cannot race it."""
        assert TokenSet(access_token="at", expires_at=time.time() + 30).expired is True
        assert TokenSet(access_token="at", expires_at=time.time() + 3600).expired is False

    def test_authorization_header_format(self) -> None:
        tokens = TokenSet(access_token="abc", expires_at=time.time() + 60)
        assert tokens.authorization_header() == "Bearer abc"

    def test_tokens_are_absent_from_repr(self) -> None:
        tokens = TokenSet(access_token="secret-at", expires_at=0.0, refresh_token="secret-rt")
        assert "secret-at" not in repr(tokens)
        assert "secret-rt" not in repr(tokens)

    def test_with_refresh_token_preserves_existing(self) -> None:
        tokens = TokenSet(access_token="at", expires_at=0.0, refresh_token="original")
        assert tokens.with_refresh_token(None).refresh_token == "original"
        assert tokens.with_refresh_token("newer").refresh_token == "newer"


class TestDecodeClaims:
    def test_decodes_an_unsigned_payload(self) -> None:
        payload = base64.urlsafe_b64encode(json.dumps({"sub": "u1", "client_id": "c1"}).encode()).decode().rstrip("=")
        claims = decode_claims(f"header.{payload}.signature")
        assert claims["sub"] == "u1"
        assert claims["client_id"] == "c1"

    @pytest.mark.parametrize("token", ["", "not-a-jwt", "only.two", "a.!!!.c"])
    def test_malformed_tokens_yield_empty_claims(self, token: str) -> None:
        assert decode_claims(token) == {}


class TestTokenExchange:
    def make_client(self, handler: object) -> CognitoOidcClient:
        transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
        return CognitoOidcClient(oidc_config(), client=httpx.AsyncClient(transport=transport))

    async def test_sends_verifier_and_no_client_secret(self) -> None:
        """A public client authenticates the exchange with PKCE, not a secret."""
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["form"] = parse_qs(request.content.decode())
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600, "refresh_token": "rt"})

        challenge = PkceChallenge.create()
        async with self.make_client(handler) as client:
            tokens = await client.exchange_code("the-code", challenge)

        form = seen["form"]
        assert isinstance(form, dict)
        assert form["grant_type"] == ["authorization_code"]
        assert form["code"] == ["the-code"]
        assert form["code_verifier"] == [challenge.verifier]
        assert form["client_id"] == [CLIENT_ID]
        assert form["redirect_uri"] == [REDIRECT]
        # No HTTP Basic: there is no secret to send.
        assert seen["auth"] is None
        assert seen["url"] == f"{DOMAIN}/oauth2/token"
        assert tokens.access_token == "at"

    async def test_oauth_error_is_surfaced_with_its_description(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Code expired"})

        async with self.make_client(handler) as client:
            with pytest.raises(AuthorizationError, match="invalid_grant.*Code expired"):
                await client.exchange_code("stale", PkceChallenge.create())

    async def test_non_json_error_still_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, content=b"<html>gateway</html>")

        async with self.make_client(handler) as client:
            with pytest.raises(AuthorizationError, match="503"):
                await client.exchange_code("code", PkceChallenge.create())


class TestRefresh:
    def make_client(self, handler: object) -> CognitoOidcClient:
        transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
        return CognitoOidcClient(oidc_config(), client=httpx.AsyncClient(transport=transport))

    async def test_carries_the_refresh_token_forward(self) -> None:
        """Cognito omits refresh_token on refresh; dropping it would force a
        full re-login at the next expiry."""

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

        async with self.make_client(handler) as client:
            tokens = await client.refresh("original-rt")

        assert tokens.access_token == "fresh"
        assert tokens.refresh_token == "original-rt"

    async def test_rejected_refresh_raises_session_expired(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        async with self.make_client(handler) as client:
            with pytest.raises(SessionExpiredError):
                await client.refresh("revoked")

    async def test_revoke_reports_success_and_failure(self) -> None:
        async with self.make_client(lambda _: httpx.Response(200)) as client:
            assert await client.revoke("rt") is True

        async with self.make_client(lambda _: httpx.Response(400, json={"error": "invalid_token"})) as client:
            assert await client.revoke("rt") is False

    async def test_revoke_failure_does_not_raise(self) -> None:
        """Logout must clear local state even with no network."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        async with self.make_client(handler) as client:
            assert await client.revoke("rt") is False


class TestLoopbackReceiver:
    def test_binds_a_candidate_port_and_reports_its_redirect_uri(self) -> None:
        port = find_free_port([8976, 8977, 8978])
        if port is None:
            pytest.skip("no candidate port free on this host")
        with LoopbackReceiver([port]) as receiver:
            assert receiver.port == port
            # Must match what is registered on the app client, byte for byte.
            assert receiver.redirect_uri == f"http://localhost:{port}/callback"

    def test_falls_through_to_the_next_port_when_the_first_is_taken(self) -> None:
        first = find_free_port([8976, 8977, 8978])
        if first is None:
            pytest.skip("no candidate port free on this host")
        with LoopbackReceiver([first]):
            # Occupying `first`, a second receiver offered [first, other] must
            # skip to `other`.
            other = find_free_port([p for p in (8976, 8977, 8978) if p != first])
            if other is None:
                pytest.skip("need a second free candidate port")
            with LoopbackReceiver([first, other]) as second:
                assert second.port == other

    def test_no_bindable_port_raises(self) -> None:
        port = find_free_port([8976, 8977, 8978])
        if port is None:
            pytest.skip("no candidate port free on this host")
        with LoopbackReceiver([port]):
            with pytest.raises(LoopbackError, match="Could not bind"):
                LoopbackReceiver([port]).start()

    def test_captures_code_and_state_from_a_real_redirect(self) -> None:
        """Drives it exactly as a browser would: a GET to the callback URL."""
        port = find_free_port([8976, 8977, 8978])
        if port is None:
            pytest.skip("no candidate port free on this host")

        with LoopbackReceiver([port]) as receiver:

            def visit() -> None:
                urllib.request.urlopen(f"{receiver.redirect_uri}?code=abc123&state=xyz", timeout=5).read()

            threading.Thread(target=visit, daemon=True).start()
            result = receiver.wait(timeout=10)

        assert result.code == "abc123"
        assert result.state == "xyz"
        assert result.ok is True

    def test_captures_an_error_redirect(self) -> None:
        port = find_free_port([8976, 8977, 8978])
        if port is None:
            pytest.skip("no candidate port free on this host")

        with LoopbackReceiver([port]) as receiver:

            def visit() -> None:
                urllib.request.urlopen(
                    f"{receiver.redirect_uri}?error=access_denied&error_description=User+cancelled",
                    timeout=5,
                ).read()

            threading.Thread(target=visit, daemon=True).start()
            result = receiver.wait(timeout=10)

        assert result.error == "access_denied"
        assert result.error_description == "User cancelled"
        assert result.ok is False

    def test_times_out_when_no_redirect_arrives(self) -> None:
        port = find_free_port([8976, 8977, 8978])
        if port is None:
            pytest.skip("no candidate port free on this host")
        with LoopbackReceiver([port]) as receiver:
            with pytest.raises(LoopbackError, match="within"):
                receiver.wait(timeout=0.3)

    def test_empty_port_list_is_rejected(self) -> None:
        with pytest.raises(LoopbackError):
            LoopbackReceiver([])


class TestPerformLogin:
    """End-to-end flow with a scripted browser and a mocked token endpoint."""

    async def run_login(self, *, redirect_query, token_handler, monkeypatch) -> object:
        port = find_free_port([8976, 8977, 8978])
        if port is None:
            pytest.skip("no candidate port free on this host")

        opened: dict[str, str] = {}

        def fake_open(url: str) -> bool:
            opened["url"] = url
            # Stand in for the browser: hit the loopback callback.
            query = redirect_query(url)
            threading.Thread(
                target=lambda: urllib.request.urlopen(f"http://localhost:{port}/callback?{query}", timeout=5).read(),
                daemon=True,
            ).start()
            return True

        monkeypatch.setattr("agentcore_tui.auth.flow.webbrowser.open", fake_open)

        transport = httpx.MockTransport(token_handler)
        return await perform_login(
            base_url="https://api.example.test",
            domain_url=DOMAIN,
            client_id=CLIENT_ID,
            ports=(port,),
            timeout=10.0,
            client=httpx.AsyncClient(transport=transport),
        )

    async def test_happy_path_yields_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("agentcore_tui.auth.flow.save_refresh_token", lambda *a: None)

        def redirect_query(url: str) -> str:
            state = parse_qs(urlparse(url).query)["state"][0]
            return f"code=good-code&state={state}"

        seen: dict[str, object] = {}

        def token_handler(request: httpx.Request) -> httpx.Response:
            seen["form"] = parse_qs(request.content.decode())
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600, "refresh_token": "rt"})

        outcome, url = await self.run_login(redirect_query=redirect_query, token_handler=token_handler, monkeypatch=monkeypatch)

        assert outcome.tokens.access_token == "at"  # type: ignore[attr-defined]
        assert outcome.refresh_token_stored is True  # type: ignore[attr-defined]
        assert url.startswith(f"{DOMAIN}/oauth2/authorize")
        form = seen["form"]
        assert isinstance(form, dict)
        assert form["code"] == ["good-code"]

    async def test_state_mismatch_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A forged redirect to our loopback port must not be redeemable."""
        monkeypatch.setattr("agentcore_tui.auth.flow.save_refresh_token", lambda *a: None)

        def redirect_query(_: str) -> str:
            return "code=attacker-code&state=not-the-state-we-sent"

        def token_handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("token endpoint must not be called on state mismatch")

        with pytest.raises(StateMismatchError):
            await self.run_login(redirect_query=redirect_query, token_handler=token_handler, monkeypatch=monkeypatch)

    async def test_user_denial_surfaces_the_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def redirect_query(_: str) -> str:
            return "error=access_denied&error_description=User+cancelled"

        def token_handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("token endpoint must not be called after a denial")

        with pytest.raises(AuthorizationError, match="User cancelled"):
            await self.run_login(redirect_query=redirect_query, token_handler=token_handler, monkeypatch=monkeypatch)

    async def test_keyring_failure_still_returns_a_usable_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A session good until exit beats refusing to log in at all."""

        def explode(*_: object) -> None:
            raise ConfigError("no keyring here", hint="use env vars")

        monkeypatch.setattr("agentcore_tui.auth.flow.save_refresh_token", explode)

        def redirect_query(url: str) -> str:
            state = parse_qs(urlparse(url).query)["state"][0]
            return f"code=good-code&state={state}"

        def token_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600, "refresh_token": "rt"})

        outcome, _ = await self.run_login(redirect_query=redirect_query, token_handler=token_handler, monkeypatch=monkeypatch)

        assert outcome.tokens.access_token == "at"  # type: ignore[attr-defined]
        assert outcome.refresh_token_stored is False  # type: ignore[attr-defined]
        assert outcome.keyring_error is not None  # type: ignore[attr-defined]
