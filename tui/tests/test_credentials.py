"""Credential discriminant and endpoint construction.

These two modules exist to stop "is an API key set?" standing in for "is this
client authenticated?", and to stop endpoint paths living in the config module.
"""

from __future__ import annotations

import pytest

from agentcore_tui.client.auth import ApiKeyAuth, AuthProvider, NoAuth, SessionAuth
from agentcore_tui.client.endpoints import Endpoints
from agentcore_tui.credentials import CAPABILITIES, Capability, CredentialSource, resolve_source


class TestResolution:
    def test_no_base_url_means_no_credential(self) -> None:
        """A credential with nowhere to go is not usable."""
        assert resolve_source(base_url="", api_key="k") is CredentialSource.NONE

    def test_an_api_key_resolves_to_api_key(self) -> None:
        assert resolve_source(base_url="https://h", api_key="k") is CredentialSource.API_KEY

    def test_a_stored_session_resolves_to_a_bff_session(self) -> None:
        source = resolve_source(base_url="https://h", api_key=None, session_probe=lambda _: True)
        assert source is CredentialSource.BFF_SESSION

    def test_nothing_stored_resolves_to_none(self) -> None:
        source = resolve_source(base_url="https://h", api_key=None, session_probe=lambda _: False)
        assert source is CredentialSource.NONE

    def test_a_session_now_wins_over_an_api_key(self) -> None:
        """Flipped when `client/agent_stream.py` landed.

        A session is strictly more capable — it reaches the tool-using agent,
        the conversation list and the catalogues, none of which an API key can
        touch. The old order was mechanical, not a judgement: the only transport
        that existed spoke `/chat/api-converse`, which accepts `X-API-Key` and
        nothing else, so preferring a session would have selected a credential
        no transport could present.
        """
        source = resolve_source(base_url="https://h", api_key="k", session_probe=lambda _: True)
        assert source is CredentialSource.BFF_SESSION

    def test_an_api_key_is_still_used_when_there_is_no_session(self) -> None:
        """Flipping the order must not strand API-key-only users."""
        source = resolve_source(base_url="https://h", api_key="k", session_probe=lambda _: False)
        assert source is CredentialSource.API_KEY

    def test_the_probe_receives_the_base_url(self) -> None:
        seen: list[str] = []
        resolve_source(base_url="https://h/api", api_key=None, session_probe=lambda url: bool(seen.append(url)))
        assert seen == ["https://h/api"]


class TestUsability:
    def test_none_is_not_usable(self) -> None:
        assert CredentialSource.NONE.usable is False

    @pytest.mark.parametrize("source", [CredentialSource.API_KEY, CredentialSource.BFF_SESSION])
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
            assert CredentialSource.BFF_SESSION.can(capability) is True

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
        # Trailing slash on purpose: the router mounts `/tools/` and the bare
        # path answers 307, which httpx would follow but which costs a round trip
        # on every catalogue read.
        assert endpoints.tools == "https://h/api/tools/"
        assert endpoints.skills == "https://h/api/skills/"
        assert endpoints.system_prompts == "https://h/api/system-prompts/"
        assert endpoints.tool_preferences == "https://h/api/tools/preferences"
        assert endpoints.skill_preferences == "https://h/api/skills/preferences"
        assert endpoints.generate_title == "https://h/api/chat/generate-title"
        assert endpoints.sessions_bulk_delete == "https://h/api/sessions/bulk-delete"

    def test_session_sub_paths(self) -> None:
        endpoints = Endpoints("https://h/api")
        assert endpoints.session("abc") == "https://h/api/sessions/abc"
        assert endpoints.session_messages("abc") == "https://h/api/sessions/abc/messages"
        assert endpoints.session_interrupt("abc") == "https://h/api/sessions/abc/interrupt"
        assert endpoints.session_metadata("abc") == "https://h/api/sessions/abc/metadata"
        assert endpoints.session_read("abc") == "https://h/api/sessions/abc/read"
        assert endpoints.session_unread("abc") == "https://h/api/sessions/abc/unread"
        assert endpoints.session_pending_interrupts("abc") == "https://h/api/sessions/abc/pending-interrupts"

    def test_session_ids_are_url_encoded(self) -> None:
        """Ids are client-minted, so a caller could supply anything."""
        assert Endpoints("https://h").session("a/b?c") == "https://h/sessions/a%2Fb%3Fc"

    def test_device_auth_endpoints(self) -> None:
        endpoints = Endpoints("https://h/api/")
        assert endpoints.cli_authorize == "https://h/api/auth/cli/authorize"
        assert endpoints.cli_token == "https://h/api/auth/cli/token"
        assert endpoints.auth_session == "https://h/api/auth/session"

    def test_there_is_no_verify_url_builder(self) -> None:
        """`/auth/cli/verify` is deliberately absent.

        The server returns `verification_uri_complete`, derived from its own
        `BFF_AUTH_CALLBACK_URL`. A client that built the URL itself would send
        users to the wrong host on any deployment whose routing differs, and
        would silently stop working when the server's derivation changed.
        """
        assert not hasattr(Endpoints("https://h"), "cli_verify")


class TestAuthProviders:
    async def test_api_key_sets_the_header_app_api_expects(self) -> None:
        assert await ApiKeyAuth("secret").headers() == {"X-API-Key": "secret"}

    async def test_session_sends_the_bff_scheme_the_middleware_looks_for(self) -> None:
        """`BFF`, not `Bearer`.

        `sealed_session_from_header` in app-api's SessionRefreshMiddleware
        matches on this scheme. Sending `Bearer` would fall through to the
        no-credential path and 401.
        """
        assert await SessionAuth("sealed").headers() == {"Authorization": "BFF sealed"}

    async def test_no_auth_sends_nothing(self) -> None:
        assert await NoAuth().headers() == {}

    def test_sources_are_reported(self) -> None:
        assert ApiKeyAuth("k").source is CredentialSource.API_KEY
        assert SessionAuth("sealed").source is CredentialSource.BFF_SESSION
        assert NoAuth().source is CredentialSource.NONE

    def test_the_key_never_appears_in_a_repr(self) -> None:
        assert "secret" not in repr(ApiKeyAuth("secret"))

    def test_the_sealed_session_never_appears_in_a_repr(self) -> None:
        """It is a bearer credential for the whole account."""
        assert "sealed-value" not in repr(SessionAuth("sealed-value"))

    @pytest.mark.parametrize("provider", [ApiKeyAuth("k"), NoAuth(), SessionAuth("s")])
    def test_implementations_satisfy_the_protocol(self, provider: object) -> None:
        assert isinstance(provider, AuthProvider)
