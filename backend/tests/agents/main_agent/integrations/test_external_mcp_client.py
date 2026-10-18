"""
Tests for the external MCP client helpers.

OAuth provisioning moved to `OAuthConsentHook` (see
`tests/agents/main_agent/session/hooks/test_oauth_consent.py`); this
module covers the URL-parsing helpers and the integration's
MCPClient -> provider_id map that the hook reads from.

Requirements: 25.1–25.3
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from strands.types.exceptions import (
    MCPClientInitializationError,
    ToolProviderException,
)

from agents.main_agent.integrations.external_mcp_client import (
    ConsentGatedTool,
    ExternalMCPIntegration,
    _is_auth_failure,
    detect_aws_service_from_url,
    extract_region_from_url,
)


def _wrapped_preflight_failure(inner: BaseException) -> Exception:
    """Rebuild the exception `client.load_tools()` actually raises.

    Strands opens the MCP session on a background thread, so the real cause
    surfaces inside an anyio `ExceptionGroup`, wrapped by
    `MCPClientInitializationError` (raised by `start()`), wrapped again by
    `ToolProviderException` (raised by `load_tools()`). Neither wrapper's
    message carries the status, so a classifier that only looks at the
    exception it caught sees nothing — which is the whole point of these
    tests using the real shape instead of a bare `RuntimeError`.
    """
    group = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [inner])
    init_exc = MCPClientInitializationError(
        f"the client initialization failed: {group}"
    )
    init_exc.__cause__ = group
    outer = ToolProviderException(f"Failed to start MCP client: {init_exc}")
    outer.__cause__ = init_exc
    return outer


def _http_status_error(
    status: int, message: str = "the server rejected the request"
) -> httpx.HTTPStatusError:
    """The error `response.raise_for_status()` raises inside the MCP transport.

    `message` defaults to text with no status in it, so tests using this
    exercise the *structural* check (`.response.status_code`) rather than
    accidentally passing on httpx's usual "Client error '401 Unauthorized'"
    wording.
    """
    request = httpx.Request("POST", "https://api.example.com/mcp")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


class TestExtractRegionFromUrl:
    """Tests for extract_region_from_url region extraction."""

    def test_extracts_region_from_lambda_url(self):
        """Req 25.1: Extracts region from Lambda Function URL."""
        url = "https://abc123.lambda-url.us-west-2.on.aws/"
        assert extract_region_from_url(url) == "us-west-2"

    def test_extracts_region_from_api_gateway_url(self):
        """Req 25.1: Extracts region from API Gateway URL."""
        url = "https://xyz789.execute-api.eu-west-1.amazonaws.com/prod"
        assert extract_region_from_url(url) == "eu-west-1"

    def test_extracts_region_from_agentcore_url(self):
        """Req 25.1: Extracts region from AgentCore Gateway URL."""
        url = "https://gateway-abc.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
        assert extract_region_from_url(url) == "us-east-1"

    def test_returns_none_for_non_matching_url(self):
        """Req 25.2: Returns None when URL has no recognizable region pattern."""
        url = "https://example.com/api/v1"
        assert extract_region_from_url(url) is None

    def test_returns_none_for_plain_domain(self):
        """Req 25.2: Returns None for a plain domain with no AWS pattern."""
        url = "https://my-mcp-server.herokuapp.com/mcp"
        assert extract_region_from_url(url) is None


class TestDetectAwsServiceFromUrl:
    """Tests for detect_aws_service_from_url service detection."""

    def test_detects_lambda_service(self):
        """Req 25.3: Detects 'lambda' for Lambda Function URLs."""
        url = "https://abc123.lambda-url.us-west-2.on.aws/"
        assert detect_aws_service_from_url(url) == "lambda"

    def test_detects_execute_api_service(self):
        """Req 25.3: Detects 'execute-api' for API Gateway URLs."""
        url = "https://xyz789.execute-api.us-east-1.amazonaws.com/prod"
        assert detect_aws_service_from_url(url) == "execute-api"

    def test_detects_bedrock_agentcore_service(self):
        """Req 25.3: Detects 'bedrock-agentcore' for AgentCore Gateway URLs."""
        url = "https://gateway-abc.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
        assert detect_aws_service_from_url(url) == "bedrock-agentcore"

    def test_returns_none_for_unknown_url(self):
        """Req 25.3: Returns None for any URL that isn't a recognized AWS
        service hostname. Callers wiring SigV4 must treat None as a refusal
        — issuing a SigV4 request to an arbitrary host would attach IAM
        credentials to a request the destination has no business seeing."""
        url = "https://example.com/api/v1"
        assert detect_aws_service_from_url(url) is None


class TestAwsUrlHostSanitization:
    """Regression tests for CodeQL py/incomplete-url-substring-sanitization
    (alert #695): AWS-endpoint markers must only be honored when they appear
    in the URL *host*, not anywhere in the string. Otherwise an attacker can
    smuggle the marker into a path/query and trick the caller into attaching
    SigV4 IAM credentials to a request bound for a non-AWS host."""

    # (description, malicious_url) — every one of these must NOT be treated as AWS.
    SPOOFED_URLS = [
        (
            "marker in query string",
            "https://evil.example/?x=.execute-api.us-east-1.amazonaws.com",
        ),
        (
            "marker in path",
            "https://evil.example/.lambda-url.us-east-1.on.aws/mcp",
        ),
        (
            "real AWS suffix as a non-terminal label of an attacker domain",
            "https://x.execute-api.us-east-1.amazonaws.com.evil.example/mcp",
        ),
        (
            "marker in userinfo",
            "https://.execute-api.us-east-1.amazonaws.com@evil.example/mcp",
        ),
        (
            "agentcore marker in fragment",
            "https://evil.example/#.bedrock-agentcore.us-east-1.amazonaws.com",
        ),
    ]

    @pytest.mark.parametrize("desc,url", SPOOFED_URLS)
    def test_detect_service_rejects_spoofed_url(self, desc, url):
        assert detect_aws_service_from_url(url) is None, desc

    @pytest.mark.parametrize("desc,url", SPOOFED_URLS)
    def test_extract_region_rejects_spoofed_url(self, desc, url):
        assert extract_region_from_url(url) is None, desc

    def test_unparseable_url_is_not_aws(self):
        assert detect_aws_service_from_url("not a url") is None
        assert extract_region_from_url("not a url") is None

    def test_legitimate_hosts_still_detected(self):
        """The hardening must not regress genuine AWS endpoints (incl. paths/ports)."""
        assert (
            detect_aws_service_from_url(
                "https://x.execute-api.us-east-1.amazonaws.com:443/prod"
            )
            == "execute-api"
        )
        assert (
            extract_region_from_url("https://x.lambda-url.eu-west-1.on.aws/")
            == "eu-west-1"
        )


class TestProviderForClient:
    """The integration's MCPClient -> provider_id map is what
    `OAuthConsentHook.provider_lookup` consults."""

    def test_unknown_client_returns_none(self):
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        assert integration.provider_for_client(FakeClient()) is None

    def test_records_and_resolves_provider_for_client(self):
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        client = FakeClient()
        # Simulate what `load_external_tools` does after creating an
        # OAuth-gated MCP client.
        integration._provider_for_client_id[id(client)] = "google-workspace"

        assert integration.provider_for_client(client) == "google-workspace"

    def test_clear_user_clients_drops_provider_mapping(self):
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        client = FakeClient()
        integration.clients["alice:gmail"] = client
        integration._provider_for_client_id[id(client)] = "google-workspace"

        integration.clear_user_clients("alice")

        assert "alice:gmail" not in integration.clients
        assert integration.provider_for_client(client) is None


class TestClearToolClients:
    """Admin updates to a tool must invalidate cached clients for that
    tool so the next agent build reconnects with the updated config."""

    def test_clears_non_oauth_tool_and_keeps_other_tools(self):
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        gmail = FakeClient()
        jira = FakeClient()
        integration.clients["gmail"] = gmail
        integration.clients["jira"] = jira

        integration.clear_tool_clients("gmail")

        assert "gmail" not in integration.clients
        assert integration.clients["jira"] is jira

    def test_clears_all_user_scoped_keys_for_tool(self):
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        alice_gmail = FakeClient()
        bob_gmail = FakeClient()
        alice_jira = FakeClient()
        integration.clients["alice:gmail"] = alice_gmail
        integration.clients["bob:gmail"] = bob_gmail
        integration.clients["alice:jira"] = alice_jira
        integration._provider_for_client_id[id(alice_gmail)] = "google-workspace"
        integration._provider_for_client_id[id(bob_gmail)] = "google-workspace"

        integration.clear_tool_clients("gmail")

        assert "alice:gmail" not in integration.clients
        assert "bob:gmail" not in integration.clients
        assert integration.clients["alice:jira"] is alice_jira
        assert integration.provider_for_client(alice_gmail) is None
        assert integration.provider_for_client(bob_gmail) is None

    def test_does_not_match_tool_id_as_key_suffix_without_colon(self):
        """Guard against substring false positives: a tool named "gmail"
        must not clear a tool named "super-gmail"."""
        integration = ExternalMCPIntegration()

        class FakeClient:
            pass

        super_gmail = FakeClient()
        integration.clients["super-gmail"] = super_gmail

        integration.clear_tool_clients("gmail")

        assert integration.clients["super-gmail"] is super_gmail

    def test_clears_tool_subset_keys(self):
        """Scoped and admin-listed clients carry an `|allow:` suffix."""
        integration = ExternalMCPIntegration()
        scoped = object()
        user_scoped = object()
        integration.clients["gmail|allow:send"] = scoped
        integration.clients["alice:gmail|allow:search,send"] = user_scoped

        integration.clear_tool_clients("gmail")

        assert integration.clients == {}

    def test_no_op_when_tool_not_cached(self):
        integration = ExternalMCPIntegration()
        integration.clear_tool_clients("never-loaded")
        assert integration.clients == {}


def _fake_tool(updated_at, tool_id="gmail"):
    """Minimal tool stand-in for load_external_tools."""
    return SimpleNamespace(
        tool_id=tool_id,
        protocol="mcp_external",
        mcp_config=SimpleNamespace(
            server_url="https://example.com/mcp",
            approval_required_names=lambda: set(),
        ),
        forward_auth_token=False,
        requires_oauth_provider=None,
        updated_at=updated_at,
    )


class TestLoadExternalToolsVersioning:
    """`load_external_tools` must rebuild the MCPClient when the tool's
    `updated_at` changes. Without this, admin edits to MCP config (URL,
    auth mode, etc.) never take effect for the process lifetime."""

    @pytest.mark.asyncio
    async def test_reuses_client_when_updated_at_unchanged(self):
        integration = ExternalMCPIntegration()
        tool = _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))

        client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            return_value=client,
        ) as create_mock:
            first = await integration.load_external_tools(["gmail"])
            second = await integration.load_external_tools(["gmail"])

        assert first == second
        assert create_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_rebuilds_client_when_updated_at_changes(self):
        integration = ExternalMCPIntegration()
        old = _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        new = _fake_tool(datetime(2025, 2, 1, tzinfo=timezone.utc))

        repo = SimpleNamespace(get_tool=AsyncMock(side_effect=[old, new]))

        client_old = SimpleNamespace(load_tools=AsyncMock(return_value=[]))
        client_new = SimpleNamespace(load_tools=AsyncMock(return_value=[]))

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            side_effect=[client_old, client_new],
        ):
            first = await integration.load_external_tools(["gmail"])
            second = await integration.load_external_tools(["gmail"])

        assert first == [client_old]
        assert second == [client_new]
        assert integration.clients["gmail"] is client_new
        # Old client must be evicted, not left dangling under the same key.
        assert client_old not in integration.clients.values()


class TestLoadExternalToolsPreflight:
    """A single unreachable MCP server must not fail the whole turn —
    `load_external_tools` pre-flights each new client and silently drops
    the ones whose session can't be opened."""

    @pytest.mark.asyncio
    async def test_skips_client_when_preflight_fails(self):
        integration = ExternalMCPIntegration()
        tool = _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))

        bad_client = SimpleNamespace(
            load_tools=AsyncMock(side_effect=RuntimeError("connection refused"))
        )

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            return_value=bad_client,
        ):
            result = await integration.load_external_tools(["gmail"])

        assert result == []
        # Failed clients must not be cached — otherwise we'd serve a
        # broken client back on subsequent turns.
        assert "gmail" not in integration.clients

    @pytest.mark.asyncio
    async def test_one_failing_client_does_not_block_others(self):
        integration = ExternalMCPIntegration()
        bad_tool = _fake_tool(
            datetime(2025, 1, 1, tzinfo=timezone.utc), tool_id="calendar"
        )
        good_tool = _fake_tool(
            datetime(2025, 1, 1, tzinfo=timezone.utc), tool_id="gmail"
        )
        repo = SimpleNamespace(
            get_tool=AsyncMock(side_effect=[bad_tool, good_tool])
        )

        bad_client = SimpleNamespace(
            load_tools=AsyncMock(side_effect=RuntimeError("connection refused"))
        )
        good_client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            side_effect=[bad_client, good_client],
        ):
            result = await integration.load_external_tools(["calendar", "gmail"])

        assert result == [good_client]
        assert integration.clients == {"gmail": good_client}


class TestLoadExternalToolsScoped:
    """Per-tool enablement: a scoped id (`base::tool`) restricts the client to
    the selected tool names; a bare id exposes the whole server."""

    @pytest.mark.asyncio
    async def test_scoped_ids_collapse_to_one_filtered_client(self):
        integration = ExternalMCPIntegration()
        tool = _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))
        client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            return_value=client,
        ) as create_mock:
            await integration.load_external_tools(["gmail::send", "gmail::search"])

        # Two scoped ids for one server build a single client, restricted to
        # the selected tool names.
        assert create_mock.call_count == 1
        assert create_mock.call_args.kwargs["allowed_tool_names"] == {"send", "search"}

    @pytest.mark.asyncio
    async def test_bare_id_exposes_whole_server(self):
        integration = ExternalMCPIntegration()
        tool = _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))
        client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            return_value=client,
        ) as create_mock:
            await integration.load_external_tools(["gmail"])

        assert create_mock.call_args.kwargs["allowed_tool_names"] is None

    @pytest.mark.asyncio
    async def test_different_subsets_are_distinct_cached_clients(self):
        integration = ExternalMCPIntegration()
        tool = _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))
        client_a = SimpleNamespace(load_tools=AsyncMock(return_value=[]))
        client_b = SimpleNamespace(load_tools=AsyncMock(return_value=[]))

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            side_effect=[client_a, client_b],
        ):
            first = await integration.load_external_tools(["gmail::send"])
            second = await integration.load_external_tools(["gmail::search"])

        # A different selected-tool subset must not reuse the other's client.
        assert first == [client_a]
        assert second == [client_b]
        assert len(integration.clients) == 2


def _listed_tool(updated_at, names, tool_id="zoom", status=None):
    """A catalog row whose admin "Available Tools" list names ``names``."""
    tool = _fake_tool(updated_at, tool_id=tool_id)
    tool.mcp_config.tools = [SimpleNamespace(name=n) for n in names]
    if status is not None:
        tool.status = status
    return tool


async def _load(integration, ids, tool):
    repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))
    client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))
    with patch(
        "apis.shared.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ), patch(
        "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
        return_value=client,
    ) as create_mock:
        result = await integration.load_external_tools(ids)
    return result, create_mock


class TestAdminToolListIsAnAllowlist:
    """The admin tool form's "Available Tools" list says which of a server's
    tools reach the model ("Leave empty to discover tools at runtime"). It was
    only ever read for approval flags, so a server limited to nine tools still
    registered all of them (dev, 2026-10-17)."""

    @pytest.mark.asyncio
    async def test_whole_server_is_limited_to_the_listed_tools(self):
        tool = _listed_tool(datetime(2025, 1, 1, tzinfo=timezone.utc), ["search", "send"])

        _, create_mock = await _load(ExternalMCPIntegration(), ["zoom"], tool)

        assert create_mock.call_args.kwargs["allowed_tool_names"] == {"search", "send"}

    @pytest.mark.asyncio
    async def test_a_scoped_selection_cannot_reach_past_the_list(self):
        tool = _listed_tool(datetime(2025, 1, 1, tzinfo=timezone.utc), ["search", "send"])

        _, create_mock = await _load(
            ExternalMCPIntegration(), ["zoom::send", "zoom::get_file_content"], tool
        )

        assert create_mock.call_args.kwargs["allowed_tool_names"] == {"send"}

    @pytest.mark.asyncio
    async def test_a_selection_with_nothing_listed_loads_no_client(self):
        tool = _listed_tool(datetime(2025, 1, 1, tzinfo=timezone.utc), ["search"])

        result, create_mock = await _load(
            ExternalMCPIntegration(), ["zoom::get_file_content"], tool
        )

        assert result == []
        create_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_empty_list_still_means_discover_everything(self):
        tool = _listed_tool(datetime(2025, 1, 1, tzinfo=timezone.utc), [])

        _, create_mock = await _load(ExternalMCPIntegration(), ["zoom"], tool)

        assert create_mock.call_args.kwargs["allowed_tool_names"] is None

    @pytest.mark.asyncio
    async def test_a_listed_server_still_resolves_by_its_bare_id(self):
        """The allowlist puts an `|allow:` suffix on a whole-server key, which
        `get_client` (OAuth and approval lookups) must still find."""
        tool = _listed_tool(datetime(2025, 1, 1, tzinfo=timezone.utc), ["search"])
        integration = ExternalMCPIntegration()

        result, _ = await _load(integration, ["zoom"], tool)

        assert integration.get_client("zoom") is result[0]


class TestDisabledToolsNeverLoad:
    """Status "disabled" takes a server out of service even for users whose
    toggle is still on. "deprecated" (being retired) keeps working for them,
    as docs/specs/mcp-server-retirement.md §7 requires."""

    @pytest.mark.asyncio
    async def test_a_disabled_server_is_skipped(self):
        tool = _listed_tool(datetime(2025, 1, 1, tzinfo=timezone.utc), [], status="disabled")
        timings: list = []
        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
        ) as create_mock:
            result = await integration.load_external_tools(["zoom"], timings=timings)

        assert result == []
        create_mock.assert_not_called()
        assert timings[0]["outcome"] == "disabled"

    @pytest.mark.asyncio
    async def test_the_enum_value_is_read_the_same_as_the_string(self):
        from apis.shared.tools.models import ToolStatus

        tool = _listed_tool(
            datetime(2025, 1, 1, tzinfo=timezone.utc), [], status=ToolStatus.DISABLED
        )

        result, create_mock = await _load(ExternalMCPIntegration(), ["zoom"], tool)

        assert result == []
        create_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_cached_client_is_not_served_once_disabled(self):
        integration = ExternalMCPIntegration()
        active = _listed_tool(datetime(2025, 1, 1, tzinfo=timezone.utc), [])
        first, _ = await _load(integration, ["zoom"], active)
        assert len(first) == 1

        disabled = _listed_tool(
            datetime(2025, 1, 1, tzinfo=timezone.utc), [], status="disabled"
        )
        second, _ = await _load(integration, ["zoom"], disabled)

        assert second == []

    @pytest.mark.asyncio
    async def test_a_deprecated_server_still_loads(self):
        tool = _listed_tool(datetime(2025, 1, 1, tzinfo=timezone.utc), [], status="deprecated")

        result, _ = await _load(ExternalMCPIntegration(), ["zoom"], tool)

        assert len(result) == 1


class TestGetClientResolvesScopedBindings:
    """`get_client` must map a (possibly-scoped) catalog id back to the client
    `load_external_tools` cached, including the per-tool `|allow:` cache-key
    suffix a subset binding produces. Regression: a skill binding a *subset*
    of an external MCP server (scoped ids like `canvas::courses`) resolved to
    no client at fold time, so the skill folded zero tools and the model
    reported the server "not connected"."""

    @staticmethod
    def _oauth_tool(tool_id="canvas"):
        return SimpleNamespace(
            tool_id=tool_id,
            protocol="mcp_external",
            mcp_config=SimpleNamespace(
                server_url="https://example.com/mcp",
                approval_required_names=lambda: set(),
            ),
            forward_auth_token=False,
            requires_oauth_provider="canvas-oauth",
            updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )

    @pytest.mark.asyncio
    async def test_resolves_scoped_oauth_binding_round_trip(self):
        integration = ExternalMCPIntegration()
        client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=self._oauth_tool()))

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            return_value=client,
        ):
            await integration.load_external_tools(
                ["canvas::courses", "canvas::submissions"], user_id="alice"
            )

        # Cached under "alice:canvas|allow:courses,submissions"; the skill folds
        # by the scoped id (or the base) — every form must find that client.
        assert integration.get_client("canvas::courses", "alice") is client
        assert integration.get_client("canvas::submissions", "alice") is client
        assert integration.get_client("canvas", "alice") is client

    @pytest.mark.asyncio
    async def test_whole_server_binding_still_resolves(self):
        integration = ExternalMCPIntegration()
        client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=self._oauth_tool()))

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            return_value=client,
        ):
            await integration.load_external_tools(["canvas"], user_id="alice")

        # Exact whole-server key resolves, and a scoped lookup against it does too.
        assert integration.get_client("canvas", "alice") is client
        assert integration.get_client("canvas::courses", "alice") is client

    def test_missing_returns_none(self):
        integration = ExternalMCPIntegration()
        assert integration.get_client("canvas::courses", "alice") is None
        assert integration.get_client("canvas") is None


def _status_code_error(status: int) -> Exception:
    """An exception carrying the status directly, with no httpx `.response`."""
    exc = RuntimeError("request rejected")
    exc.status_code = status
    return exc


class TestIsAuthFailure:
    """Only a refusal to authorize can be fixed by consenting. Everything
    else — server down, host doesn't resolve, request timed out — must not be
    read as a consent gap, or an unconsented user is asked to connect a
    server that isn't there, on every turn, forever."""

    def test_wrapped_401_is_an_auth_failure(self):
        """The status lives three wrappers down, in an ExceptionGroup, and
        never appears in the outermost message."""
        exc = _wrapped_preflight_failure(_http_status_error(401))

        assert "401" not in str(exc)
        assert _is_auth_failure(exc) is True

    def test_wrapped_403_is_an_auth_failure(self):
        assert _is_auth_failure(_wrapped_preflight_failure(_http_status_error(403)))

    def test_wrapped_connect_error_is_not_an_auth_failure(self):
        """The dev `canvas_faculty` case: `serverUrl` points at a localhost
        port that the AgentCore Runtime container cannot reach."""
        exc = _wrapped_preflight_failure(
            httpx.ConnectError("All connection attempts failed")
        )

        assert _is_auth_failure(exc) is False

    def test_wrapped_timeout_is_not_an_auth_failure(self):
        exc = _wrapped_preflight_failure(httpx.ConnectTimeout("timed out"))

        assert _is_auth_failure(exc) is False

    def test_dns_failure_is_not_an_auth_failure(self):
        exc = _wrapped_preflight_failure(
            httpx.ConnectError("[Errno 8] nodename nor servname provided")
        )

        assert _is_auth_failure(exc) is False

    def test_server_error_is_not_an_auth_failure(self):
        """A 500 is the server's problem, not the user's authorization."""
        exc = _wrapped_preflight_failure(_http_status_error(500))

        assert _is_auth_failure(exc) is False

    def test_plain_status_code_attribute_is_an_auth_failure(self):
        """Not every client wraps the response; some carry the status directly."""
        assert _is_auth_failure(_status_code_error(403)) is True

    def test_message_only_unauthorized_is_an_auth_failure(self):
        """A server that reports the refusal as protocol text rather than an
        HTTP error still gets the user a consent prompt."""
        assert _is_auth_failure(RuntimeError("Unauthorized: missing bearer token"))

    def test_digits_inside_a_url_do_not_read_as_a_status(self):
        """The text fallback must not fire on a port or path that merely
        contains the digits — that would resurrect the bug it guards."""
        assert not _is_auth_failure(
            httpx.ConnectError("connection to localhost:8403 failed")
        )
        assert not _is_auth_failure(RuntimeError("cannot reach https://x/v1/4010/mcp"))

    def test_self_referential_chain_terminates(self):
        """`__context__` cycles are possible when exceptions are re-raised;
        the walk must not spin."""
        first = RuntimeError("boom")
        second = RuntimeError("bang")
        first.__context__ = second
        second.__context__ = first

        assert _is_auth_failure(first) is False


class TestOAuthPreflightRecovery:
    """An OAuth-gated MCP server that requires auth even for `tools/list`
    (GitHub's does) 401s whenever the in-process token cache is cold — which
    it always is on a fresh microVM.

    Before `_recover_oauth_preflight` the tool was dropped, and since
    `OAuthConsentHook` only runs `BeforeToolCall` for *registered* tools,
    nothing ever warmed the cache: the tool stayed missing for the life of
    the process even for a user whose token sat in the AgentCore vault the
    whole time, and the user was never told why.
    """

    PROVIDER = "github-oauth"

    @staticmethod
    def _oauth_tool(tool_id="github_issues"):
        return SimpleNamespace(
            tool_id=tool_id,
            protocol="mcp_external",
            mcp_config=SimpleNamespace(
                server_url="https://api.githubcopilot.com/mcp/x/issues",
                approval_required_names=lambda: set(),
            ),
            forward_auth_token=False,
            requires_oauth_provider=TestOAuthPreflightRecovery.PROVIDER,
            updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )

    @staticmethod
    def _patches(client, repo, resolved):
        """Common patch stack; `resolved` is the vault's answer."""
        return (
            patch(
                "apis.shared.tools.repository.get_tool_catalog_repository",
                return_value=repo,
            ),
            patch(
                "agents.main_agent.integrations.external_mcp_client."
                "create_external_mcp_client",
                return_value=client,
            ),
            patch(
                "apis.shared.oauth.token_resolution.resolve_token_or_consent_url",
                AsyncMock(return_value=resolved),
            ),
        )

    @pytest.mark.asyncio
    async def test_warms_vault_token_and_registers_tool(self):
        """Consented user: the vault has a token, so the retry succeeds and
        the tool comes back instead of vanishing for the process lifetime."""
        from agents.main_agent.integrations import oauth_token_cache

        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=self._oauth_tool()))
        # Fails cold, succeeds once a token is in the cache.
        client = SimpleNamespace(
            load_tools=AsyncMock(side_effect=[RuntimeError("401 Unauthorized"), []])
        )
        resolved = {"token": "vault-token", "url": None}

        oauth_token_cache.clear_user_provider("alice", self.PROVIDER)
        p1, p2, p3 = self._patches(client, repo, resolved)
        try:
            with p1, p2, p3:
                result = await integration.load_external_tools(
                    ["github_issues"], user_id="alice"
                )

            assert result == [client]
            assert client.load_tools.await_count == 2
            # Cache warmed so the client's lazy token provider can use it.
            assert oauth_token_cache.get("alice", self.PROVIDER) == "vault-token"
            # Nothing to prompt for — the user is already connected.
            assert integration.take_pending_consents("alice") == {}
        finally:
            oauth_token_cache.clear_user_provider("alice", self.PROVIDER)

    @pytest.mark.asyncio
    async def test_records_pending_consent_when_vault_returns_url(self):
        """Unconsented user: AgentCore hands back an authorization URL, so
        the drop is recorded for the turn to surface as `oauth_required`."""
        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=self._oauth_tool()))
        client = SimpleNamespace(
            load_tools=AsyncMock(side_effect=RuntimeError("401 Unauthorized"))
        )
        resolved = {"token": None, "url": "https://consent.example/authorize"}

        p1, p2, p3 = self._patches(client, repo, resolved)
        with p1, p2, p3:
            result = await integration.load_external_tools(
                ["github_issues"], user_id="alice"
            )

        assert result == []
        assert "github_issues" not in integration.clients
        assert integration.take_pending_consents("alice") == {
            self.PROVIDER: "https://consent.example/authorize"
        }

    @pytest.mark.asyncio
    async def test_no_prompt_when_vault_cannot_be_reached(self):
        """A hard error is "couldn't ask", NOT "user must consent". Prompting
        here would nag a connected user every turn the vault is unhappy."""
        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=self._oauth_tool()))
        # 401 so the pre-flight really does reach the vault — the vault
        # itself is what fails here.
        client = SimpleNamespace(
            load_tools=AsyncMock(side_effect=RuntimeError("401 Unauthorized"))
        )

        p1, p2, p3 = self._patches(client, repo, None)
        with p1, p2, p3:
            result = await integration.load_external_tools(
                ["github_issues"], user_id="alice"
            )

        assert result == []
        assert integration.take_pending_consents("alice") == {}

    @pytest.mark.asyncio
    async def test_unreachable_server_never_prompts_for_consent(self):
        """The dev `canvas_faculty` regression: an OAuth-gated server whose
        URL the runtime cannot reach failed pre-flight with a ConnectError,
        the vault correctly answered "no token, here is an authorization
        URL", and the user was shown a connect prompt on every turn that
        completing consent could never satisfy — the server simply is not
        there. Classify before asking."""
        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(
            get_tool=AsyncMock(return_value=self._oauth_tool(tool_id="canvas_faculty"))
        )
        client = SimpleNamespace(
            load_tools=AsyncMock(
                side_effect=_wrapped_preflight_failure(
                    httpx.ConnectError("All connection attempts failed")
                )
            )
        )
        resolve_mock = AsyncMock(
            return_value={"token": None, "url": "https://consent.example/authorize"}
        )

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client."
            "create_external_mcp_client",
            return_value=client,
        ), patch(
            "apis.shared.oauth.token_resolution.resolve_token_or_consent_url",
            resolve_mock,
        ):
            result = await integration.load_external_tools(
                ["canvas_faculty"], user_id="alice"
            )

        assert result == []
        # Never even asked — the vault would have said "not consented" and
        # that answer is meaningless for a server that isn't listening.
        resolve_mock.assert_not_awaited()
        assert integration.take_pending_consents("alice") == {}

    @pytest.mark.asyncio
    async def test_wrapped_401_still_records_consent(self):
        """The real production shape, not a bare `RuntimeError`: a 401 buried
        under Strands' two wrappers and an anyio ExceptionGroup must still
        reach the vault, or classifying would undo the recovery it guards."""
        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=self._oauth_tool()))
        client = SimpleNamespace(
            load_tools=AsyncMock(
                side_effect=_wrapped_preflight_failure(_http_status_error(401))
            )
        )
        resolved = {"token": None, "url": "https://consent.example/authorize"}

        p1, p2, p3 = self._patches(client, repo, resolved)
        with p1, p2, p3:
            result = await integration.load_external_tools(
                ["github_issues"], user_id="alice"
            )

        assert result == []
        assert integration.take_pending_consents("alice") == {
            self.PROVIDER: "https://consent.example/authorize"
        }

    @pytest.mark.asyncio
    async def test_skips_vault_when_cached_token_already_present(self):
        """A warm token that still failed is an expiry case owned by the
        hook's AfterToolCall 401 handler — re-asking the vault without
        force_authentication would just return the same token."""
        from agents.main_agent.integrations import oauth_token_cache

        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=self._oauth_tool()))
        client = SimpleNamespace(
            load_tools=AsyncMock(side_effect=RuntimeError("500 Server Error"))
        )
        resolve_mock = AsyncMock(return_value={"token": "t", "url": None})

        oauth_token_cache.set("alice", self.PROVIDER, "already-warm")
        try:
            with patch(
                "apis.shared.tools.repository.get_tool_catalog_repository",
                return_value=repo,
            ), patch(
                "agents.main_agent.integrations.external_mcp_client."
                "create_external_mcp_client",
                return_value=client,
            ), patch(
                "apis.shared.oauth.token_resolution.resolve_token_or_consent_url",
                resolve_mock,
            ):
                result = await integration.load_external_tools(
                    ["github_issues"], user_id="alice"
                )

            assert result == []
            resolve_mock.assert_not_awaited()
            assert integration.take_pending_consents("alice") == {}
        finally:
            oauth_token_cache.clear_user_provider("alice", self.PROVIDER)

    @pytest.mark.asyncio
    async def test_non_oauth_tool_failure_never_consults_the_vault(self):
        """Unchanged behaviour for a plain unreachable server."""
        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(
            get_tool=AsyncMock(
                return_value=_fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
            )
        )
        client = SimpleNamespace(
            load_tools=AsyncMock(side_effect=RuntimeError("connection refused"))
        )
        resolve_mock = AsyncMock()

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client."
            "create_external_mcp_client",
            return_value=client,
        ), patch(
            "apis.shared.oauth.token_resolution.resolve_token_or_consent_url",
            resolve_mock,
        ):
            result = await integration.load_external_tools(["gmail"], user_id="alice")

        assert result == []
        resolve_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retries_with_a_token_a_parallel_preflight_warmed(self):
        """Prod, 2026-10-16: `github_orgs` and `github_repos` pre-flighted in
        parallel on a cold cache. Repos asked the vault and warmed the token
        while orgs was still failing; orgs then saw a cached token, took it
        for an expired one, and was dropped for a connected user. A token
        this pre-flight never tried is a reason to retry, not to give up."""
        from agents.main_agent.integrations import oauth_token_cache

        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=self._oauth_tool()))

        async def _load_tools():
            if client.load_tools.await_count == 1:
                # The sibling's vault answer lands while this one is failing.
                oauth_token_cache.set("alice", self.PROVIDER, "sibling-token")
                raise _wrapped_preflight_failure(_http_status_error(401))
            return []

        client = SimpleNamespace(load_tools=AsyncMock(side_effect=_load_tools))
        resolve_mock = AsyncMock(return_value={"token": None, "url": "https://x"})

        oauth_token_cache.clear_user_provider("alice", self.PROVIDER)
        try:
            with patch(
                "apis.shared.tools.repository.get_tool_catalog_repository",
                return_value=repo,
            ), patch(
                "agents.main_agent.integrations.external_mcp_client."
                "create_external_mcp_client",
                return_value=client,
            ), patch(
                "apis.shared.oauth.token_resolution.resolve_token_or_consent_url",
                resolve_mock,
            ):
                result = await integration.load_external_tools(
                    ["github_issues"], user_id="alice"
                )

            assert result == [client]
            assert client.load_tools.await_count == 2
            resolve_mock.assert_not_awaited()
            assert integration.take_pending_consents("alice") == {}
        finally:
            oauth_token_cache.clear_user_provider("alice", self.PROVIDER)

    @pytest.mark.asyncio
    async def test_a_consent_drop_is_reported_to_the_build(self):
        """The build needs to know which servers are missing for consent, so it
        can tell the model and re-check on a cache hit."""
        tool = self._oauth_tool()
        tool.display_name = "Github Issues"
        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))
        client = SimpleNamespace(
            load_tools=AsyncMock(
                side_effect=_wrapped_preflight_failure(_http_status_error(401))
            )
        )
        resolved = {"token": None, "url": "https://consent.example/authorize"}
        gated: list = []
        timings: list = []

        p1, p2, p3 = self._patches(client, repo, resolved)
        with p1, p2, p3:
            await integration.load_external_tools(
                ["github_issues"], user_id="alice", timings=timings, consent_gated=gated
            )

        assert gated == [
            ConsentGatedTool(
                tool_id="github_issues",
                display_name="Github Issues",
                provider_id=self.PROVIDER,
            )
        ]
        assert timings[0]["outcome"] == "consent"

    @pytest.mark.asyncio
    async def test_other_drops_are_not_reported_as_consent(self):
        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=self._oauth_tool()))
        client = SimpleNamespace(
            load_tools=AsyncMock(
                side_effect=_wrapped_preflight_failure(
                    httpx.ConnectError("All connection attempts failed")
                )
            )
        )
        gated: list = []

        p1, p2, p3 = self._patches(client, repo, {"token": None, "url": "https://x"})
        with p1, p2, p3:
            await integration.load_external_tools(
                ["github_issues"], user_id="alice", consent_gated=gated
            )

        assert gated == []

    @pytest.mark.asyncio
    async def test_consent_drops_are_sorted_whatever_order_they_finish_in(
        self, monkeypatch
    ):
        """Parallel pre-flights finish in any order; the note built from this
        list is prefix bytes."""
        monkeypatch.setenv("MCP_PARALLEL_PREFLIGHT_ENABLED", "true")
        tools = {}
        for tool_id, name in (("github_repos", "Github Repos"), ("github_issues", "Github Issues")):
            tool = self._oauth_tool(tool_id=tool_id)
            tool.display_name = name
            tools[tool_id] = tool
        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(side_effect=lambda tid: tools[tid]))
        client = SimpleNamespace(
            load_tools=AsyncMock(
                side_effect=_wrapped_preflight_failure(_http_status_error(401))
            )
        )
        gated: list = []

        p1, p2, p3 = self._patches(client, repo, {"token": None, "url": "https://x"})
        with p1, p2, p3:
            await integration.load_external_tools(
                ["github_repos", "github_issues"], user_id="alice", consent_gated=gated
            )

        assert [g.display_name for g in gated] == ["Github Issues", "Github Repos"]

    def test_take_pending_consents_drains_and_is_per_user(self):
        """Draining stops a stale prompt re-firing on an agent-cache hit,
        and one user's consent must never leak into another's stream."""
        integration = ExternalMCPIntegration()
        integration._pending_consents = {
            "alice": {self.PROVIDER: "https://consent.example/a"},
            "bob": {self.PROVIDER: "https://consent.example/b"},
        }

        assert integration.take_pending_consents("alice") == {
            self.PROVIDER: "https://consent.example/a"
        }
        assert integration.take_pending_consents("alice") == {}
        # Bob's entry is untouched by Alice's drain.
        assert integration.take_pending_consents("bob") == {
            self.PROVIDER: "https://consent.example/b"
        }
        assert integration.take_pending_consents("carol") == {}


class TestRecheckConsent:
    """A cache hit runs no pre-flight, so an agent built before the user
    connected would otherwise serve them without the tools until it was
    evicted. `recheck_consent` is the question the hit asks instead."""

    PROVIDER = "github"

    @staticmethod
    def _resolve(answers):
        return patch(
            "apis.shared.oauth.token_resolution.resolve_token_or_consent_url",
            AsyncMock(side_effect=lambda provider, user: answers[provider]),
        )

    @pytest.mark.asyncio
    async def test_a_token_warms_the_cache_and_asks_for_a_rebuild(self):
        from agents.main_agent.integrations import oauth_token_cache

        integration = ExternalMCPIntegration()
        oauth_token_cache.clear_user_provider("alice", self.PROVIDER)
        try:
            with self._resolve({self.PROVIDER: {"token": "now-connected", "url": None}}):
                assert await integration.recheck_consent("alice", [self.PROVIDER]) is True
            assert oauth_token_cache.get("alice", self.PROVIDER) == "now-connected"
            assert integration.take_pending_consents("alice") == {}
        finally:
            oauth_token_cache.clear_user_provider("alice", self.PROVIDER)

    @pytest.mark.asyncio
    async def test_still_unconnected_re_records_the_prompt(self):
        """So the turn served from the cache emits `oauth_required` again."""
        integration = ExternalMCPIntegration()
        with self._resolve({self.PROVIDER: {"token": None, "url": "https://consent.example/a"}}):
            assert await integration.recheck_consent("alice", [self.PROVIDER]) is False
        assert integration.take_pending_consents("alice") == {
            self.PROVIDER: "https://consent.example/a"
        }

    @pytest.mark.asyncio
    async def test_couldnt_ask_changes_nothing(self):
        integration = ExternalMCPIntegration()
        with self._resolve({self.PROVIDER: None}):
            assert await integration.recheck_consent("alice", [self.PROVIDER]) is False
        assert integration.take_pending_consents("alice") == {}

    @pytest.mark.asyncio
    async def test_asks_once_per_provider(self):
        """Three GitHub servers share one connection: one round trip."""
        integration = ExternalMCPIntegration()
        resolve = AsyncMock(return_value={"token": None, "url": "https://x"})
        with patch(
            "apis.shared.oauth.token_resolution.resolve_token_or_consent_url", resolve
        ):
            await integration.recheck_consent("alice", [self.PROVIDER] * 3)
        assert resolve.await_count == 1


class TestLoadExternalToolsTimings:
    """Per-server timings behind `agent_build.tools.mcp` (turn-path P1a): one
    entry per server so one slow server is distinguishable from many."""

    @pytest.mark.asyncio
    async def test_one_entry_per_server_with_its_outcome(self):
        integration = ExternalMCPIntegration()
        when = datetime(2025, 1, 1, tzinfo=timezone.utc)
        repo = SimpleNamespace(
            get_tool=AsyncMock(
                side_effect=[
                    _fake_tool(when, tool_id="calendar"),
                    _fake_tool(when, tool_id="gmail"),
                    None,
                ]
            )
        )
        bad_client = SimpleNamespace(
            load_tools=AsyncMock(side_effect=RuntimeError("connection refused"))
        )
        good_client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))
        timings: list = []

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            side_effect=[bad_client, good_client],
        ):
            await integration.load_external_tools(
                ["calendar", "gmail", "retired"], timings=timings
            )

        assert [(t["id"], t["outcome"]) for t in timings] == [
            ("calendar", "dropped"),
            ("gmail", "loaded"),
            ("retired", "skipped"),
        ]
        for entry in timings[:2]:
            assert {"catalogMs", "preflightMs", "totalMs"} <= set(entry)
            assert entry["totalMs"] >= entry["preflightMs"]
        # Never reached a pre-flight, so it must not claim one.
        assert "preflightMs" not in timings[2]

    @pytest.mark.asyncio
    async def test_a_cached_client_reports_no_preflight(self):
        integration = ExternalMCPIntegration()
        tool = _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc))
        repo = SimpleNamespace(get_tool=AsyncMock(return_value=tool))
        client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))
        timings: list = []

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            return_value=client,
        ):
            await integration.load_external_tools(["gmail"])
            await integration.load_external_tools(["gmail"], timings=timings)

        assert timings[0]["outcome"] == "cached"
        assert "preflightMs" not in timings[0]

    @pytest.mark.asyncio
    async def test_a_catalog_error_is_recorded_not_raised(self):
        integration = ExternalMCPIntegration()
        repo = SimpleNamespace(get_tool=AsyncMock(side_effect=RuntimeError("ddb")))
        timings: list = []

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ):
            result = await integration.load_external_tools(["gmail"], timings=timings)

        assert result == []
        assert timings[0]["outcome"] == "error"
        assert "totalMs" in timings[0]


class TestLoadExternalToolsConcurrentPreflight:
    """Several servers pre-flight together (turn-path spec §5 P3).

    Strands' `load_tools` blocks its thread for the whole `initialize`
    handshake, so the fakes here block synchronously too — an `await
    asyncio.sleep` fake would overlap under `gather` and prove nothing. On dev
    two cold Lambda-URL servers cost 9.5s + 12s in series. What must hold:
    the pre-flights overlap, the client list (and so the tool order in the
    cacheable `toolConfig`) stays in catalog order, a lone server keeps the
    inline call, and `MCP_PARALLEL_PREFLIGHT_ENABLED=false` is the old loop.
    """

    @staticmethod
    def _repo(*tool_ids):
        tools = {
            tid: _fake_tool(datetime(2025, 1, 1, tzinfo=timezone.utc), tool_id=tid)
            for tid in tool_ids
        }
        return SimpleNamespace(get_tool=AsyncMock(side_effect=lambda tid: tools[tid]))

    @staticmethod
    def _blocking_client(on_load):
        async def _load_tools():
            on_load()
            return []

        return SimpleNamespace(load_tools=_load_tools)

    async def _load(self, integration, tool_ids, clients_by_id, **kwargs):
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=self._repo(*tool_ids),
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            side_effect=lambda config, tool_definition, **kw: clients_by_id[tool_definition.tool_id],
        ):
            return await integration.load_external_tools(tool_ids, **kwargs)

    @pytest.mark.asyncio
    async def test_preflights_overlap(self, monkeypatch):
        import threading

        monkeypatch.delenv("MCP_PARALLEL_PREFLIGHT_ENABLED", raising=False)
        # Each pre-flight blocks until BOTH are in flight: serial loading
        # would break the barrier instead of passing it.
        barrier = threading.Barrier(2, timeout=5)
        clients = {
            "canvas": self._blocking_client(barrier.wait),
            "class_search": self._blocking_client(barrier.wait),
        }

        result = await self._load(ExternalMCPIntegration(), ["canvas", "class_search"], clients)

        assert result == [clients["canvas"], clients["class_search"]]

    @pytest.mark.asyncio
    async def test_catalog_order_survives_a_slower_first_server(self, monkeypatch):
        import threading
        import time as _time

        monkeypatch.delenv("MCP_PARALLEL_PREFLIGHT_ENABLED", raising=False)
        finished = []
        lock = threading.Lock()

        def _after(name, delay):
            def _run():
                _time.sleep(delay)
                with lock:
                    finished.append(name)
            return _run

        clients = {
            "slow": self._blocking_client(_after("slow", 0.2)),
            "fast": self._blocking_client(_after("fast", 0.0)),
        }
        timings: list = []

        result = await self._load(
            ExternalMCPIntegration(), ["slow", "fast"], clients, timings=timings
        )

        assert finished == ["fast", "slow"]
        assert result == [clients["slow"], clients["fast"]]
        assert [t["id"] for t in timings] == ["slow", "fast"]
        assert all(t["outcome"] == "loaded" for t in timings)

    @pytest.mark.asyncio
    async def test_a_lone_server_preflights_on_the_calling_thread(self, monkeypatch):
        import threading

        monkeypatch.delenv("MCP_PARALLEL_PREFLIGHT_ENABLED", raising=False)
        seen = []
        clients = {"canvas": self._blocking_client(lambda: seen.append(threading.current_thread()))}

        await self._load(ExternalMCPIntegration(), ["canvas"], clients)

        assert seen == [threading.current_thread()]

    @pytest.mark.asyncio
    async def test_context_reaches_the_preflight_thread(self, monkeypatch):
        """`BedrockAgentCoreContext` (workload token, OAuth callback URL) is
        contextvar-backed; the pre-flight must see what the build set."""
        import contextvars

        monkeypatch.delenv("MCP_PARALLEL_PREFLIGHT_ENABLED", raising=False)
        marker = contextvars.ContextVar("marker", default=None)
        seen = []
        clients = {
            "a": self._blocking_client(lambda: seen.append(marker.get())),
            "b": self._blocking_client(lambda: seen.append(marker.get())),
        }

        marker.set("from-the-build")
        await self._load(ExternalMCPIntegration(), ["a", "b"], clients)

        assert seen == ["from-the-build", "from-the-build"]

    @pytest.mark.asyncio
    async def test_kill_switch_loads_one_at_a_time(self, monkeypatch):
        import threading

        monkeypatch.setenv("MCP_PARALLEL_PREFLIGHT_ENABLED", "false")
        active = 0
        peak = 0
        lock = threading.Lock()

        def _track():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            with lock:
                active -= 1

        seen_threads = []
        clients = {
            tid: self._blocking_client(
                lambda: (_track(), seen_threads.append(threading.current_thread()))
            )
            for tid in ("a", "b", "c")
        }

        result = await self._load(ExternalMCPIntegration(), ["a", "b", "c"], clients)

        assert result == [clients["a"], clients["b"], clients["c"]]
        assert peak == 1
        assert set(seen_threads) == {threading.current_thread()}


class TestCustomHeaders:
    """An admin's static `customHeaders` (e.g. GitHub's `X-MCP-Toolsets`)
    reach the MCP transport, and changing them rebuilds a cached client."""

    def _config(self, headers):
        from apis.shared.tools.models import MCPServerConfig

        return MCPServerConfig(
            server_url="https://api.githubcopilot.com/mcp/",
            auth_type="none",
            custom_headers=headers,
        )

    def test_transport_receives_the_headers(self):
        from agents.main_agent.integrations.external_mcp_client import (
            create_external_mcp_client,
        )

        client = create_external_mcp_client(
            self._config({"X-MCP-Toolsets": "issues,context"}), oauth_token="tok"
        )
        with patch(
            "agents.main_agent.integrations.external_mcp_client.streamable_http_transport"
        ) as transport:
            client._transport_callable()

        transport.assert_called_once()
        args, kwargs = transport.call_args
        assert args == ("https://api.githubcopilot.com/mcp/",)
        assert kwargs["headers"] == {"X-MCP-Toolsets": "issues,context"}
        # The bearer still rides the auth handler, not a static header.
        assert kwargs["auth"] is not None

    def test_no_headers_sends_none(self):
        from agents.main_agent.integrations.external_mcp_client import (
            create_external_mcp_client,
        )

        client = create_external_mcp_client(self._config({}))
        with patch(
            "agents.main_agent.integrations.external_mcp_client.streamable_http_transport"
        ) as transport:
            client._transport_callable()

        assert transport.call_args.kwargs["headers"] is None

    @pytest.mark.asyncio
    async def test_a_header_change_rebuilds_the_cached_client(self):
        """Same `updated_at`, different headers: the cached client must not be
        served, because the headers are part of the connection itself."""
        integration = ExternalMCPIntegration()
        updated_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        before = _fake_tool(updated_at, tool_id="github")
        before.mcp_config.custom_headers = {"X-MCP-Toolsets": "issues"}
        after = _fake_tool(updated_at, tool_id="github")
        after.mcp_config.custom_headers = {"X-MCP-Toolsets": "issues,context"}

        repo = SimpleNamespace(get_tool=AsyncMock(side_effect=[before, after, after]))
        client_before = SimpleNamespace(load_tools=AsyncMock(return_value=[]))
        client_after = SimpleNamespace(load_tools=AsyncMock(return_value=[]))

        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ), patch(
            "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
            side_effect=[client_before, client_after],
        ) as create_mock:
            first = await integration.load_external_tools(["github"])
            second = await integration.load_external_tools(["github"])
            third = await integration.load_external_tools(["github"])

        assert first == [client_before]
        assert second == [client_after]
        # Unchanged headers on the next turn reuse the rebuilt client.
        assert third == [client_after]
        assert create_mock.call_count == 2

    def test_header_digest_ignores_order_and_name_case(self):
        from agents.main_agent.integrations.external_mcp_client import _headers_digest

        assert _headers_digest({"X-A": "1", "X-B": "2"}) == _headers_digest(
            {"x-b": "2", "X-A": "1"}
        )
        assert _headers_digest({"X-A": "1"}) != _headers_digest({"X-A": "2"})
