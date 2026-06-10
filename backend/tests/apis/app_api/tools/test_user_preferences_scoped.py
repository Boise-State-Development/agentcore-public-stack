"""Per-tool (scoped) user preferences + server-tool surfacing in ToolCatalogService.

A user can enable a subset of an MCP server's tools. The preference key is
``<tool_id>::<mcp_tool_name>``; the base tool_id is still the RBAC unit, and
``UserToolAccess.server_tools`` carries the server's tools so the UI can render
sub-toggles. Dependencies are mocked — the logic under test is pure.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.app_api.tools.service import ToolCatalogService
from apis.shared.auth.models import User
from apis.shared.rbac.models import UserEffectivePermissions
from apis.shared.tools.models import (
    MCPServerConfig,
    MCPToolEntry,
    ToolDefinition,
    ToolProtocol,
    ToolStatus,
    UserToolPreference,
)


def _user():
    return User(user_id="u1", email="u@x.com", name="U", roles=["User"], raw_token="t")


def _perms(tools):
    return UserEffectivePermissions(
        user_id="u1",
        app_roles=["User"],
        tools=tools,
        models=["*"],
        quota_tier=None,
        resolved_at="2024-01-01T00:00:00Z",
    )


def _mcp_tool(tool_id="gmail", names=("send", "search")):
    return ToolDefinition(
        tool_id=tool_id,
        display_name=tool_id,
        description="x",
        protocol=ToolProtocol.MCP_EXTERNAL,
        status=ToolStatus.ACTIVE,
        mcp_config=MCPServerConfig(
            server_url="https://example.com/mcp",
            tools=[MCPToolEntry(name=n) for n in names],
        ),
    )


def _service(tools, prefs=None):
    repo = MagicMock()
    repo.list_tools = AsyncMock(return_value=tools)
    repo.get_user_preferences = AsyncMock(
        return_value=UserToolPreference(user_id="u1", tool_preferences=prefs or {})
    )
    repo.save_user_preferences = AsyncMock(
        side_effect=lambda uid, p: UserToolPreference(user_id=uid, tool_preferences=p)
    )
    role_service = MagicMock()
    role_service.resolve_user_permissions = AsyncMock(return_value=_perms(["*"]))
    return ToolCatalogService(repository=repo, app_role_service=role_service)


@pytest.mark.asyncio
async def test_accessible_tools_surface_server_tools():
    service = _service([_mcp_tool(names=("send", "search", "draft"))])
    tools = await service.get_user_accessible_tools(_user())
    assert len(tools) == 1
    assert {st.name for st in tools[0].server_tools} == {"send", "search", "draft"}


@pytest.mark.asyncio
async def test_scoped_pref_drives_per_tool_enabled_state():
    # send explicitly off, search explicitly on; draft falls back to the
    # server default (enabled_by_default=False here).
    service = _service(
        [_mcp_tool(names=("send", "search", "draft"))],
        prefs={"gmail::send": False, "gmail::search": True},
    )
    tools = await service.get_user_accessible_tools(_user())
    by_name = {st.name: st.enabled for st in tools[0].server_tools}
    assert by_name == {"send": False, "search": True, "draft": False}
    # The server row is "on" because at least one tool (search) is enabled.
    assert tools[0].is_enabled is True


@pytest.mark.asyncio
async def test_server_level_pref_applies_to_all_subtools():
    # A bare (server-level) preference applies to every sub-tool absent a
    # more-specific scoped preference.
    service = _service(
        [_mcp_tool(names=("send", "search"))],
        prefs={"gmail": True},
    )
    tools = await service.get_user_accessible_tools(_user())
    assert all(st.enabled for st in tools[0].server_tools)
    assert tools[0].is_enabled is True


@pytest.mark.asyncio
async def test_save_scoped_preference_for_exposed_tool():
    service = _service([_mcp_tool(names=("send", "search"))])
    saved = await service.save_user_preferences(
        _user(), {"gmail::send": True, "gmail::search": False}
    )
    assert saved.tool_preferences == {"gmail::send": True, "gmail::search": False}


@pytest.mark.asyncio
async def test_save_scoped_preference_for_unexposed_tool_rejected():
    service = _service([_mcp_tool(names=("send",))])
    with pytest.raises(ValueError, match="not exposed by their server"):
        await service.save_user_preferences(_user(), {"gmail::delete_all": True})


@pytest.mark.asyncio
async def test_save_preference_for_inaccessible_base_rejected():
    # RBAC is still enforced on the base id — no role grants 'secret_server'.
    service = _service([_mcp_tool(tool_id="gmail", names=("send",))])
    service.app_role_service.resolve_user_permissions = AsyncMock(
        return_value=_perms(["gmail"])
    )
    with pytest.raises(ValueError, match="have access to"):
        await service.save_user_preferences(_user(), {"secret_server::x": True})


@pytest.mark.asyncio
async def test_scoped_preference_on_dynamic_server_accepted():
    # No curated list (discovered live) → name can't be validated, so accept.
    service = _service([_mcp_tool(names=())])
    saved = await service.save_user_preferences(_user(), {"gmail::live_tool": True})
    assert saved.tool_preferences == {"gmail::live_tool": True}
