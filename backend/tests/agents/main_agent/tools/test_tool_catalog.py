"""
Tests for ToolCatalogService — tool metadata querying, gateway tool registration,
and ToolMetadata serialization.

Requirements: 7.1–7.6
"""

import pytest

from agents.main_agent.tools.tool_catalog import (
    ToolCatalogService,
    ToolCategory,
    ToolMetadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_test_catalog() -> dict[str, ToolMetadata]:
    """Return a small, deterministic catalog for testing."""
    return {
        "calculator": ToolMetadata(
            tool_id="calculator",
            name="Calculator",
            description="Math operations",
            category=ToolCategory.UTILITIES,
            icon="calc",
        ),
        "fetch_url_content": ToolMetadata(
            tool_id="fetch_url_content",
            name="URL Fetcher",
            description="Fetch web pages",
            category=ToolCategory.SEARCH,
            icon="link",
        ),
        "create_visualization": ToolMetadata(
            tool_id="create_visualization",
            name="Charts & Graphs",
            description="Create charts",
            category=ToolCategory.DATA,
            icon="chart-bar",
        ),
    }


@pytest.fixture
def catalog_service() -> ToolCatalogService:
    """ToolCatalogService backed by a small test catalog."""
    return ToolCatalogService(catalog=_build_test_catalog())


# ---------------------------------------------------------------------------
# 7.1 — get_all_tools returns all tools in the catalog
# ---------------------------------------------------------------------------

class TestGetAllTools:
    def test_returns_all_tools(self, catalog_service: ToolCatalogService):
        """Validates: Requirement 7.1"""
        tools = catalog_service.get_all_tools()
        assert len(tools) == 3
        ids = {t.tool_id for t in tools}
        assert ids == {"calculator", "fetch_url_content", "create_visualization"}

    def test_default_catalog_used_when_none_provided(self):
        """Validates: Requirement 7.1 (edge case — default catalog)"""
        service = ToolCatalogService()
        tools = service.get_all_tools()
        # Default catalog has at least the built-in tools
        assert len(tools) > 0


# ---------------------------------------------------------------------------
# 7.2 — get_tool with valid tool_id returns correct ToolMetadata
# 7.3 — get_tool with invalid tool_id returns None
# ---------------------------------------------------------------------------

class TestGetTool:
    def test_valid_tool_id_returns_metadata(self, catalog_service: ToolCatalogService):
        """Validates: Requirement 7.2"""
        tool = catalog_service.get_tool("calculator")
        assert tool is not None
        assert tool.tool_id == "calculator"
        assert tool.name == "Calculator"
        assert tool.category == ToolCategory.UTILITIES

    def test_invalid_tool_id_returns_none(self, catalog_service: ToolCatalogService):
        """Validates: Requirement 7.3"""
        assert catalog_service.get_tool("nonexistent_tool") is None


# ---------------------------------------------------------------------------
# 7.4 — get_tools_by_category returns only matching tools
# ---------------------------------------------------------------------------

class TestGetToolsByCategory:
    def test_returns_only_matching_category(self, catalog_service: ToolCatalogService):
        """Validates: Requirement 7.4"""
        search_tools = catalog_service.get_tools_by_category(ToolCategory.SEARCH)
        assert len(search_tools) == 1
        assert search_tools[0].tool_id == "fetch_url_content"

    def test_category_with_no_tools_returns_empty(self, catalog_service: ToolCatalogService):
        """Validates: Requirement 7.4 (edge case — no tools in category)"""
        gateway_tools = catalog_service.get_tools_by_category(ToolCategory.GATEWAY)
        assert gateway_tools == []


# ---------------------------------------------------------------------------
# 7.5 — add_gateway_tool auto-prefixes with "gateway_"
# ---------------------------------------------------------------------------

class TestAddGatewayTool:
    def test_auto_prefix_added_when_missing(self, catalog_service: ToolCatalogService):
        """Validates: Requirement 7.5"""
        catalog_service.add_gateway_tool(
            tool_id="wikipedia_search",
            name="Wikipedia",
            description="Search Wikipedia",
        )
        tool = catalog_service.get_tool("gateway_wikipedia_search")
        assert tool is not None
        assert tool.tool_id == "gateway_wikipedia_search"
        assert tool.is_gateway_tool is True
        assert tool.category == ToolCategory.GATEWAY

    def test_prefix_not_doubled_when_present(self, catalog_service: ToolCatalogService):
        """Validates: Requirement 7.5 (edge case — prefix already present)"""
        catalog_service.add_gateway_tool(
            tool_id="gateway_arxiv_search",
            name="ArXiv",
            description="Search ArXiv",
        )
        tool = catalog_service.get_tool("gateway_arxiv_search")
        assert tool is not None
        assert tool.tool_id == "gateway_arxiv_search"
        # Ensure no double prefix
        assert catalog_service.get_tool("gateway_gateway_arxiv_search") is None


# ---------------------------------------------------------------------------
# 7.6 — ToolMetadata.to_dict produces camelCase keys
# ---------------------------------------------------------------------------

class TestToolMetadataToDict:
    def test_to_dict_has_camel_case_keys(self):
        """Validates: Requirement 7.6"""
        meta = ToolMetadata(
            tool_id="test_tool",
            name="Test Tool",
            description="A test tool",
            category=ToolCategory.UTILITIES,
            is_gateway_tool=True,
            requires_oauth_provider="google",
            icon="wrench",
        )
        d = meta.to_dict()

        # Verify camelCase keys exist
        assert "toolId" in d
        assert "isGatewayTool" in d
        assert "requiresOauthProvider" in d

        # Verify values
        assert d["toolId"] == "test_tool"
        assert d["name"] == "Test Tool"
        assert d["description"] == "A test tool"
        assert d["category"] == "utilities"
        assert d["isGatewayTool"] is True
        assert d["requiresOauthProvider"] == "google"
        assert d["icon"] == "wrench"

    def test_to_dict_with_none_optional_fields(self):
        """Validates: Requirement 7.6 (edge case — None optionals)"""
        meta = ToolMetadata(
            tool_id="basic",
            name="Basic",
            description="Basic tool",
            category=ToolCategory.DATA,
        )
        d = meta.to_dict()

        assert d["isGatewayTool"] is False
        assert d["requiresOauthProvider"] is None
        assert d["icon"] is None
