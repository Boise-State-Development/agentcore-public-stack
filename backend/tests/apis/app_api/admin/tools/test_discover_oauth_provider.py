"""Tests for OAuth (3LO) MCP discovery on `/admin/tools/discover`.

An OAuth-gated MCP server (e.g. the GitHub remote MCP server) is discovered
using the *admin's own* vaulted token for the named provider — fetched via
AgentCore Identity and injected as a bearer, mirroring how the agent loop
attaches the end-user's provider token at runtime. See
`apis/app_api/admin/tools/routes.py`.
"""

import pytest
from fastapi import HTTPException

from apis.app_api.admin.tools import routes
from apis.shared.auth.models import User
from apis.shared.tools.models import MCPAuthType, MCPDiscoverRequest

_CREATE_TARGET = (
    "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client"
)
_URL = "https://api.githubcopilot.com/mcp/"


def _admin(raw_token="admin-tok"):
    return User(
        email="admin@example.edu",
        user_id="u1",
        name="Admin",
        roles=["system_admin"],
        raw_token=raw_token,
    )


class _FakeSpec:
    def __init__(self, name, description=None):
        self.name = name
        self.description = description


class _FakeTool:
    def __init__(self, name, description=None):
        self.mcp_tool = _FakeSpec(name, description)
        self.tool_name = name


class _FakeClient:
    def __init__(self, tools):
        self._tools = tools

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_tools_sync(self):
        return list(self._tools)


class _FakeProvider:
    def __init__(self, provider_id="github", display_name="GitHub"):
        self.provider_id = provider_id
        self.display_name = display_name
        self.scopes = ["repo", "read:org"]
        self.custom_parameters = None


class _FakeProviderRepo:
    def __init__(self, provider):
        self._provider = provider

    async def get_provider(self, provider_id):
        if self._provider and self._provider.provider_id == provider_id:
            return self._provider
        return None


class _FakeTokenResult:
    def __init__(self, access_token=None, requires_consent=False):
        self.access_token = access_token
        self.requires_consent = requires_consent


class _FakeIdentity:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def get_token_for_user(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


@pytest.mark.asyncio
async def test_oauth_provider_discovery_uses_admin_vaulted_token(monkeypatch):
    """A connected provider yields a vaulted bearer injected into the client."""
    captured = {}

    def fake_create(config, oauth_token=None, **kwargs):
        captured["oauth_token"] = oauth_token
        return _FakeClient([_FakeTool("search_repositories", "Find repos")])

    monkeypatch.setattr(_CREATE_TARGET, fake_create)

    identity = _FakeIdentity(_FakeTokenResult(access_token="gho_vaulted"))
    monkeypatch.setattr(routes, "get_agentcore_identity_client", lambda: identity)

    req = MCPDiscoverRequest(
        serverUrl=_URL,
        authType=MCPAuthType.OAUTH2,
        requiresOauthProvider="github",
    )
    resp = await routes.admin_discover_mcp_tools(
        req, admin=_admin(), provider_repo=_FakeProviderRepo(_FakeProvider())
    )

    assert captured["oauth_token"] == "gho_vaulted"
    assert [t.name for t in resp.tools] == ["search_repositories"]
    # The token was fetched for the admin's own actor id.
    assert identity.calls[0]["user_id"] == "u1"
    assert identity.calls[0]["provider_name"] == "github"


@pytest.mark.asyncio
async def test_oauth_provider_discovery_requires_consent_returns_409(monkeypatch):
    """An unconnected admin gets an actionable 409, and no client is built."""

    def fake_create(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("client must not be built without a vaulted token")

    monkeypatch.setattr(_CREATE_TARGET, fake_create)

    identity = _FakeIdentity(_FakeTokenResult(requires_consent=True))
    monkeypatch.setattr(routes, "get_agentcore_identity_client", lambda: identity)

    req = MCPDiscoverRequest(
        serverUrl=_URL,
        authType=MCPAuthType.OAUTH2,
        requiresOauthProvider="github",
    )
    with pytest.raises(HTTPException) as exc_info:
        await routes.admin_discover_mcp_tools(
            req, admin=_admin(), provider_repo=_FakeProviderRepo(_FakeProvider())
        )

    assert exc_info.value.status_code == 409
    assert "connect" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_unknown_oauth_provider_returns_400(monkeypatch):
    def fake_create(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("client must not be built for an unknown provider")

    monkeypatch.setattr(_CREATE_TARGET, fake_create)

    req = MCPDiscoverRequest(
        serverUrl=_URL,
        authType=MCPAuthType.OAUTH2,
        requiresOauthProvider="ghost",
    )
    with pytest.raises(HTTPException) as exc_info:
        await routes.admin_discover_mcp_tools(
            req, admin=_admin(), provider_repo=_FakeProviderRepo(_FakeProvider())
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_oauth_provider_and_forward_auth_conflict_returns_400(monkeypatch):
    def fake_create(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("client must not be built for a conflicting request")

    monkeypatch.setattr(_CREATE_TARGET, fake_create)

    req = MCPDiscoverRequest(
        serverUrl=_URL,
        authType=MCPAuthType.OAUTH2,
        requiresOauthProvider="github",
        forwardAuthToken=True,
    )
    with pytest.raises(HTTPException) as exc_info:
        await routes.admin_discover_mcp_tools(
            req, admin=_admin(), provider_repo=_FakeProviderRepo(_FakeProvider())
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_oauth2_authtype_without_provider_returns_400(monkeypatch):
    """auth_type=oauth2 with no provider named can't be discovered — 400."""

    def fake_create(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("client must not be built without a provider")

    monkeypatch.setattr(_CREATE_TARGET, fake_create)

    req = MCPDiscoverRequest(serverUrl=_URL, authType=MCPAuthType.OAUTH2)
    with pytest.raises(HTTPException) as exc_info:
        await routes.admin_discover_mcp_tools(
            req, admin=_admin(), provider_repo=_FakeProviderRepo(None)
        )

    assert exc_info.value.status_code == 400
