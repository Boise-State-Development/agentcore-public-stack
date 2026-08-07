"""Credential discriminant and endpoint construction.

These two modules exist to stop "is an API key set?" standing in for "is this
client authenticated?", and to stop endpoint paths living in the config module.
"""

from __future__ import annotations

import pytest

from agentcore_tui.client.auth import ApiKeyAuth, AuthProvider, BearerAuth, NoAuth
from agentcore_tui.client.endpoints import Endpoints
from agentcore_tui.credentials import CAPABILITIES, Capability, CredentialSource, resolve_source


class TestResolution:
    def test_no_base_url_means_no_credential(self) -> None:
        """A credential with nowhere to go is not usable."""
        assert resolve_source(base_url="", api_key="k") is CredentialSource.NONE

    def test_an_api_key_resolves_to_api_key(self) -> None:
        assert resolve_source(base_url="https://h", api_key="k") is CredentialSource.API_KEY

    def test_a_stored_session_resolves_to_sso(self) -> None:
        source = resolve_source(base_url="https://h", api_key=None, session_probe=lambda _: True)
        assert source is CredentialSource.SSO_SESSION

    def test_nothing_stored_resolves_to_none(self) -> None:
        source = resolve_source(base_url="https://h", api_key=None, session_probe=lambda _: False)
        assert source is CredentialSource.NONE

    def test_api_key_currently_wins_over_a_session(self) -> None:
        """Not because it is better — a session is strictly more capable — but
        because no bearer transport exists yet, so selecting a session would
        select a path that cannot issue a request. Flip this, and this test,
        when the app-api bearer branch lands."""
        source = resolve_source(base_url="https://h", api_key="k", session_probe=lambda _: True)
        assert source is CredentialSource.API_KEY

    def test_the_probe_receives_the_base_url(self) -> None:
        seen: list[str] = []
        resolve_source(base_url="https://h/api", api_key=None, session_probe=lambda url: bool(seen.append(url)))
        assert seen == ["https://h/api"]


class TestUsability:
    def test_none_is_not_usable(self) -> None:
        assert CredentialSource.NONE.usable is False

    @pytest.mark.parametrize("source", [CredentialSource.API_KEY, CredentialSource.SSO_SESSION])
    def test_real_credentials_are_usable(self, source: CredentialSource) -> None:
        assert source.usable is True

    @pytest.mark.parametrize("source", list(CredentialSource))
    def test_every_source_has_a_label(self, source: CredentialSource) -> None:
        assert source.label


class TestCapabilities:
    def test_an_api_key_can_chat_but_nothing_more(self) -> None:
        """/chat/api-converse is the only API-key endpoint in app-api, and it has
        no tools, memory or session persistence."""
        assert CredentialSource.API_KEY.can(Capability.CHAT) is True
        assert CredentialSource.API_KEY.can(Capability.AGENT) is False
        assert CredentialSource.API_KEY.can(Capability.SESSIONS) is False
        assert CredentialSource.API_KEY.can(Capability.CATALOG) is False

    def test_a_session_reaches_everything(self) -> None:
        for capability in Capability:
            assert CredentialSource.SSO_SESSION.can(capability) is True

    def test_no_credential_reaches_nothing(self) -> None:
        for capability in Capability:
            assert CredentialSource.NONE.can(capability) is False

    @pytest.mark.parametrize("source", list(CredentialSource))
    def test_every_source_is_mapped(self, source: CredentialSource) -> None:
        """A new source without an entry would raise on first `can()`."""
        assert source in CAPABILITIES


class TestEndpoints:
    @pytest.mark.parametrize("base", ["https://h/api", "https://h/api/", "https://h/api///"])
    def test_trailing_slashes_never_double_up(self, base: str) -> None:
        assert Endpoints(base).api_converse == "https://h/api/chat/api-converse"

    def test_known_paths(self) -> None:
        endpoints = Endpoints("https://h/api")
        assert endpoints.health == "https://h/api/health"
        assert endpoints.chat_stream == "https://h/api/chat/stream"
        assert endpoints.sessions == "https://h/api/sessions"
        assert endpoints.models == "https://h/api/models"
        assert endpoints.tools == "https://h/api/tools"

    def test_session_sub_paths(self) -> None:
        endpoints = Endpoints("https://h/api")
        assert endpoints.session("abc") == "https://h/api/sessions/abc"
        assert endpoints.session_messages("abc") == "https://h/api/sessions/abc/messages"
        assert endpoints.session_interrupt("abc") == "https://h/api/sessions/abc/interrupt"

    def test_session_ids_are_url_encoded(self) -> None:
        """Ids are client-minted, so a caller could supply anything."""
        assert Endpoints("https://h").session("a/b?c") == "https://h/sessions/a%2Fb%3Fc"


class TestAuthProviders:
    async def test_api_key_sets_the_header_app_api_expects(self) -> None:
        assert await ApiKeyAuth("secret").headers() == {"X-API-Key": "secret"}

    async def test_bearer_awaits_its_supplier_each_time(self) -> None:
        """Caching would keep sending a token after it expired."""
        tokens = iter(["first", "second"])

        async def supply() -> str:
            return next(tokens)

        auth = BearerAuth(supply)
        assert await auth.headers() == {"Authorization": "Bearer first"}
        assert await auth.headers() == {"Authorization": "Bearer second"}

    async def test_no_auth_sends_nothing(self) -> None:
        assert await NoAuth().headers() == {}

    def test_sources_are_reported(self) -> None:
        assert ApiKeyAuth("k").source is CredentialSource.API_KEY
        assert BearerAuth(lambda: None).source is CredentialSource.SSO_SESSION  # type: ignore[arg-type,return-value]
        assert NoAuth().source is CredentialSource.NONE

    def test_the_key_never_appears_in_a_repr(self) -> None:
        assert "secret" not in repr(ApiKeyAuth("secret"))

    @pytest.mark.parametrize("provider", [ApiKeyAuth("k"), NoAuth()])
    def test_implementations_satisfy_the_protocol(self, provider: object) -> None:
        assert isinstance(provider, AuthProvider)
