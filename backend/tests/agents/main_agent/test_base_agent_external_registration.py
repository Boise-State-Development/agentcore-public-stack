"""Regression: external MCP tool *registration* must base-normalize scoped ids.

`_register_external_mcp_tools` queries the catalog and seeds the tool filter's
external set. The catalog only knows *base* ids and the filter classifies by
the base (`base in _external_mcp_tools`), so a scoped binding (`base::tool`,
e.g. a skill that enables a subset of a Canvas server) must register its base.

Before the fix it looked the catalog up with the raw scoped id — an exact PK
lookup that returns nothing — so the base was never registered, the id was
never classified as external, and the per-tool binding was never loaded (the
skill folded zero tools and the model reported the server "not connected").
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.tools.tool_filter import ToolFilter


def _make_filter() -> ToolFilter:
    # The registry only needs has_tool for classification; no local tools here.
    return ToolFilter(SimpleNamespace(has_tool=lambda _b: False))


class TestRegisterExternalMcpToolsScoped:
    def test_scoped_external_registration_enables_classification(self):
        tool_filter = _make_filter()
        agent = SimpleNamespace(
            enabled_tools=["canvas::list_courses", "canvas::whoami"],
            tool_filter=tool_filter,
        )

        async def fake_get_tool(tool_id):
            # The catalog only knows the base id.
            if tool_id == "canvas":
                return SimpleNamespace(protocol="mcp_external")
            return None

        repo = SimpleNamespace(get_tool=AsyncMock(side_effect=fake_get_tool))
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ):
            BaseAgent._register_external_mcp_tools(agent)

        # Base id registered (deduped across the two scoped ids)...
        assert tool_filter._external_mcp_tools == {"canvas"}
        # ...and the catalog was queried by base, never the raw scoped id.
        queried = {c.args[0] for c in repo.get_tool.await_args_list}
        assert queried == {"canvas"}
        # End-to-end: classification now carries both scoped ids through as
        # external (the contract load_external_tools / the skill fold rely on).
        result = tool_filter.filter_tools_extended(
            ["canvas::list_courses", "canvas::whoami"]
        )
        assert result.external_mcp_tool_ids == [
            "canvas::list_courses",
            "canvas::whoami",
        ]

    def test_bare_id_still_registers(self):
        tool_filter = _make_filter()
        agent = SimpleNamespace(enabled_tools=["canvas"], tool_filter=tool_filter)
        repo = SimpleNamespace(
            get_tool=AsyncMock(
                return_value=SimpleNamespace(protocol="mcp_external")
            )
        )
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ):
            BaseAgent._register_external_mcp_tools(agent)

        assert tool_filter._external_mcp_tools == {"canvas"}
