"""A first-party tool bound to a connector is OAuth-gated like an MCP tool.

Three seams, in the order a turn crosses them:

1. ``BaseAgent._build_oauth_consent_hook``'s provider lookup resolves a bound
   local tool to its connector, an unbound one (or any ordinary tool) to None,
   and an external MCP tool exactly as before.
2. ``OAuthConsentHook._gate`` with that lookup warms the token cache the tool
   body reads, or pauses the turn with the ``oauth_required`` interrupt, and the
   401 shape the tool returns triggers the hook's single retry.
3. ``_build_meeting_tools`` on the inference route builds the families only
   while ``MEETING_TASKS_ENABLED`` is on, with the connector from the catalog
   snapshot closed over; the snapshot itself reads only ``local`` rows.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from strands import tool
from strands.interrupt import InterruptException
from strands.tools.mcp import MCPAgentTool

from agents.local_tools import meeting_tasks as mt
from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.integrations import oauth_token_cache
from agents.main_agent.session.hooks.oauth_consent import OAuthConsentHook
from apis.shared.feature_flags import meeting_tasks_enabled
from apis.shared.oauth.agentcore_identity import TokenResult
from apis.shared.tools import freshness
from apis.shared.tools.injected import (
    INJECTED_TOOL_IDS,
    KEY_DESCRIBED_INJECTED_TOOL_IDS,
    MEETING_TASKS_TOOL_IDS,
    injected_tools_are_key_described,
)

USER = "alice"


@pytest.fixture(autouse=True)
def _isolate():
    oauth_token_cache.clear_user(USER)
    freshness._reset_for_tests()
    yield
    oauth_token_cache.clear_user(USER)
    freshness._reset_for_tests()


def _provider_lookup():
    """The lookup `_build_oauth_consent_hook` installs, with the MCP integration stubbed."""
    integration = SimpleNamespace(provider_for_client=lambda client: getattr(client, "provider", None))
    agent = SimpleNamespace(user_id=USER)
    with patch(
        "agents.main_agent.integrations.external_mcp_client.get_external_mcp_integration",
        return_value=integration,
    ):
        hook = BaseAgent._build_oauth_consent_hook(agent)
    return hook._provider_lookup


@tool
async def plain_tool() -> dict:
    """An ordinary tool."""
    return {}


class TestProviderLookup:
    def test_bound_local_tool_resolves_to_its_connector(self):
        lookup = _provider_lookup()
        [calendar] = mt.make_google_calendar_tools(USER, "google-conn")
        assert lookup(calendar) == "google-conn"

    def test_unbound_local_tool_and_ordinary_tool_resolve_to_none(self):
        lookup = _provider_lookup()
        [calendar] = mt.make_google_calendar_tools(USER, None)
        assert lookup(calendar) is None
        assert lookup(plain_tool) is None
        assert lookup(None) is None

    def test_external_mcp_tool_still_resolves_through_its_client(self):
        lookup = _provider_lookup()
        mcp_tool = MagicMock(spec=MCPAgentTool)
        mcp_tool.mcp_client = SimpleNamespace(provider="canvas")
        assert lookup(mcp_tool) == "canvas"


def _event(selected_tool):
    event = MagicMock()
    event.selected_tool = selected_tool
    event.cancel_tool = None
    event.interrupt = MagicMock(side_effect=InterruptException(MagicMock()))
    return event


class TestConsentGateOnLocalTools:
    @pytest.mark.asyncio
    async def test_vault_hit_warms_the_cache_the_tool_reads(self):
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(return_value=TokenResult(access_token="fresh"))
        hook = OAuthConsentHook(
            user_id=USER,
            provider_lookup=_provider_lookup(),
            scopes_lookup=lambda _pid: ["https://www.googleapis.com/auth/calendar.readonly"],
        )
        [calendar] = mt.make_google_calendar_tools(USER, "google-conn")

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(_event(calendar))

        kwargs = identity.get_token_for_user.call_args.kwargs
        assert kwargs["provider_name"] == "google-conn" and kwargs["user_id"] == USER
        assert oauth_token_cache.get(USER, "google-conn") == "fresh"
        # The same (user, provider) key the tool body reads.
        assert mt._bearer(USER, "google-conn", "Google Calendar", "google_calendar_meetings") == ("fresh", None)

    @pytest.mark.asyncio
    async def test_consent_required_pauses_the_turn(self):
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock(return_value=TokenResult(authorization_url="https://zoom.us/oauth/authorize?x"))
        hook = OAuthConsentHook(
            user_id=USER,
            provider_lookup=_provider_lookup(),
            scopes_lookup=lambda _pid: ["recording:read"],
        )
        zoom_tool = mt.make_zoom_tools(USER, "zoom-conn")[1]
        event = _event(zoom_tool)

        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            with pytest.raises(InterruptException):
                await hook._gate(event)

        event.interrupt.assert_called_once()
        assert event.interrupt.call_args.kwargs["name"] == "oauth:zoom-conn"
        assert event.interrupt.call_args.kwargs["reason"]["providerId"] == "zoom-conn"

    @pytest.mark.asyncio
    async def test_unbound_tool_is_not_gated(self):
        identity = MagicMock()
        identity.get_token_for_user = AsyncMock()
        hook = OAuthConsentHook(user_id=USER, provider_lookup=_provider_lookup(), scopes_lookup=lambda _pid: [])
        [calendar] = mt.make_google_calendar_tools(USER, None)
        with patch(
            "agents.main_agent.session.hooks.oauth_consent.get_agentcore_identity_client",
            return_value=identity,
        ):
            await hook._gate(_event(calendar))
        identity.get_token_for_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_tools_401_shape_clears_the_cache_and_retries_once(self):
        oauth_token_cache.set(USER, "github-conn", "stale")
        hook = OAuthConsentHook(user_id=USER, provider_lookup=_provider_lookup(), scopes_lookup=lambda _pid: [])
        github_tool = mt.make_github_tools(USER, "github-conn")[0]

        event = MagicMock()
        event.selected_tool = github_tool
        event.tool_use = {"name": github_tool.tool_name}
        event.result = mt._auth_error("GitHub")
        event.retry = False

        await hook._handle_auth_failure(event)
        assert event.retry is True
        assert oauth_token_cache.get(USER, "github-conn") is None

        event.retry = False
        await hook._handle_auth_failure(event)
        assert event.retry is False  # one retry per provider per turn


class TestFlag:
    def test_defaults_off_and_only_true_enables(self, monkeypatch):
        monkeypatch.delenv("MEETING_TASKS_ENABLED", raising=False)
        assert not meeting_tasks_enabled()
        for value in ("", "false", "1", "yes"):
            monkeypatch.setenv("MEETING_TASKS_ENABLED", value)
            assert not meeting_tasks_enabled()
        for value in ("true", " TRUE "):
            monkeypatch.setenv("MEETING_TASKS_ENABLED", value)
            assert meeting_tasks_enabled()


def _row(tool_id, protocol="local", provider=None):
    return SimpleNamespace(
        tool_id=tool_id,
        protocol=protocol,
        requires_oauth_provider=provider,
        is_public=False,
        always_on=False,
        mcp_config=None,
        mcp_gateway_config=None,
        system=False,
        status="active",
    )


def _patch_catalog(rows):
    repo = SimpleNamespace(list_tools=AsyncMock(return_value=rows))
    return patch("apis.shared.tools.repository.get_tool_catalog_repository", return_value=repo)


class TestConnectorSnapshot:
    @pytest.mark.asyncio
    async def test_reads_only_local_rows_with_a_connector(self):
        rows = [
            _row("zoom_meeting_transcripts", provider="zoom-conn"),
            _row("github_issues_local", provider="  "),
            _row("google_calendar_meetings"),
            _row("canvas", protocol="mcp_external", provider="canvas-conn"),
            _row("gateway_x", protocol=SimpleNamespace(value="mcp"), provider="x"),
        ]
        with _patch_catalog(rows) as get_repo:
            assert dict(await freshness.get_local_tool_oauth_providers()) == {"zoom_meeting_transcripts": "zoom-conn"}
            # Same single read fills the other slots.
            assert "canvas" in await freshness.get_all_tool_ids()
            assert get_repo.return_value.list_tools.await_count == 1

    @pytest.mark.asyncio
    async def test_read_failure_is_empty_then_last_known(self):
        with _patch_catalog([]) as get_repo:
            get_repo.return_value.list_tools = AsyncMock(side_effect=RuntimeError("ddb"))
            assert dict(await freshness.get_local_tool_oauth_providers()) == {}
        with _patch_catalog([_row("github_issues_local", provider="gh")]):
            assert dict(await freshness.get_local_tool_oauth_providers()) == {"github_issues_local": "gh"}
        freshness.invalidate("github_issues_local")
        with _patch_catalog([]) as get_repo:
            get_repo.return_value.list_tools = AsyncMock(side_effect=RuntimeError("ddb"))
            # Invalidated and unreadable: empty rather than stale, like the id snapshots.
            assert dict(await freshness.get_local_tool_oauth_providers()) == {}

    def test_invalidate_and_reset_clear_the_slot(self):
        freshness._local_oauth_providers_cache[0] = ({"a": "b"}, 0.0)
        freshness.invalidate()
        assert freshness._local_oauth_providers_cache[0] is None
        freshness._local_oauth_providers_cache[0] = ({"a": "b"}, 0.0)
        freshness._reset_for_tests()
        assert freshness._local_oauth_providers_cache[0] is None


class TestRouteBuilder:
    ALL = sorted(MEETING_TASKS_TOOL_IDS)

    @pytest.mark.asyncio
    async def test_off_by_default_builds_nothing_and_reads_nothing(self, monkeypatch):
        monkeypatch.delenv("MEETING_TASKS_ENABLED", raising=False)
        from apis.inference_api.chat.routes import _build_meeting_tools

        with _patch_catalog([]) as get_repo:
            assert await _build_meeting_tools(self.ALL, USER) == []
            get_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_family_enabled_builds_nothing_even_when_on(self, monkeypatch):
        monkeypatch.setenv("MEETING_TASKS_ENABLED", "true")
        from apis.inference_api.chat.routes import _build_meeting_tools

        with _patch_catalog([]) as get_repo:
            assert await _build_meeting_tools(["calculator"], USER) == []
            assert await _build_meeting_tools(None, USER) == []
            get_repo.assert_not_called()

    @pytest.mark.asyncio
    async def test_builds_enabled_families_bound_to_their_row_connectors(self, monkeypatch, caplog):
        monkeypatch.setenv("MEETING_TASKS_ENABLED", "true")
        from agents.local_tools.oauth_binding import oauth_provider_for
        from apis.inference_api.chat.routes import _build_meeting_tools

        rows = [_row("zoom_meeting_transcripts", provider="zoom-conn"), _row("github_issues_local")]
        with _patch_catalog(rows), caplog.at_level("WARNING", logger="apis.inference_api.chat.routes"):
            tools = await _build_meeting_tools(["zoom_meeting_transcripts", "github_issues_local", "calculator"], USER)

        assert [(t.tool_name, oauth_provider_for(t)) for t in tools] == [
            ("list_zoom_recordings", "zoom-conn"),
            ("get_zoom_transcript", "zoom-conn"),
            ("list_github_repositories", None),
            ("create_github_issues", None),
        ]
        assert any("github_issues_local" in r.getMessage() for r in caplog.records)


class TestCacheEligibility:
    def test_family_is_injected_and_key_described(self):
        assert MEETING_TASKS_TOOL_IDS <= INJECTED_TOOL_IDS
        assert MEETING_TASKS_TOOL_IDS <= KEY_DESCRIBED_INJECTED_TOOL_IDS
        assert injected_tools_are_key_described(["zoom_meeting_transcripts", "calculator"])
