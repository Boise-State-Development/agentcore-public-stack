"""
External MCP Client for connecting to externally deployed MCP servers.

Creates MCP clients based on tool catalog configuration,
supporting various authentication methods (AWS IAM, API Key, OAuth, etc.)

OAuth Support:
    When a tool has `requires_oauth_provider` set, the MCP client will
    automatically inject the user's OAuth token into requests. This requires
    per-user client instances since tokens are user-specific.
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
from agents.main_agent.integrations.oauth_auth import (
    OAuthBearerAuth,
    CompositeAuth,
    create_oauth_bearer_auth,
)

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
    oauth_token: Optional[str] = None,
) -> Optional[MCPClient]:
    """
    Create an MCP client for an externally deployed MCP server.

    Args:
        config: MCP server configuration from tool catalog
        tool_definition: Optional tool definition for logging
        oauth_token: Optional OAuth token to include in requests (for user-specific auth)

    Returns:
        MCPClient instance or None if configuration is invalid

    Example:
        >>> config = MCPServerConfig(
        ...     server_url="https://xxx.lambda-url.us-west-2.on.aws/",
        ...     transport=MCPTransport.STREAMABLE_HTTP,
        ...     auth_type=MCPAuthType.AWS_IAM,
        ... )
        >>> client = create_external_mcp_client(config)

        # With OAuth token for user-specific access:
        >>> client = create_external_mcp_client(config, oauth_token="user_access_token")
    """
    if not config.server_url:
        logger.warning("MCP server URL is required")
        return None

    tool_id = tool_definition.tool_id if tool_definition else "unknown"
    requires_oauth = tool_definition.requires_oauth_provider if tool_definition else None
    logger.info(f"Creating external MCP client for tool: {tool_id}")
    logger.info(f"  Server URL: {config.server_url}")
    logger.info(f"  Transport: {config.transport}")
    logger.info(f"  Auth Type: {config.auth_type}")
    if requires_oauth:
        logger.info(f"  Requires OAuth Provider: {requires_oauth}")
        logger.info(f"  OAuth Token Provided: {bool(oauth_token)}")

    try:
        # Build list of auth handlers (may combine multiple)
        auth_handlers = []

        # When an OAuth token is provided, use it exclusively as the auth method.
        # SigV4 and OAuth both use the Authorization header and cannot coexist —
        # SigV4 sets "AWS4-HMAC-SHA256 ..." while OAuth sets "Bearer ...".
        # The Lambda Function URL auth type should be NONE for OAuth-authenticated tools.
        if oauth_token:
            oauth_auth = create_oauth_bearer_auth(token=oauth_token)
            auth_handlers.append(oauth_auth)
            logger.info("  Using OAuth Bearer token auth (skipping SigV4)")

        # AWS IAM SigV4 authentication (for Lambda/API Gateway without OAuth)
        elif config.auth_type == MCPAuthType.AWS_IAM or config.auth_type == "aws-iam":
            region = config.aws_region
            if not region:
                region = extract_region_from_url(config.server_url)
            if not region:
                region = "us-west-2"  # Default fallback
                logger.warning(f"Could not extract region from URL, using default: {region}")

            # Detect the correct AWS service name for SigV4 signing
            service = detect_aws_service_from_url(config.server_url)

            sigv4_auth = get_sigv4_auth(service=service, region=region)
            auth_handlers.append(sigv4_auth)
            logger.info(f"  Using AWS IAM SigV4 auth for service: {service}, region: {region}")

        elif config.auth_type == MCPAuthType.API_KEY or config.auth_type == "api-key":
            # API key authentication would be handled via headers
            logger.warning("API Key authentication not yet implemented for external MCP")
            # TODO: Implement API key auth via custom httpx Auth class

        elif config.auth_type == MCPAuthType.BEARER_TOKEN or config.auth_type == "bearer-token":
            # Static bearer token (not user-specific OAuth)
            logger.warning("Static bearer token authentication not yet implemented for external MCP")
            # TODO: Implement static bearer token auth

        # Combine auth handlers
        auth = None
        if len(auth_handlers) == 1:
            auth = auth_handlers[0]
        elif len(auth_handlers) > 1:
            auth = CompositeAuth(*auth_handlers)
            logger.info(f"  Using composite auth with {len(auth_handlers)} handlers")

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

    OAuth Support:
        Tools with `requires_oauth_provider` set will have their MCP clients
        created with the user's OAuth token injected. Since tokens are user-specific,
        OAuth-enabled tools use a per-user cache key.
    """

    def __init__(self):
        """Initialize external MCP integration."""
        # Cache key: tool_id for non-OAuth tools, "user_id:tool_id" for OAuth tools
        self.clients: dict[str, MCPClient] = {}

    def _get_cache_key(self, tool_id: str, user_id: Optional[str], requires_oauth: bool) -> str:
        """Get the cache key for a tool client."""
        if requires_oauth and user_id:
            return f"{user_id}:{tool_id}"
        return tool_id

    async def _get_oauth_token(
        self,
        user_id: str,
        provider_id: str,
    ) -> Optional[str]:
        """
        Get decrypted OAuth token for a user and provider.

        Args:
            user_id: The user's ID
            provider_id: The OAuth provider ID

        Returns:
            Decrypted access token or None if not connected
        """
        try:
            from apis.app_api.oauth.service import get_oauth_service

            oauth_service = get_oauth_service()
            token = await oauth_service.get_decrypted_token(user_id, provider_id)
            return token
        except Exception as e:
            logger.error(f"Error getting OAuth token for user {user_id}, provider {provider_id}: {e}")
            return None

    async def load_external_tools(
        self,
        enabled_tool_ids: List[str],
        user_id: Optional[str] = None,
    ) -> List[MCPClient]:
        """
        Load external MCP clients for enabled tools.

        This method queries the tool catalog for tools with protocol='mcp_external'
        and creates MCP clients for them. For tools requiring OAuth, the user's
        token is retrieved and injected into the client.

        Args:
            enabled_tool_ids: List of enabled tool IDs
            user_id: User ID for OAuth token retrieval (required for OAuth-enabled tools)

        Returns:
            List of MCPClient instances to add to the agent's tools
        """
        from apis.app_api.tools.repository import get_tool_catalog_repository

        clients = []
        repository = get_tool_catalog_repository()

        for tool_id in enabled_tool_ids:
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

                # Check if tool requires OAuth
                requires_oauth = bool(tool.requires_oauth_provider)
                cache_key = self._get_cache_key(tool_id, user_id, requires_oauth)

                # Check cache
                if cache_key in self.clients:
                    clients.append(self.clients[cache_key])
                    continue

                # Get OAuth token if required
                oauth_token = None
                if requires_oauth:
                    if not user_id:
                        logger.warning(
                            f"Tool {tool_id} requires OAuth provider '{tool.requires_oauth_provider}' "
                            "but no user_id provided"
                        )
                        continue

                    oauth_token = await self._get_oauth_token(
                        user_id=user_id,
                        provider_id=tool.requires_oauth_provider,
                    )

                    if not oauth_token:
                        logger.warning(
                            f"User {user_id} not connected to OAuth provider "
                            f"'{tool.requires_oauth_provider}' for tool {tool_id}"
                        )
                        # Still create the client - it will fail gracefully when used
                        # The MCP server should return an appropriate error

                # Create MCP client with optional OAuth token
                client = create_external_mcp_client(
                    config=tool.mcp_config,
                    tool_definition=tool,
                    oauth_token=oauth_token,
                )

                if client:
                    self.clients[cache_key] = client
                    clients.append(client)
                    logger.info(
                        f"✅ Loaded external MCP tool: {tool_id}"
                        f"{' (with OAuth)' if oauth_token else ''}"
                    )

            except Exception as e:
                logger.error(f"Error loading external MCP tool {tool_id}: {e}")
                continue

        return clients

    def get_client(self, tool_id: str, user_id: Optional[str] = None) -> Optional[MCPClient]:
        """
        Get a specific MCP client by tool ID.

        Args:
            tool_id: The tool ID
            user_id: User ID for OAuth-enabled tools

        Returns:
            MCPClient or None if not found
        """
        # Try user-specific key first, then generic
        if user_id:
            user_key = f"{user_id}:{tool_id}"
            if user_key in self.clients:
                return self.clients[user_key]
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

    def clear_user_clients(self, user_id: str) -> None:
        """
        Clear cached MCP clients for a specific user.

        Call this when a user disconnects from an OAuth provider
        to ensure fresh clients are created on next use.

        Args:
            user_id: User ID to clear clients for
        """
        keys_to_remove = [
            key for key in self.clients.keys()
            if key.startswith(f"{user_id}:")
        ]
        for key in keys_to_remove:
            del self.clients[key]

        if keys_to_remove:
            logger.info(f"Cleared {len(keys_to_remove)} cached MCP clients for user {user_id}")


# Global instance
_external_mcp_integration: Optional[ExternalMCPIntegration] = None


def get_external_mcp_integration() -> ExternalMCPIntegration:
    """Get or create the global ExternalMCPIntegration instance."""
    global _external_mcp_integration
    if _external_mcp_integration is None:
        _external_mcp_integration = ExternalMCPIntegration()
    return _external_mcp_integration
