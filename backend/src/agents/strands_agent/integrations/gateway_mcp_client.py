"""
Gateway MCP Client for AgentCore Gateway Tools
Creates MCP client with SigV4 authentication for Gateway tools
"""

import logging
import os
import boto3
from typing import Optional, List, Callable, Any
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient
from agents.strands_agent.integrations.gateway_auth import get_sigv4_auth, get_gateway_region_from_url

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

        return PaginatedList(filtered_tools, token=paginated_result.pagination_token)


def get_gateway_url_from_ssm(
    project_name: str = "strands-agent-chatbot",
    environment: str = "dev",
    region: str = "us-west-2"
) -> Optional[str]:
    """
    Retrieve Gateway URL from SSM Parameter Store.

    Args:
        project_name: Project name for SSM parameter path
        environment: Environment name (dev, prod, etc.)
        region: AWS region

    Returns:
        Gateway URL or None if not found
    """
    try:
        ssm = boto3.client('ssm', region_name=region)
        response = ssm.get_parameter(
            Name=f'/{project_name}/{environment}/mcp/gateway-url'
        )
        gateway_url = response['Parameter']['Value']
        logger.info(f"✅ Gateway URL retrieved from SSM: {gateway_url}")
        return gateway_url
    except Exception as e:
        logger.warning(f"⚠️  Failed to get Gateway URL from SSM: {e}")
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
    # We'll filter tools manually after listing them
    mcp_client = MCPClient(
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
GATEWAY_ENABLED = os.environ.get('AGENTCORE_GATEWAY_MCP_ENABLED', 'true').lower() == 'true'

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
