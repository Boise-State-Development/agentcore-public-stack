"""
OAuth Bearer Token Authentication for External MCP Servers

Provides an httpx Auth class that injects OAuth Bearer tokens into requests.
The token is retrieved dynamically at request time based on user context.
"""

import logging
from typing import Generator, Optional, Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)


class OAuthBearerAuth(httpx.Auth):
    """
    HTTPX Auth class that adds OAuth Bearer tokens to requests.

    The token is retrieved dynamically via a callback function,
    allowing user-specific tokens to be injected at request time.

    Usage:
        async def get_token() -> Optional[str]:
            return await oauth_service.get_decrypted_token(user_id, provider_id)

        auth = OAuthBearerAuth(token_provider=get_token)
        client = httpx.AsyncClient(auth=auth)
    """

    def __init__(
        self,
        token: Optional[str] = None,
        token_provider: Optional[Callable[[], str | None]] = None,
    ):
        """
        Initialize OAuth Bearer authentication.

        Args:
            token: Static token to use (for simple cases)
            token_provider: Callback function that returns the current token.
                           Called synchronously at request time.
        """
        self._token = token
        self._token_provider = token_provider

        if not token and not token_provider:
            raise ValueError("Either token or token_provider must be provided")

    def _get_token(self) -> Optional[str]:
        """Get the current token, either static or from provider."""
        if self._token_provider:
            return self._token_provider()
        return self._token

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """
        Add Bearer token to the request Authorization header.

        This method is called by httpx for each request.
        """
        token = self._get_token()

        if token:
            request.headers["Authorization"] = f"Bearer {token}"
            logger.debug("Added OAuth Bearer token to request")
        else:
            logger.warning("No OAuth token available for request")

        yield request


class CompositeAuth(httpx.Auth):
    """
    Combines multiple auth methods (e.g., SigV4 + OAuth Bearer).

    Useful when an MCP server requires both AWS IAM auth and user OAuth tokens.
    """

    def __init__(self, *auth_handlers: httpx.Auth):
        """
        Initialize with multiple auth handlers.

        Args:
            *auth_handlers: Auth handlers to apply in order
        """
        self._handlers = auth_handlers

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """
        Apply all auth handlers to the request.
        """
        for handler in self._handlers:
            # Each handler's auth_flow is a generator
            flow = handler.auth_flow(request)
            try:
                request = next(flow)
            except StopIteration:
                pass

        yield request


def create_oauth_bearer_auth(
    token: Optional[str] = None,
    token_provider: Optional[Callable[[], str | None]] = None,
) -> OAuthBearerAuth:
    """
    Create an OAuth Bearer auth handler.

    Args:
        token: Static token to use
        token_provider: Function that returns current token

    Returns:
        OAuthBearerAuth instance for use with httpx clients
    """
    return OAuthBearerAuth(token=token, token_provider=token_provider)
