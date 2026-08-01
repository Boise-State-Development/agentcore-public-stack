"""
OAuth Bearer Token Authentication for External MCP Servers

Provides an httpx Auth class that injects OAuth Bearer tokens into requests.
The token is retrieved dynamically at request time based on user context.
"""

import inspect
import logging
from typing import AsyncGenerator, Generator, Optional, Callable

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
            token_provider: Callback returning the current token. May be a plain
                callable (resolved synchronously at request time, e.g. an
                in-memory cache lookup) or an async callable (awaited in
                `async_auth_flow`, e.g. an RFC 8693 token exchange that may need
                a network round trip).
        """
        self._token = token
        self._token_provider = token_provider
        self._provider_is_async = inspect.iscoroutinefunction(token_provider)

        if not token and not token_provider:
            raise ValueError("Either token or token_provider must be provided")

    def _get_token(self) -> Optional[str]:
        """Get the current token, either static or from provider."""
        if self._token_provider:
            if self._provider_is_async:
                # Refuse rather than return a coroutine, which would be
                # stringified into the Authorization header and produce a
                # baffling 401 at the far end.
                raise RuntimeError(
                    "async token_provider requires the async request path "
                    "(httpx will call async_auth_flow); this is a wiring bug"
                )
            return self._token_provider()
        return self._token

    async def _get_token_async(self) -> Optional[str]:
        """Resolve the token, awaiting the provider when it is async."""
        if self._token_provider:
            if self._provider_is_async:
                return await self._token_provider()
            return self._token_provider()
        return self._token

    def _apply(self, request: httpx.Request, token: Optional[str]) -> None:
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
            logger.debug("Added OAuth Bearer token to request")
        else:
            logger.warning("No OAuth token available for request")

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """
        Add Bearer token to the request Authorization header.

        This method is called by httpx for each request.
        """
        self._apply(request, self._get_token())
        yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """
        Async counterpart of `auth_flow`.

        httpx calls this on async clients, which is what the MCP streamable-HTTP
        transport uses. Overriding it is what makes an async token provider
        possible: the default implementation delegates to the sync `auth_flow`,
        which cannot await. Without this, a network-backed provider would have to
        block the event loop.
        """
        self._apply(request, await self._get_token_async())
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
