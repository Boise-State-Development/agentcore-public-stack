"""Tests for forward-auth MCP discovery on `/admin/tools/discover`.

A same-team MCP server can validate a forwarded user JWT (Lambda Function URL
AuthType=NONE) instead of requiring SigV4/IAM. When the admin sets
`forwardAuthToken`, discovery must sign the request with the admin's *own* OIDC
token — mirroring how the agent loop forwards the end-user's token at runtime —
rather than falling back to SigV4. See `apis/app_api/admin/tools/routes.py`.
"""

import pytest
from fastapi import HTTPException

from apis.app_api.admin.tools import routes
from apis.shared.auth.models import User
from apis.shared.tools.models import MCPAuthType, MCPDiscoverRequest

_CREATE_TARGET = (
    "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client"
)
_URL = "https://x.lambda-url.us-west-2.on.aws/mcp"


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
    """Mirrors Strands' MCPAgentTool: spec lives on `mcp_tool`."""

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


@pytest.mark.asyncio
async def test_forward_auth_discovery_signs_with_admin_token(monkeypatch):
    captured = {}

    def fake_create(config, oauth_token=None, **kwargs):
        captured["oauth_token"] = oauth_token
        return _FakeClient([_FakeTool("search_classes", "Search the catalog")])

    monkeypatch.setattr(_CREATE_TARGET, fake_create)

    req = MCPDiscoverRequest(
        serverUrl=_URL, authType=MCPAuthType.NONE, forwardAuthToken=True
    )
    resp = await routes.admin_discover_mcp_tools(req, admin=_admin("admin-tok"))

    assert captured["oauth_token"] == "admin-tok"
    assert [t.name for t in resp.tools] == ["search_classes"]


@pytest.mark.asyncio
async def test_forward_auth_discovery_without_token_returns_400(monkeypatch):
    def fake_create(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("client must not be built without a forwardable token")

    monkeypatch.setattr(_CREATE_TARGET, fake_create)

    req = MCPDiscoverRequest(
        serverUrl=_URL, authType=MCPAuthType.NONE, forwardAuthToken=True
    )
    with pytest.raises(HTTPException) as exc_info:
        await routes.admin_discover_mcp_tools(req, admin=_admin(raw_token=None))

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_non_forward_discovery_does_not_forward_token(monkeypatch):
    """Default (SigV4) discovery must not leak the admin's bearer token."""
    captured = {}

    def fake_create(config, oauth_token=None, **kwargs):
        captured["oauth_token"] = oauth_token
        return _FakeClient([_FakeTool("search_classes")])

    monkeypatch.setattr(_CREATE_TARGET, fake_create)

    req = MCPDiscoverRequest(
        serverUrl=_URL, authType=MCPAuthType.AWS_IAM, forwardAuthToken=False
    )
    await routes.admin_discover_mcp_tools(req, admin=_admin("admin-tok"))

    assert captured["oauth_token"] is None
