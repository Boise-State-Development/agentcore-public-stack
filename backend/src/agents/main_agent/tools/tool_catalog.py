"""
Tool Catalog - Metadata for all available tools

Provides tool metadata for authorization, UI display, and discovery.
Tools are identified by their function name (tool_id).
"""
from dataclasses import dataclass
from typing import List, Dict, Optional
from enum import Enum

from agents.main_agent.config.constants import Prefixes


class ToolCategory(str, Enum):
    """Categories for organizing tools in the UI."""
    SEARCH = "search"
    DATA = "data"
    UTILITIES = "utilities"
    CODE = "code"
    GATEWAY = "gateway"
    ACCOUNT = "account"


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
    # Platform self-service tier (.kiro/specs/platform-self-service/). ``system``
    # = shipped with the app, always-on, not a user picker toggle. ``hidden`` =
    # kept OUT of the settings Tools toggle list but retained (flagged) in the
    # catalog payload so a tool-use event for it still renders a friendly label
    # and icon in the transcript. Both default false — every existing entry is
    # unchanged.
    system: bool = False
    hidden: bool = False

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
            "system": self.system,
            "hidden": self.hidden,
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
    "create_visualization": ToolMetadata(
        tool_id="create_visualization",
        name="Charts & Graphs",
        description="Create interactive bar, line, and pie charts from data.",
        category=ToolCategory.DATA,
        icon="chart-bar",
    ),

    # --- Platform Self-Service (Account & Usage) ---
    # system=true → always-on plumbing, injected per request as extra_tools
    # (see agents/local_tools/account_tools.py). These entries exist for
    # transcript labeling + the settings-panel hide rule; the runnable tool
    # objects are the closure-bound ones the inference route injects.
    "whoami": ToolMetadata(
        tool_id="whoami",
        name="Account Lookup",
        description="Look up who the signed-in user is on this platform (name, roles, plan).",
        category=ToolCategory.ACCOUNT,
        icon="identification",
        system=True,
        # Pure plumbing: hidden from the settings toggle list, still shown in
        # the transcript when invoked.
        hidden=True,
    ),
    "get_my_quota": ToolMetadata(
        tool_id="get_my_quota",
        name="Usage & Quota",
        description="Report how much of the signed-in user's usage quota is left.",
        category=ToolCategory.ACCOUNT,
        icon="chart-pie",
        system=True,
        # User-beneficial capability: visible-but-locked so users discover they
        # can just ask.
        hidden=False,
    ),
    "get_my_settings": ToolMetadata(
        tool_id="get_my_settings",
        name="My Settings",
        description="Report the signed-in user's account settings, such as their default model.",
        category=ToolCategory.ACCOUNT,
        icon="cog-6-tooth",
        system=True,
        hidden=False,
    ),
    "set_default_model": ToolMetadata(
        tool_id="set_default_model",
        name="Change My Default Model",
        description="Change the signed-in user's default model, to one they are allowed to use. Confirmation-gated write.",
        category=ToolCategory.ACCOUNT,
        icon="adjustments-horizontal",
        system=True,
        # User-beneficial write: visible-but-locked so users can see the agent
        # made the change (and can't turn the capability off themselves).
        hidden=False,
    ),

    # --- Built-in Tools (Utilities) ---
    "calculator": ToolMetadata(
        tool_id="calculator",
        name="Calculator",
        description="Perform mathematical calculations and evaluations.",
        category=ToolCategory.UTILITIES,
        icon="calculator",
    ),
    "ask_user_question": ToolMetadata(
        tool_id="ask_user_question",
        name="Clarifying Questions",
        description="Pause and ask the user multiple-choice questions when a request is ambiguous, then continue with their answer.",
        category=ToolCategory.UTILITIES,
        icon="question-mark-circle",
    ),

    # --- Built-in Tools (Browser) ---
    "browse_web": ToolMetadata(
        tool_id="browse_web",
        name="Web Browser",
        description="Browse the web in a real Chrome browser: navigate pages, read JavaScript-rendered content, fill forms, and click through multi-step flows.",
        category=ToolCategory.SEARCH,
        icon="globe-alt",
    ),

    "request_user_login": ToolMetadata(
        tool_id="request_user_login",
        name="Browser Sign-In",
        description="Hand the browser to the user so they can sign in to a site the agent cannot reach, then continue browsing the authenticated session.",
        category=ToolCategory.SEARCH,
        icon="key",
    ),

    # --- Built-in Tools (Code Interpreter) ---
    "generate_diagram_and_validate": ToolMetadata(
        tool_id="generate_diagram_and_validate",
        name="Code Interpreter",
        description="Generate diagrams, charts, and visualizations using Python code in a sandboxed environment.",
        category=ToolCategory.CODE,
        icon="code-bracket",
    ),

    # --- Built-in Tools (Spreadsheet Analysis) ---
    "list_spreadsheets": ToolMetadata(
        tool_id="list_spreadsheets",
        name="List Spreadsheet Files",
        description="List spreadsheet files available for analysis from the assistant's knowledge base or conversation attachments.",
        category=ToolCategory.DATA,
        icon="folder-open",
    ),
    "analyze_spreadsheet": ToolMetadata(
        tool_id="analyze_spreadsheet",
        name="Spreadsheet Analysis",
        description="Analyze spreadsheet data using Python code. Use for aggregations, comparisons, trends, filtering, and chart generation. For simple factual lookups, use the knowledge base search instead.",
        category=ToolCategory.DATA,
        icon="table-cells",
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
        if not tool_id.startswith(Prefixes.GATEWAY_TOOL):
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
