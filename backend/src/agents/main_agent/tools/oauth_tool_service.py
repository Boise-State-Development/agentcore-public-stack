"""
OAuth Tool Service - Enables tools to securely access user OAuth tokens

This service provides a clean interface for tools to:
1. Check if a user has connected to an OAuth provider
2. Retrieve decrypted access tokens for API calls
3. Handle token refresh automatically
4. Generate connection URLs for user guidance

Usage in tools:
    from agents.main_agent.tools.oauth_tool_service import get_oauth_tool_service

    @tool(context=True)
    async def my_oauth_tool(query: str, tool_context: ToolContext) -> dict:
        oauth_service = get_oauth_tool_service()

        # Get user_id from session manager
        user_id = tool_context.agent._session_manager.user_id

        # Get token for this provider
        result = await oauth_service.get_token_for_tool(
            user_id=user_id,
            provider_id="google_workspace"
        )

        if not result.connected:
            return result.not_connected_response()

        # Use the token
        headers = {"Authorization": f"Bearer {result.access_token}"}
        # ... make API calls
"""
import logging
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


@dataclass
class OAuthTokenResult:
    """Result of requesting an OAuth token for a tool."""

    connected: bool
    """Whether the user is connected to the provider."""

    access_token: Optional[str] = None
    """The decrypted access token (if connected)."""

    provider_id: str = ""
    """The provider ID requested."""

    provider_name: str = ""
    """Human-readable provider name."""

    error: Optional[str] = None
    """Error message if token retrieval failed."""

    needs_reauth: bool = False
    """Whether the user needs to re-authorize (expired/revoked)."""

    def not_connected_response(self, tool_name: str = "this tool") -> dict:
        """
        Generate a user-friendly response when not connected.

        Returns a dict suitable for returning from a tool.
        """
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:4200")
        connect_url = f"{frontend_url}/settings/connections"

        if self.needs_reauth:
            message = f"""⚠️ **Re-authorization Required**

Your connection to **{self.provider_name}** has expired or been revoked.

Please reconnect to continue using {tool_name}:

👉 [Reconnect to {self.provider_name}]({connect_url})

After reconnecting, try your request again."""
        else:
            message = f"""🔗 **Connection Required**

To use {tool_name}, you need to connect your **{self.provider_name}** account.

This allows me to securely access your data on your behalf.

👉 [Connect {self.provider_name}]({connect_url})

After connecting, try your request again."""

        return {
            "status": "not_connected",
            "content": [{"text": message}],
            "requires_oauth": True,
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "connect_url": connect_url,
            "needs_reauth": self.needs_reauth,
        }


class OAuthToolService:
    """
    Service for tools to access OAuth tokens.

    This service wraps the OAuth service and provides a simplified
    interface optimized for tool usage.
    """

    def __init__(self):
        self._oauth_service = None
        self._provider_repo = None

    async def _get_oauth_service(self):
        """Lazy-load OAuth service to avoid circular imports."""
        if self._oauth_service is None:
            from apis.app_api.oauth.service import get_oauth_service
            self._oauth_service = get_oauth_service()
        return self._oauth_service

    async def _get_provider_repo(self):
        """Lazy-load provider repository."""
        if self._provider_repo is None:
            from apis.app_api.oauth.provider_repository import get_provider_repository
            self._provider_repo = get_provider_repository()
        return self._provider_repo

    async def get_token_for_tool(
        self,
        user_id: str,
        provider_id: str,
    ) -> OAuthTokenResult:
        """
        Get an OAuth token for a tool to use.

        Args:
            user_id: The user's ID (from session manager)
            provider_id: The OAuth provider ID (e.g., "google_workspace")

        Returns:
            OAuthTokenResult with token or connection guidance
        """
        try:
            oauth_service = await self._get_oauth_service()
            provider_repo = await self._get_provider_repo()

            # Get provider info for display name
            provider = await provider_repo.get_provider(provider_id)
            provider_name = provider.display_name if provider else provider_id

            # Try to get the token
            token = await oauth_service.get_decrypted_token(
                user_id=user_id,
                provider_id=provider_id
            )

            if token:
                logger.info(f"Retrieved OAuth token for user {user_id}, provider {provider_id}")
                return OAuthTokenResult(
                    connected=True,
                    access_token=token,
                    provider_id=provider_id,
                    provider_name=provider_name,
                )

            # Check if user has a connection but needs re-auth
            from apis.app_api.oauth.token_repository import get_token_repository
            token_repo = get_token_repository()
            user_token = await token_repo.get_user_token(user_id, provider_id)

            if user_token and user_token.status in ("expired", "needs_reauth", "revoked"):
                logger.info(f"User {user_id} needs re-auth for provider {provider_id}")
                return OAuthTokenResult(
                    connected=False,
                    provider_id=provider_id,
                    provider_name=provider_name,
                    needs_reauth=True,
                    error=f"Token {user_token.status}",
                )

            # User not connected
            logger.info(f"User {user_id} not connected to provider {provider_id}")
            return OAuthTokenResult(
                connected=False,
                provider_id=provider_id,
                provider_name=provider_name,
            )

        except Exception as e:
            logger.error(f"Error getting OAuth token: {e}", exc_info=True)
            return OAuthTokenResult(
                connected=False,
                provider_id=provider_id,
                provider_name=provider_id,
                error=str(e),
            )

    async def check_connection(
        self,
        user_id: str,
        provider_id: str,
    ) -> bool:
        """
        Quick check if user is connected to a provider.

        Args:
            user_id: The user's ID
            provider_id: The OAuth provider ID

        Returns:
            True if connected and token is valid
        """
        result = await self.get_token_for_tool(user_id, provider_id)
        return result.connected

    def get_connect_url(self, provider_id: str) -> str:
        """
        Get the URL for the user to connect to a provider.

        Args:
            provider_id: The OAuth provider ID

        Returns:
            URL to the connections page
        """
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:4200")
        return f"{frontend_url}/settings/connections"


# Singleton instance
_oauth_tool_service: Optional[OAuthToolService] = None


def get_oauth_tool_service() -> OAuthToolService:
    """Get the singleton OAuthToolService instance."""
    global _oauth_tool_service
    if _oauth_tool_service is None:
        _oauth_tool_service = OAuthToolService()
    return _oauth_tool_service


async def check_oauth_requirements_for_tools(
    user_id: str,
    enabled_tool_ids: list[str],
) -> dict[str, OAuthTokenResult]:
    """
    Check OAuth connection status for all tools that require OAuth.

    This is useful for determining which tools will work and providing
    guidance to users about missing connections.

    Args:
        user_id: The user's ID
        enabled_tool_ids: List of enabled tool IDs

    Returns:
        Dict mapping provider_id to OAuthTokenResult for tools needing OAuth
    """
    from apis.app_api.tools.repository import get_tool_catalog_repository

    results: dict[str, OAuthTokenResult] = {}
    repository = get_tool_catalog_repository()
    oauth_service = get_oauth_tool_service()

    # Find all unique OAuth providers required by enabled tools
    providers_needed: set[str] = set()

    for tool_id in enabled_tool_ids:
        try:
            tool = await repository.get_tool(tool_id)
            if tool and tool.requires_oauth_provider:
                providers_needed.add(tool.requires_oauth_provider)
        except Exception as e:
            logger.warning(f"Could not check tool {tool_id}: {e}")

    # Check connection status for each provider
    for provider_id in providers_needed:
        result = await oauth_service.get_token_for_tool(user_id, provider_id)
        results[provider_id] = result

    return results


def format_oauth_connection_guidance(
    missing_connections: list[OAuthTokenResult],
) -> str:
    """
    Format a user-friendly message about missing OAuth connections.

    Args:
        missing_connections: List of OAuthTokenResult for unconnected providers

    Returns:
        Markdown formatted message for the user
    """
    if not missing_connections:
        return ""

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:4200")
    connect_url = f"{frontend_url}/settings/connections"

    if len(missing_connections) == 1:
        conn = missing_connections[0]
        if conn.needs_reauth:
            return f"""⚠️ Your connection to **{conn.provider_name}** has expired.

Please [reconnect]({connect_url}) to use tools that require {conn.provider_name} access."""
        else:
            return f"""🔗 To use tools that require **{conn.provider_name}** access, please [connect your account]({connect_url})."""

    # Multiple missing connections
    provider_names = [c.provider_name for c in missing_connections]
    names_str = ", ".join(provider_names[:-1]) + f" and {provider_names[-1]}"

    return f"""🔗 Some tools require account connections.

To use all your enabled tools, please connect: **{names_str}**

👉 [Manage Connections]({connect_url})"""
