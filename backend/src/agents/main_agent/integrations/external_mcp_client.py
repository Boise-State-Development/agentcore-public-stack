"""
External MCP Client for connecting to externally deployed MCP servers.

Creates MCP clients based on tool catalog configuration,
supporting various authentication methods (AWS IAM, API Key, etc.)
"""

import logging
import re
from typing import Optional, List, Any, Callable

from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient

from apis.app_api.tools.models import (
    MCPServerConfig,
    MCPAuthType,
    MCPTransport,
    ToolDefinition,
)
from agents.main_agent.integrations.gateway_auth import get_sigv4_auth

logger = logging.getLogger(__name__)


def extract_region_from_url(url: str) -> Optional[str]:
    """
    Extract AWS region from Lambda Function URL or API Gateway URL.

    Patterns:
    - Lambda: https://xxx.lambda-url.{region}.on.aws/
    - API Gateway: https://xxx.execute-api.{region}.amazonaws.com/

    Args:
        url: The server URL

    Returns:
        AWS region or None if not extractable
    """
    patterns = [
        r"\.lambda-url\.([a-z0-9-]+)\.on\.aws",
        r"\.execute-api\.([a-z0-9-]+)\.amazonaws\.com",
        r"\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return None


def detect_aws_service_from_url(url: str) -> str:
    """
    Detect the AWS service name for SigV4 signing based on URL pattern.

    Different AWS services require different service names for SigV4 signing:
    - Lambda Function URLs: "lambda"
    - API Gateway: "execute-api"
    - AgentCore Gateway: "bedrock-agentcore"

    Args:
        url: The server URL

    Returns:
        AWS service name for SigV4 signing
    """
    if ".lambda-url." in url and ".on.aws" in url:
        return "lambda"
    elif ".execute-api." in url and ".amazonaws.com" in url:
        return "execute-api"
    elif ".bedrock-agentcore." in url and ".amazonaws.com" in url:
        return "bedrock-agentcore"
    else:
        # Default to lambda for unknown patterns (most common for MCP servers)
        logger.warning(f"Could not detect AWS service from URL, defaulting to 'lambda': {url}")
        return "lambda"


def create_external_mcp_client(
    config: MCPServerConfig,
    tool_definition: Optional[ToolDefinition] = None,
) -> Optional[MCPClient]:
    """
    Create an MCP client for an externally deployed MCP server.

    Args:
        config: MCP server configuration from tool catalog
        tool_definition: Optional tool definition for logging

    Returns:
        MCPClient instance or None if configuration is invalid

    Example:
        >>> config = MCPServerConfig(
        ...     server_url="https://xxx.lambda-url.us-west-2.on.aws/",
        ...     transport=MCPTransport.STREAMABLE_HTTP,
        ...     auth_type=MCPAuthType.AWS_IAM,
        ... )
        >>> client = create_external_mcp_client(config)
    """
    if not config.server_url:
        logger.warning("MCP server URL is required")
        return None

    tool_id = tool_definition.tool_id if tool_definition else "unknown"
    logger.info(f"Creating external MCP client for tool: {tool_id}")
    logger.info(f"  Server URL: {config.server_url}")
    logger.info(f"  Transport: {config.transport}")
    logger.info(f"  Auth Type: {config.auth_type}")

    try:
        # Determine authentication
        auth = None
        if config.auth_type == MCPAuthType.AWS_IAM or config.auth_type == "aws-iam":
            # Use AWS IAM SigV4 authentication
            region = config.aws_region
            if not region:
                region = extract_region_from_url(config.server_url)
            if not region:
                region = "us-west-2"  # Default fallback
                logger.warning(f"Could not extract region from URL, using default: {region}")

            # Detect the correct AWS service name for SigV4 signing
            service = detect_aws_service_from_url(config.server_url)

            auth = get_sigv4_auth(service=service, region=region)
            logger.info(f"  Using AWS IAM SigV4 auth for service: {service}, region: {region}")

        elif config.auth_type == MCPAuthType.API_KEY or config.auth_type == "api-key":
            # API key authentication would be handled via headers
            # For now, we'll need to implement a custom auth class
            logger.warning("API Key authentication not yet implemented for external MCP")
            # TODO: Implement API key auth via custom httpx Auth class

        elif config.auth_type == MCPAuthType.BEARER_TOKEN or config.auth_type == "bearer-token":
            logger.warning("Bearer token authentication not yet implemented for external MCP")
            # TODO: Implement bearer token auth

        # Create the MCP client based on transport type
        transport = config.transport
        if isinstance(transport, str):
            transport = MCPTransport(transport)

        if transport == MCPTransport.STREAMABLE_HTTP:
            mcp_client = MCPClient(
                lambda url=config.server_url, auth=auth: streamablehttp_client(
                    url,
                    auth=auth
                )
            )
            logger.info(f"✅ External MCP client created for {tool_id}: {config.server_url}")
            return mcp_client
        else:
            logger.warning(f"Unsupported transport type: {transport}")
            return None

    except Exception as e:
        logger.error(f"Error creating external MCP client for {tool_id}: {e}")
        return None


class ExternalMCPIntegration:
    """
    Manages external MCP client connections for tools configured
    with protocol='mcp_external' in the tool catalog.
    """

    def __init__(self):
        """Initialize external MCP integration."""
        self.clients: dict[str, MCPClient] = {}

    async def load_external_tools(
        self,
        enabled_tool_ids: List[str],
    ) -> List[MCPClient]:
        """
        Load external MCP clients for enabled tools.

        This method queries the tool catalog for tools with protocol='mcp_external'
        and creates MCP clients for them.

        Args:
            enabled_tool_ids: List of enabled tool IDs

        Returns:
            List of MCPClient instances to add to the agent's tools
        """
        from apis.app_api.tools.repository import get_tool_catalog_repository

        clients = []
        repository = get_tool_catalog_repository()

        for tool_id in enabled_tool_ids:
            # Skip if not an external MCP tool
            if tool_id in self.clients:
                clients.append(self.clients[tool_id])
                continue

            try:
                tool = await repository.get_tool(tool_id)
                if not tool:
                    continue

                # Check if this is an external MCP tool
                if tool.protocol != "mcp_external":
                    continue

                if not tool.mcp_config:
                    logger.warning(f"Tool {tool_id} has protocol=mcp_external but no mcp_config")
                    continue

                # Create MCP client
                client = create_external_mcp_client(tool.mcp_config, tool)
                if client:
                    self.clients[tool_id] = client
                    clients.append(client)
                    logger.info(f"✅ Loaded external MCP tool: {tool_id}")

            except Exception as e:
                logger.error(f"Error loading external MCP tool {tool_id}: {e}")
                continue

        return clients

    def get_client(self, tool_id: str) -> Optional[MCPClient]:
        """Get a specific MCP client by tool ID."""
        return self.clients.get(tool_id)

    def add_to_tool_list(self, tools: List[Any]) -> List[Any]:
        """
        Add all loaded external MCP clients to the tool list.

        Args:
            tools: Existing list of tools

        Returns:
            Updated tool list with MCP clients added
        """
        for client in self.clients.values():
            if client not in tools:
                tools.append(client)
        return tools


# Global instance
_external_mcp_integration: Optional[ExternalMCPIntegration] = None


def get_external_mcp_integration() -> ExternalMCPIntegration:
    """Get or create the global ExternalMCPIntegration instance."""
    global _external_mcp_integration
    if _external_mcp_integration is None:
        _external_mcp_integration = ExternalMCPIntegration()
    return _external_mcp_integration
