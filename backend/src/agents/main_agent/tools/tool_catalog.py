"""
Tool Catalog - Metadata for all available tools

Provides tool metadata for authorization, UI display, and discovery.
Tools are identified by their function name (tool_id).
"""
from dataclasses import dataclass
from typing import List, Dict, Optional
from enum import Enum


class ToolCategory(str, Enum):
    """Categories for organizing tools in the UI."""
    SEARCH = "search"
    DATA = "data"
    UTILITIES = "utilities"
    CODE = "code"
    GATEWAY = "gateway"


@dataclass
class ToolMetadata:
    """Metadata for a single tool."""
    tool_id: str
    name: str
    description: str
    category: ToolCategory
    is_gateway_tool: bool = False
    requires_oauth_provider: Optional[str] = None  # OAuth provider ID if required
    icon: Optional[str] = None  # Icon name for UI

    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "toolId": self.tool_id,
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "isGatewayTool": self.is_gateway_tool,
            "requiresOauthProvider": self.requires_oauth_provider,
            "icon": self.icon,
        }


# =============================================================================
# Tool Catalog Definition
# =============================================================================

TOOL_CATALOG: Dict[str, ToolMetadata] = {
    # --- Local Tools (Search & Web) ---
    "fetch_url_content": ToolMetadata(
        tool_id="fetch_url_content",
        name="URL Fetcher",
        description="Fetch and extract text content from web pages, job descriptions, articles, and documentation.",
        category=ToolCategory.SEARCH,
        icon="link",
    ),
    # --- Local Tools (Data & Visualization) ---
    "get_current_weather": ToolMetadata(
        tool_id="get_current_weather",
        name="Weather",
        description="Get current weather conditions for US locations using coordinates.",
        category=ToolCategory.DATA,
        icon="cloud",
    ),
    "create_visualization": ToolMetadata(
        tool_id="create_visualization",
        name="Charts & Graphs",
        description="Create interactive bar, line, and pie charts from data.",
        category=ToolCategory.DATA,
        icon="chart-bar",
    ),

    # --- Built-in Tools (Utilities) ---
    "calculator": ToolMetadata(
        tool_id="calculator",
        name="Calculator",
        description="Perform mathematical calculations and evaluations.",
        category=ToolCategory.UTILITIES,
        icon="calculator",
    ),

    # --- Built-in Tools (Code Interpreter) ---
    "generate_diagram_and_validate": ToolMetadata(
        tool_id="generate_diagram_and_validate",
        name="Code Interpreter",
        description="Generate diagrams, charts, and visualizations using Python code in a sandboxed environment.",
        category=ToolCategory.CODE,
        icon="code-bracket",
    ),

    # --- Gateway/MCP Tools ---
    # These are loaded dynamically from the gateway but we define metadata here
    # for the admin UI. Actual tool availability depends on gateway configuration.
}


class ToolCatalogService:
    """Service for accessing the tool catalog."""

    def __init__(self, catalog: Dict[str, ToolMetadata] = None):
        """Initialize with optional custom catalog."""
        self._catalog = catalog or TOOL_CATALOG

    def get_all_tools(self) -> List[ToolMetadata]:
        """Get all tools in the catalog."""
        return list(self._catalog.values())

    def get_tool(self, tool_id: str) -> Optional[ToolMetadata]:
        """Get a specific tool by ID."""
        return self._catalog.get(tool_id)

    def get_tools_by_category(self, category: ToolCategory) -> List[ToolMetadata]:
        """Get all tools in a specific category."""
        return [t for t in self._catalog.values() if t.category == category]

    def get_tool_ids(self) -> List[str]:
        """Get list of all tool IDs."""
        return list(self._catalog.keys())

    def has_tool(self, tool_id: str) -> bool:
        """Check if a tool exists in the catalog."""
        return tool_id in self._catalog

    def add_gateway_tool(self, tool_id: str, name: str, description: str) -> None:
        """
        Register a gateway tool dynamically.

        Gateway tools are prefixed with 'gateway_' and loaded from MCP servers.
        """
        if not tool_id.startswith("gateway_"):
            tool_id = f"gateway_{tool_id}"

        self._catalog[tool_id] = ToolMetadata(
            tool_id=tool_id,
            name=name,
            description=description,
            category=ToolCategory.GATEWAY,
            is_gateway_tool=True,
            icon="server",
        )


# Singleton instance
_tool_catalog_service: Optional[ToolCatalogService] = None


def get_tool_catalog_service() -> ToolCatalogService:
    """Get the singleton ToolCatalogService instance."""
    global _tool_catalog_service
    if _tool_catalog_service is None:
        _tool_catalog_service = ToolCatalogService()
    return _tool_catalog_service
