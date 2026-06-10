"""
Tests for ToolFilter — tool categorisation into local, gateway, and external MCP buckets.

Requirements: 6.1–6.6
"""

import pytest

from agents.main_agent.tools.tool_filter import ToolFilter, ToolFilterResult


# ---------------------------------------------------------------------------
# Req 6.1 — None / empty enabled_tool_ids → empty results
# ---------------------------------------------------------------------------
class TestFilterToolsEmpty:
    """WHEN enabled_tool_ids is None or empty, filter_tools returns empty lists."""

    def test_none_returns_empty(self, tool_filter: ToolFilter):
        local_tools, gateway_ids = tool_filter.filter_tools(None)
        assert local_tools == []
        assert gateway_ids == []

    def test_empty_list_returns_empty(self, tool_filter: ToolFilter):
        local_tools, gateway_ids = tool_filter.filter_tools([])
        assert local_tools == []
        assert gateway_ids == []


# ---------------------------------------------------------------------------
# Req 6.2 — registered local tool IDs → corresponding tool objects
# ---------------------------------------------------------------------------
class TestFilterToolsLocal:
    """WHEN enabled_tool_ids contains registered local tool IDs, filter_tools
    returns the corresponding tool objects."""

    def test_single_local_tool(self, tool_filter: ToolFilter):
        local_tools, gateway_ids = tool_filter.filter_tools(["calculator"])
        assert len(local_tools) == 1
        assert local_tools[0].__name__ == "calculator"
        assert gateway_ids == []

    def test_multiple_local_tools(self, tool_filter: ToolFilter):
        local_tools, gateway_ids = tool_filter.filter_tools(
            ["calculator", "weather", "search"]
        )
        assert len(local_tools) == 3
        names = {t.__name__ for t in local_tools}
        assert names == {"calculator", "weather", "search"}
        assert gateway_ids == []


# ---------------------------------------------------------------------------
# Req 6.3 — IDs starting with "gateway_" → gateway_tool_ids list
# ---------------------------------------------------------------------------
class TestFilterToolsGateway:
    """WHEN enabled_tool_ids contains IDs starting with 'gateway_', filter_tools
    returns those IDs in the gateway_tool_ids list."""

    def test_gateway_tools_returned(self, tool_filter: ToolFilter):
        local_tools, gateway_ids = tool_filter.filter_tools(
            ["gateway_wikipedia", "gateway_arxiv"]
        )
        assert local_tools == []
        assert set(gateway_ids) == {"gateway_wikipedia", "gateway_arxiv"}

    def test_mixed_local_and_gateway(self, tool_filter: ToolFilter):
        local_tools, gateway_ids = tool_filter.filter_tools(
            ["calculator", "gateway_wikipedia"]
        )
        assert len(local_tools) == 1
        assert local_tools[0].__name__ == "calculator"
        assert gateway_ids == ["gateway_wikipedia"]


# ---------------------------------------------------------------------------
# Req 6.4 — unrecognized tool IDs are skipped
# ---------------------------------------------------------------------------
class TestFilterToolsUnrecognized:
    """WHEN enabled_tool_ids contains unrecognized tool IDs, filter_tools skips them."""

    def test_unknown_ids_skipped(self, tool_filter: ToolFilter):
        local_tools, gateway_ids = tool_filter.filter_tools(
            ["nonexistent_tool", "another_unknown"]
        )
        assert local_tools == []
        assert gateway_ids == []

    def test_mixed_known_and_unknown(self, tool_filter: ToolFilter):
        local_tools, gateway_ids = tool_filter.filter_tools(
            ["calculator", "nonexistent_tool", "gateway_wiki"]
        )
        assert len(local_tools) == 1
        assert gateway_ids == ["gateway_wiki"]


# ---------------------------------------------------------------------------
# Req 6.5 — set_external_mcp_tools + filter_tools_extended
# ---------------------------------------------------------------------------
class TestFilterToolsExtended:
    """WHEN set_external_mcp_tools is called and filter_tools_extended is used,
    external MCP tool IDs appear in the external_mcp_tool_ids list."""

    def test_external_mcp_tools_returned(self, tool_filter: ToolFilter):
        tool_filter.set_external_mcp_tools(["ext_tool_a", "ext_tool_b"])

        result = tool_filter.filter_tools_extended(
            ["calculator", "gateway_wiki", "ext_tool_a", "ext_tool_b"]
        )

        assert isinstance(result, ToolFilterResult)
        assert len(result.local_tools) == 1
        assert result.gateway_tool_ids == ["gateway_wiki"]
        assert set(result.external_mcp_tool_ids) == {"ext_tool_a", "ext_tool_b"}

    def test_extended_empty_returns_empty_result(self, tool_filter: ToolFilter):
        result = tool_filter.filter_tools_extended(None)
        assert result.local_tools == []
        assert result.gateway_tool_ids == []
        assert result.external_mcp_tool_ids == []

    def test_external_mcp_not_in_basic_filter(self, tool_filter: ToolFilter):
        """External MCP tools are silently skipped by the basic filter_tools."""
        tool_filter.set_external_mcp_tools(["ext_tool_a"])
        local_tools, gateway_ids = tool_filter.filter_tools(["ext_tool_a"])
        assert local_tools == []
        assert gateway_ids == []


# ---------------------------------------------------------------------------
# Scoped ids (`base::tool`) — classify by the base catalog id, carry through
# ---------------------------------------------------------------------------
class TestFilterToolsScoped:
    """A scoped id selecting one tool of an MCP server is classified by its base
    catalog id; the scoped id rides through to the per-source resolver."""

    def test_scoped_gateway_id_carried_through(self, tool_filter: ToolFilter):
        result = tool_filter.filter_tools_extended(["gateway_wiki::search"])
        assert result.gateway_tool_ids == ["gateway_wiki::search"]
        assert result.external_mcp_tool_ids == []

    def test_scoped_external_id_carried_through(self, tool_filter: ToolFilter):
        tool_filter.set_external_mcp_tools(["ext_tool_a"])
        result = tool_filter.filter_tools_extended(["ext_tool_a::do_thing"])
        assert result.external_mcp_tool_ids == ["ext_tool_a::do_thing"]
        assert result.local_tools == []

    def test_bare_and_scoped_local_not_double_added(self, tool_filter: ToolFilter):
        # A local tool is a single tool — bare + scoped must not add it twice.
        result = tool_filter.filter_tools_extended(["calculator", "calculator::x"])
        assert len(result.local_tools) == 1

    def test_scoped_counts_as_its_base_category_in_stats(self, tool_filter: ToolFilter):
        tool_filter.set_external_mcp_tools(["ext_tool_a"])
        stats = tool_filter.get_statistics(
            ["gateway_wiki::search", "ext_tool_a::do_thing"]
        )
        assert stats["gateway_tools"] == 1
        assert stats["external_mcp_tools"] == 1
        assert stats["unknown_tools"] == 0


# ---------------------------------------------------------------------------
# Req 6.6 — get_statistics returns correct counts
# ---------------------------------------------------------------------------
class TestGetStatistics:
    """get_statistics returns correct counts for each tool category."""

    def test_empty_input(self, tool_filter: ToolFilter):
        stats = tool_filter.get_statistics(None)
        assert stats == {
            "total_requested": 0,
            "local_tools": 0,
            "gateway_tools": 0,
            "external_mcp_tools": 0,
            "unknown_tools": 0,
        }

    def test_all_categories(self, tool_filter: ToolFilter):
        tool_filter.set_external_mcp_tools(["ext_tool_a"])

        stats = tool_filter.get_statistics(
            ["calculator", "weather", "gateway_wiki", "ext_tool_a", "unknown_x"]
        )

        assert stats["total_requested"] == 5
        assert stats["local_tools"] == 2
        assert stats["gateway_tools"] == 1
        assert stats["external_mcp_tools"] == 1
        assert stats["unknown_tools"] == 1

    def test_only_local(self, tool_filter: ToolFilter):
        stats = tool_filter.get_statistics(["calculator", "browser"])
        assert stats["total_requested"] == 2
        assert stats["local_tools"] == 2
        assert stats["gateway_tools"] == 0
        assert stats["external_mcp_tools"] == 0
        assert stats["unknown_tools"] == 0

    def test_only_gateway(self, tool_filter: ToolFilter):
        stats = tool_filter.get_statistics(["gateway_a", "gateway_b"])
        assert stats["total_requested"] == 2
        assert stats["gateway_tools"] == 2
        assert stats["local_tools"] == 0
