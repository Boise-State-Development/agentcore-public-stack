"""The user-facing side of always-on (PR-3 of docs/specs/admin-always-on-tools.md).

Two halves that must agree, or the picker and the turn tell the user different
stories:

* `GET /tools` forces a pinned tool on and marks it locked, so the SPA can
  render policy rather than a toggle that springs back.
* `save_user_preferences` drops an attempt to turn one off (D6), rather than
  persisting a preference the turn will override anyway.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apis.app_api.tools.service import ToolCatalogService
from apis.shared.tools.models import (
    MCPServerConfig,
    MCPToolEntry,
    ToolDefinition,
    ToolProtocol,
    UserToolPreference,
)

USER = SimpleNamespace(user_id="u1", name="U", roles=["staff"])


def _tool(tool_id, *, always_on=False, enabled_by_default=False, entries=None):
    cfg = None
    if entries is not None:
        cfg = MCPServerConfig(
            server_url="https://example.com/mcp",
            tools=[MCPToolEntry(name=n, always_on=a) for n, a in entries],
        )
    return ToolDefinition(
        tool_id=tool_id,
        display_name=tool_id,
        description="d",
        protocol=ToolProtocol.MCP_EXTERNAL if cfg else ToolProtocol.LOCAL,
        always_on=always_on,
        enabled_by_default=enabled_by_default,
        mcp_config=cfg,
    )


def _service(tools, prefs=None):
    repo = SimpleNamespace()
    repo.list_tools = AsyncMock(return_value=tools)
    repo.get_user_preferences = AsyncMock(
        return_value=UserToolPreference(
            user_id="u1", tool_preferences=dict(prefs or {})
        )
    )
    repo.save_user_preferences = AsyncMock(side_effect=lambda uid, p: p)

    roles = SimpleNamespace()
    roles.resolve_user_permissions = AsyncMock(
        return_value=SimpleNamespace(tools={"*"}, app_roles=["staff"])
    )
    return ToolCatalogService(
        repository=repo,
        app_role_service=roles,
        app_role_admin_service=SimpleNamespace(),
    )


class TestGetTools:
    @pytest.mark.asyncio
    async def test_a_pinned_tool_is_on_and_locked(self):
        svc = _service([_tool("kb_search", always_on=True)])
        [row] = await svc.get_user_accessible_tools(USER)
        assert row.always_on is True
        assert row.is_enabled is True

    @pytest.mark.asyncio
    async def test_a_stored_opt_out_does_not_win_over_the_pin(self):
        """The preference is kept, not honoured.

        It is the user's, and it applies again the moment an admin unpins —
        but while the pin is in force the picker must show what the turn will
        actually do.
        """
        svc = _service(
            [_tool("kb_search", always_on=True)], prefs={"kb_search": False}
        )
        [row] = await svc.get_user_accessible_tools(USER)
        assert row.is_enabled is True
        assert row.user_enabled is False

    @pytest.mark.asyncio
    async def test_an_unpinned_tool_is_untouched(self):
        svc = _service([_tool("browse_web")], prefs={"browse_web": False})
        [row] = await svc.get_user_accessible_tools(USER)
        assert row.always_on is False
        assert row.is_enabled is False

    @pytest.mark.asyncio
    async def test_a_pinned_sub_tool_locks_only_its_own_row(self):
        svc = _service(
            [_tool("weather", entries=[("get_forecast", True), ("get_history", False)])],
            prefs={"weather::get_forecast": False, "weather::get_history": False},
        )
        [row] = await svc.get_user_accessible_tools(USER)
        by_name = {st.name: st for st in row.server_tools}
        assert by_name["get_forecast"].always_on is True
        assert by_name["get_forecast"].enabled is True
        assert by_name["get_history"].always_on is False
        assert by_name["get_history"].enabled is False
        # The server row itself is NOT locked: the user may still turn the
        # unpinned tools off, so a row-level lock would overstate the policy.
        assert row.always_on is False
        # But it reads as on, because "any tool enabled" and one cannot be off.
        assert row.is_enabled is True

    @pytest.mark.asyncio
    async def test_pinning_a_whole_server_locks_every_sub_tool(self):
        svc = _service(
            [_tool("weather", always_on=True, entries=[("a", False), ("b", False)])]
        )
        [row] = await svc.get_user_accessible_tools(USER)
        assert row.always_on is True
        assert all(st.always_on and st.enabled for st in row.server_tools)


class TestSavePreferencesGuard:
    @pytest.mark.asyncio
    async def test_an_opt_out_of_a_pinned_tool_is_dropped(self):
        svc = _service([_tool("kb_search", always_on=True), _tool("browse_web")])
        saved = await svc.save_user_preferences(
            USER, {"kb_search": False, "browse_web": False}
        )
        # Dropped, not rejected: the SPA sends the whole map on every toggle,
        # so 400-ing would let one pinned tool block every other change.
        assert saved == {"browse_web": False}

    @pytest.mark.asyncio
    async def test_turning_a_pinned_tool_ON_is_kept(self):
        """It agrees with the pin, and it is what should apply if unpinned."""
        svc = _service([_tool("kb_search", always_on=True)])
        assert await svc.save_user_preferences(USER, {"kb_search": True}) == {
            "kb_search": True
        }

    @pytest.mark.asyncio
    async def test_an_opt_out_of_a_pinned_sub_tool_is_dropped(self):
        svc = _service(
            [_tool("weather", entries=[("get_forecast", True), ("get_history", False)])]
        )
        saved = await svc.save_user_preferences(
            USER,
            {"weather::get_forecast": False, "weather::get_history": False},
        )
        assert saved == {"weather::get_history": False}

    @pytest.mark.asyncio
    async def test_ordinary_preferences_are_untouched(self):
        svc = _service([_tool("browse_web"), _tool("search_web")])
        prefs = {"browse_web": False, "search_web": True}
        assert await svc.save_user_preferences(USER, prefs) == prefs
