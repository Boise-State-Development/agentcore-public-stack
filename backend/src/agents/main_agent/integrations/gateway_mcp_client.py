"""
Gateway MCP Client for AgentCore Gateway Tools
Creates MCP client with SigV4 authentication for Gateway tools
"""

import logging
import os
from typing import Optional, List, Callable, Any
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient
from agents.main_agent.config.constants import EnvVars, Defaults
from agents.main_agent.integrations.gateway_auth import get_sigv4_auth, get_gateway_region_from_url
from apis.shared.tools.gateway_identity import resolve_gateway_id, gateway_url_from_id
from agents.main_agent.integrations.mcp_apps import (
    UICapableMCPClient,
    ensure_ui_extension_session_patch,
    record_and_filter_ui_tools,
)
from agents.main_agent.integrations.mcp_tool_folding import drop_folded_tools

logger = logging.getLogger(__name__)


class FilteredMCPClient(MCPClient):
    """
    MCPClient wrapper that filters tools based on enabled tool IDs.
    This allows us to use Managed Integration while still filtering tools.

    The client automatically maintains the MCP session for the lifetime
    of the ChatbotAgent instance, ensuring tools remain accessible.
    """

    def __init__(
        self,
        client_factory: Callable[[], Any],
        enabled_tool_ids: List[str],
        prefix: str = "gateway"
    ):
        """
        Initialize filtered MCP client.

        Args:
            client_factory: Factory function to create MCP client transport
            enabled_tool_ids: List of tool IDs that should be enabled
            prefix: Prefix used for tool IDs (default: 'gateway')
        """
        # Advertise the MCP Apps UI extension on this client's initialize
        # (inert unless AGENTCORE_MCP_APPS_HOST_ENABLED=true).
        ensure_ui_extension_session_patch()
        super().__init__(client_factory)
        self.enabled_tool_ids = enabled_tool_ids
        self.prefix = prefix
        self._session_started = False
        logger.info(f"FilteredMCPClient created with {len(enabled_tool_ids)} enabled tool IDs")

    def __enter__(self):
        """Start MCP session when entering context"""
        logger.info("Starting FilteredMCPClient session")
        result = super().__enter__()
        self._session_started = True
        return result

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close MCP session when exiting context"""
        logger.info("Closing FilteredMCPClient session")
        self._session_started = False
        return super().__exit__(exc_type, exc_val, exc_tb)

    def ensure_session(self):
        """Deprecated: Session is managed by Strands ToolRegistry."""
        pass

    def list_tools_sync(self, *args, **kwargs):
        """List tools from Gateway and filter based on enabled_tool_ids."""
        from strands.types import PaginatedList

        paginated_result = super().list_tools_sync()

        filtered_tools = [
            tool for tool in paginated_result
            if any(
                enabled_id.replace(f"{self.prefix}_", "") == tool.tool_name or
                tool.tool_name in enabled_id
                for enabled_id in self.enabled_tool_ids
            )
        ]

        logger.info(f"✅ Filtered {len(filtered_tools)} tools from {len(paginated_result)} available")
        logger.info(f"   Enabled tool IDs: {self.enabled_tool_ids}")
        logger.info(f"   Filtered tool names: {[t.tool_name for t in filtered_tools]}")

        # Record `_meta.ui` into the catalog and drop app-only tools so the
        # model never sees them (no-op unless the host flag is enabled).
        # `self` is the client hosting these tools — recorded so PR #3 can
        # issue `resources/read` against it.
        filtered_tools = record_and_filter_ui_tools(filtered_tools, client=self)

        # Drop tools folded behind a skill's meta-tools (PR-6b). No-op unless a
        # SkillAgent registered a fold set for this client. The tools stay
        # executable via skill_executor → this client's call_tool_sync.
        filtered_tools = drop_folded_tools(self, filtered_tools)

        return PaginatedList(filtered_tools, token=paginated_result.pagination_token)


def get_gateway_url_from_ssm(region: Optional[str] = None) -> Optional[str]:
    """Resolve the centralized Gateway's MCP URL from infra config.

    Uses the SAME resolution as the admin-side ``GatewayTargetService``
    (``AGENTCORE_GATEWAY_ID`` override → SSM ``/{PROJECT_PREFIX}/gateway/id``,
    published by the gateway CDK construct), so the agent connects to exactly
    the gateway that admin-registered targets live on. Previously this read a
    hardcoded ``/strands-agent-chatbot/dev/mcp/gateway-url``, which pointed at a
    *different* gateway than the one the admin form manages.

    Returns None (and logs) on failure so the agent degrades gracefully instead
    of failing the turn when no gateway is configured.
    """
    try:
        gateway_id = resolve_gateway_id(region=region)
        gateway_url = gateway_url_from_id(gateway_id, region=region)
        logger.info(f"✅ Gateway URL resolved from infra config: {gateway_url}")
        return gateway_url
    except Exception as e:
        logger.warning(f"⚠️  Failed to resolve Gateway URL from infra config: {e}")
        return None


def create_gateway_mcp_client(
    gateway_url: Optional[str] = None,
    prefix: str = "gateway",
    tool_filters: Optional[dict] = None,
    region: Optional[str] = None
) -> Optional[MCPClient]:
    """
    Create MCP client for AgentCore Gateway with SigV4 authentication.

    Args:
        gateway_url: Gateway URL. If None, retrieves from SSM Parameter Store.
        prefix: Prefix for tool names (default: 'gateway')
        tool_filters: Tool filtering configuration (allowed/rejected lists)
        region: AWS region. If None, extracts from gateway_url or uses default.

    Returns:
        MCPClient instance or None if Gateway URL not available

    Example:
        >>> # Create client with all tools
        >>> client = create_gateway_mcp_client()
        >>>
        >>> # Create client with tool filtering
        >>> client = create_gateway_mcp_client(
        ...     tool_filters={"allowed": ["wikipedia_search", "arxiv_search"]}
        ... )
        >>>
        >>> # Use with Strands Agent (Managed approach - Experimental)
        >>> agent = Agent(tools=[client])
        >>>
        >>> # Or manual approach
        >>> with client:
        ...     tools = client.list_tools_sync()
        ...     agent = Agent(tools=tools)
    """
    # Get Gateway URL from SSM if not provided
    if not gateway_url:
        gateway_url = get_gateway_url_from_ssm()
        if not gateway_url:
            logger.warning("⚠️  Gateway URL not available. Gateway tools will not be loaded.")
            return None

    # Extract region from URL if not provided
    if not region:
        region = get_gateway_region_from_url(gateway_url)

    # Create SigV4 auth for Gateway
    auth = get_sigv4_auth(region=region)

    # Create MCP client with streamable HTTP transport
    # Note: prefix and tool_filters are no longer supported in MCPClient constructor
    # We'll filter tools manually after listing them.
    # UICapableMCPClient advertises the MCP Apps UI extension on initialize and
    # records/filters `_meta.ui` tools (inert unless the host flag is enabled).
    mcp_client = UICapableMCPClient(
        lambda: streamablehttp_client(
            gateway_url,
            auth=auth  # httpx Auth class for automatic SigV4 signing
        )
    )

    logger.info(f"✅ Gateway MCP client created: {gateway_url}")
    logger.info(f"   Region: {region}")
    logger.info(f"   Note: Prefix '{prefix}' will be applied manually")
    if tool_filters:
        logger.info(f"   Note: Filters {tool_filters} will be applied manually")

    return mcp_client


def create_filtered_gateway_client(
    enabled_tool_ids: List[str],
    prefix: str = "gateway"
) -> Optional[FilteredMCPClient]:
    """
    Create Gateway MCP client with tool filtering based on enabled tool IDs.

    This is used to dynamically filter Gateway tools based on user's
    tool selection in the UI sidebar.

    Args:
        enabled_tool_ids: List of tool IDs that are enabled by user
                         e.g., ["gateway_wikipedia-search___wikipedia_search", "gateway_arxiv-search___arxiv_search"]
        prefix: Prefix used for Gateway tools (default: 'gateway')

    Returns:
        FilteredMCPClient with filtered tools or None if no Gateway tools enabled

    Example:
        >>> # User enabled only Wikipedia tools
        >>> enabled = ["gateway_wikipedia-search___wikipedia_search", "gateway_wikipedia-get-article___wikipedia_get_article"]
        >>> client = create_filtered_gateway_client(enabled)
        >>>
        >>> # Use with Agent (Managed Integration)
        >>> agent = Agent(tools=[client])
    """
    # Filter to only Gateway tool IDs
    gateway_tool_ids = [tid for tid in enabled_tool_ids if tid.startswith(f"{prefix}_")]

    if not gateway_tool_ids:
        logger.info("No Gateway tools enabled")
        return None

    # Get Gateway URL from SSM
    gateway_url = get_gateway_url_from_ssm()
    if not gateway_url:
        logger.warning("⚠️  Gateway URL not available. Gateway tools will not be loaded.")
        return None

    # Extract region from URL
    region = get_gateway_region_from_url(gateway_url)

    # Create SigV4 auth for Gateway
    auth = get_sigv4_auth(region=region)

    # Create FilteredMCPClient with tool filtering
    logger.info(f"Creating FilteredMCPClient with {len(gateway_tool_ids)} enabled tool IDs")

    mcp_client = FilteredMCPClient(
        lambda: streamablehttp_client(
            gateway_url,
            auth=auth  # httpx Auth class for automatic SigV4 signing
        ),
        enabled_tool_ids=gateway_tool_ids,
        prefix=prefix
    )

    logger.info(f"✅ FilteredMCPClient created: {gateway_url}")
    logger.info(f"   Region: {region}")
    logger.info(f"   Enabled tool IDs: {gateway_tool_ids}")

    return mcp_client


# Environment variable control
GATEWAY_ENABLED = os.environ.get(EnvVars.GATEWAY_MCP_ENABLED, str(Defaults.GATEWAY_MCP_ENABLED).lower()).lower() == 'true'

def get_gateway_client_if_enabled(
    enabled_tool_ids: Optional[List[str]] = None
) -> Optional[MCPClient]:
    """
    Get Gateway MCP client if enabled via environment variable.

    Args:
        enabled_tool_ids: List of enabled tool IDs for filtering

    Returns:
        MCPClient or None if disabled or no tools enabled
    """
    if not GATEWAY_ENABLED:
        logger.info("Gateway MCP is disabled via AGENTCORE_GATEWAY_MCP_ENABLED=false")
        return None

    if enabled_tool_ids:
        return create_filtered_gateway_client(enabled_tool_ids)
    else:
        return create_gateway_mcp_client()
