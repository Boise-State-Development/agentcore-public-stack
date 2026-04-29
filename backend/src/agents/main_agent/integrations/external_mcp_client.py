"""
External MCP Client for connecting to externally deployed MCP servers.

Creates MCP clients based on tool catalog configuration,
supporting various authentication methods (AWS IAM, API Key, OAuth, etc.)

OAuth Support:
    Tools with `requires_oauth_provider` set get an `OAuthBearerAuth` whose
    token is resolved lazily on every MCP request via `oauth_token_cache`.
    The cache is warmed by `OAuthConsentHook` (which also pauses the agent
    via Strands interrupts when consent is needed). This module never
    pre-flights OAuth — the agent loads tools optimistically; the hook
    gates execution.
"""

import logging
import re
from typing import Any, Callable, Optional, List

from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient

from apis.app_api.tools.models import (
    MCPServerConfig,
    MCPAuthType,
    MCPTransport,
    ToolDefinition,
)
from agents.main_agent.integrations import oauth_token_cache
from agents.main_agent.integrations.gateway_auth import get_sigv4_auth
from agents.main_agent.integrations.oauth_auth import (
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
    token_provider: Optional[Callable[[], Optional[str]]] = None,
) -> Optional[MCPClient]:
    """
    Create an MCP client for an externally deployed MCP server.

    Pass either `oauth_token` (static, for OIDC forwarding) or `token_provider`
    (callable, for OAuth tokens that the consent hook resolves lazily).

    Args:
        config: MCP server configuration from tool catalog
        tool_definition: Optional tool definition for logging
        oauth_token: Optional static token (used for OIDC forwarding)
        token_provider: Optional callable returning the current token

    Returns:
        MCPClient instance or None if configuration is invalid
    """
    if not config.server_url:
        logger.warning("MCP server URL is required")
        return None

    tool_id = tool_definition.tool_id if tool_definition else "unknown"
    requires_oauth = tool_definition.requires_oauth_provider if tool_definition else None
    has_static_token = bool(oauth_token)
    has_provider = bool(token_provider)
    logger.info(f"Creating external MCP client for tool: {tool_id}")
    logger.debug(f"  Transport: {config.transport}")
    logger.debug(f"  Auth Type: {config.auth_type}")
    if requires_oauth:
        logger.debug("  Requires OAuth Provider: yes")
        logger.debug(f"  Token mode: {'provider' if has_provider else 'static' if has_static_token else 'none'}")

    try:
        # Build list of auth handlers (may combine multiple)
        auth_handlers = []

        # OAuth/OIDC bearer auth takes precedence over SigV4. SigV4 and OAuth both
        # use the Authorization header and cannot coexist (SigV4 sets
        # "AWS4-HMAC-SHA256 ...", OAuth sets "Bearer ..."). Lambda Function URLs
        # backing OAuth-authenticated MCP servers must use auth_type=NONE.
        if token_provider:
            auth_handlers.append(create_oauth_bearer_auth(token_provider=token_provider))
            logger.debug("  Using OAuth Bearer token provider (lazy)")
        elif oauth_token:
            auth_handlers.append(create_oauth_bearer_auth(token=oauth_token))
            logger.debug("  Using OAuth Bearer token (static)")

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
            logger.debug(f"  Using AWS IAM SigV4 auth for service: {service}, region: {region}")

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
            logger.debug(f"  Using composite auth with {len(auth_handlers)} handlers")

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
        For OAuth-gated tools, clients are created with a lazy token provider
        that reads from the per-process token cache at request time. The
        cache is populated by `OAuthConsentHook`, which gates execution by
        raising a Strands interrupt when no token is available yet.

        This integration also maintains an MCPClient -> provider_id map so
        the hook can look up which provider a tool's MCP server requires
        without coupling on tool names.
    """

    def __init__(self):
        # Cache key: tool_id for non-OAuth tools, "user_id:tool_id" for OAuth tools
        self.clients: dict[str, MCPClient] = {}
        # Parallel map of cache_key -> tool updated_at at the time the
        # client was built. On lookup we compare against the tool's
        # current updated_at; mismatch means the admin edited the tool
        # (URL, auth, provider, etc.) and we must rebuild the client.
        self._client_versions: dict[str, str] = {}
        # MCPClient object identity -> provider_id, populated alongside `clients`.
        # Consumed by OAuthConsentHook via `provider_for_client`.
        self._provider_for_client_id: dict[int, str] = {}
        # MCPClient identity -> set of MCP-server-exposed tool names that the
        # admin has flagged needs_approval=True. Snapshotted at client-build
        # time from the tool's MCPServerConfig. Consumed by the per-tool
        # approval hook, which keys off the parent ToolDefinition rather
        # than a global tool-name list (so two MCP servers can share a tool
        # name and only one is gated).
        self._approval_names_for_client_id: dict[int, set[str]] = {}

    def _get_cache_key(self, tool_id: str, user_id: Optional[str], requires_oauth: bool) -> str:
        if requires_oauth and user_id:
            return f"{user_id}:{tool_id}"
        return tool_id

    def provider_for_client(self, client: Any) -> Optional[str]:
        """Return the OAuth provider_id backing `client`, or None."""
        return self._provider_for_client_id.get(id(client))

    def approval_names_for_client(self, client: Any) -> set[str]:
        """Return the set of tool names on `client` flagged needs_approval."""
        return self._approval_names_for_client_id.get(id(client), set())

    async def load_external_tools(
        self,
        enabled_tool_ids: List[str],
        user_id: Optional[str] = None,
        auth_token: Optional[str] = None,
    ) -> List[MCPClient]:
        """
        Load external MCP clients for enabled tools.

        For OAuth-gated tools, the client is created with a token provider
        that reads from `oauth_token_cache` lazily at request time. Token
        acquisition + consent prompting happen in `OAuthConsentHook` at
        tool-call time, not here. For tools with `forward_auth_token`, the
        user's OIDC token is injected statically.

        Args:
            enabled_tool_ids: List of enabled tool IDs
            user_id: User ID (required for OAuth-gated and OIDC-forwarded tools)
            auth_token: Raw OIDC token for forwarding

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

                if tool.protocol != "mcp_external":
                    continue

                if not tool.mcp_config:
                    logger.warning(f"Tool {tool_id} has protocol=mcp_external but no mcp_config")
                    continue

                forward_auth = bool(getattr(tool, "forward_auth_token", False))
                requires_oauth = bool(tool.requires_oauth_provider)
                requires_user_auth = forward_auth or requires_oauth

                cache_key = self._get_cache_key(tool_id, user_id, requires_user_auth)
                tool_version = (
                    tool.updated_at.isoformat() + "Z" if tool.updated_at else ""
                )

                if (
                    cache_key in self.clients
                    and self._client_versions.get(cache_key) == tool_version
                ):
                    clients.append(self.clients[cache_key])
                    continue

                # Stale entry — admin edited this tool since the client
                # was built. Drop it so the block below creates a fresh
                # client with the current config.
                if cache_key in self.clients:
                    stale = self.clients.pop(cache_key)
                    self._client_versions.pop(cache_key, None)
                    self._provider_for_client_id.pop(id(stale), None)
                    self._approval_names_for_client_id.pop(id(stale), None)

                static_token: Optional[str] = None
                token_provider: Optional[Callable[[], Optional[str]]] = None
                provider_id: Optional[str] = None

                if forward_auth:
                    if not auth_token:
                        logger.warning(
                            f"Tool {tool_id} has forward_auth_token=true but no auth_token provided"
                        )
                    else:
                        static_token = auth_token
                        logger.info(f"Using OIDC token forwarding for tool {tool_id}")

                elif requires_oauth:
                    if not user_id:
                        logger.warning(
                            f"Tool {tool_id} requires OAuth provider '{tool.requires_oauth_provider}' "
                            "but no user_id provided"
                        )
                        continue

                    provider_id = tool.requires_oauth_provider
                    # Bind user_id and provider_id at closure time so the
                    # provider stays valid for the client's lifetime.
                    token_provider = (
                        lambda u=user_id, p=provider_id: oauth_token_cache.get(u, p)
                    )

                client = create_external_mcp_client(
                    config=tool.mcp_config,
                    tool_definition=tool,
                    oauth_token=static_token,
                    token_provider=token_provider,
                )

                if client:
                    # Pre-flight the MCP session so a single unreachable
                    # server (e.g. a connector that isn't running locally)
                    # drops out of the registry instead of failing the
                    # whole turn when Strands later calls load_tools().
                    # On success this also primes the client's tool cache,
                    # so Strands' subsequent load_tools() is a no-op.
                    try:
                        await client.load_tools()
                    except Exception as exc:
                        logger.warning(
                            f"Skipping external MCP tool {tool_id}: "
                            f"failed to start client ({exc})"
                        )
                        continue

                    self.clients[cache_key] = client
                    self._client_versions[cache_key] = tool_version
                    if provider_id:
                        self._provider_for_client_id[id(client)] = provider_id
                    approval_names = tool.mcp_config.approval_required_names()
                    if approval_names:
                        self._approval_names_for_client_id[id(client)] = approval_names
                    clients.append(client)
                    auth_label = (
                        " (with OIDC forwarding)" if forward_auth and static_token
                        else " (OAuth)" if provider_id
                        else ""
                    )
                    logger.info(f"✅ Loaded external MCP tool: {tool_id}{auth_label}")

            except Exception as e:
                logger.error(f"Error loading external MCP tool {tool_id}: {e}")
                continue

        return clients

    def get_client(self, tool_id: str, user_id: Optional[str] = None) -> Optional[MCPClient]:
        if user_id:
            user_key = f"{user_id}:{tool_id}"
            if user_key in self.clients:
                return self.clients[user_key]
        return self.clients.get(tool_id)

    def add_to_tool_list(self, tools: List[Any]) -> List[Any]:
        for client in self.clients.values():
            if client not in tools:
                tools.append(client)
        return tools

    def clear_user_clients(self, user_id: str) -> None:
        """
        Clear cached MCP clients for a specific user.

        Call this when a user disconnects from an OAuth provider so the next
        agent build creates fresh clients (and the token cache miss forces a
        new consent flow).
        """
        keys_to_remove = [
            key for key in self.clients.keys()
            if key.startswith(f"{user_id}:")
        ]
        for key in keys_to_remove:
            client = self.clients.pop(key)
            self._client_versions.pop(key, None)
            self._provider_for_client_id.pop(id(client), None)
            self._approval_names_for_client_id.pop(id(client), None)

        if keys_to_remove:
            logger.info(f"Cleared {len(keys_to_remove)} cached MCP clients for user {user_id}")

    def clear_tool_clients(self, tool_id: str) -> None:
        """
        Clear cached MCP clients for a specific tool across all users.

        Call this when a tool's config changes (e.g. admin updates the MCP
        server URL) so the next agent build reconnects using the fresh
        config. Without this, clients cached at process start continue to
        point at the old URL for the lifetime of the process.
        """
        keys_to_remove = [
            key for key in self.clients.keys()
            if key == tool_id or key.endswith(f":{tool_id}")
        ]
        for key in keys_to_remove:
            client = self.clients.pop(key)
            self._client_versions.pop(key, None)
            self._provider_for_client_id.pop(id(client), None)
            self._approval_names_for_client_id.pop(id(client), None)

        if keys_to_remove:
            logger.info(f"Cleared {len(keys_to_remove)} cached MCP clients for tool {tool_id}")


_external_mcp_integration: Optional[ExternalMCPIntegration] = None


def get_external_mcp_integration() -> ExternalMCPIntegration:
    """Get or create the global ExternalMCPIntegration instance."""
    global _external_mcp_integration
    if _external_mcp_integration is None:
        _external_mcp_integration = ExternalMCPIntegration()
    return _external_mcp_integration
