"""Live MCP tool discovery for a saved catalog tool (per-tool enablement).

The helper connects to a saved external MCP server (or returns a gateway
target's recorded tools) so the admin skills picker and model-settings UI can
offer per-tool selection. The MCP client is patched — no network here.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apis.app_api.tools.discovery import discover_tools_for_saved_tool
from apis.shared.tools.models import (
    MCPGatewayConfig,
    MCPServerConfig,
    MCPToolEntry,
    ToolDefinition,
    ToolProtocol,
    ToolStatus,
)

CREATE_CLIENT = (
    "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client"
)


def _ext(tool_id="gmail", names=(), requires_oauth=None, forward=False):
    return ToolDefinition(
        tool_id=tool_id,
        display_name=tool_id,
        description="x",
        protocol=ToolProtocol.MCP_EXTERNAL,
        status=ToolStatus.ACTIVE,
        requires_oauth_provider=requires_oauth,
        forward_auth_token=forward,
        mcp_config=MCPServerConfig(
            server_url="https://example.com/mcp",
            tools=[MCPToolEntry(name=n) for n in names],
        ),
    )


class _FakeMCPTool:
    def __init__(self, name, desc=None):
        self.mcp_tool = SimpleNamespace(name=name, description=desc)
        self.tool_name = name


class _FakeClient:
    def __init__(self, tools):
        self._tools = tools

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_tools_sync(self):
        return self._tools


@pytest.mark.asyncio
async def test_gateway_returns_curated_tools():
    tool = ToolDefinition(
        tool_id="gw",
        display_name="gw",
        description="x",
        protocol=ToolProtocol.MCP_GATEWAY,
        status=ToolStatus.ACTIVE,
        mcp_gateway_config=MCPGatewayConfig(
            target_name="t",
            endpoint_url="https://example.com/mcp",
            tools=[MCPToolEntry(name="a"), MCPToolEntry(name="b")],
        ),
    )
    out = await discover_tools_for_saved_tool(tool)
    assert {t.name for t in out} == {"a", "b"}


@pytest.mark.asyncio
async def test_external_lists_live_tools():
    client = _FakeClient([_FakeMCPTool("send", "Send mail"), _FakeMCPTool("search")])
    with patch(CREATE_CLIENT, return_value=client):
        out = await discover_tools_for_saved_tool(_ext(names=()))
    assert {t.name for t in out} == {"send", "search"}


@pytest.mark.asyncio
async def test_oauth_server_falls_back_to_curated_without_connecting():
    tool = _ext(names=("a",), requires_oauth="google")
    with patch(CREATE_CLIENT, side_effect=AssertionError("should not connect")):
        out = await discover_tools_for_saved_tool(tool)
    assert {t.name for t in out} == {"a"}


@pytest.mark.asyncio
async def test_unreachable_server_raises_runtimeerror():
    class _BoomClient(_FakeClient):
        def list_tools_sync(self):
            raise RuntimeError("connection refused")

    with patch(CREATE_CLIENT, return_value=_BoomClient([])):
        with pytest.raises(RuntimeError, match="tools/list"):
            await discover_tools_for_saved_tool(_ext())
