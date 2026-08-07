"""Per-request authentication.

A protocol rather than a helper function because the two credential kinds behave
differently at request time. An API key is a constant string. A bearer token
expires, so the provider may have to refresh it *before* the request goes out —
which makes the call async, and makes it something a transport has to await
rather than read from a field.

``BearerAuth`` exists ahead of the app-api bearer branch on purpose: it is the
seam, and having it means the transport can be written once. It refuses to
guess where a token comes from, taking an async supplier instead, so the
refresh policy stays in :mod:`agentcore_tui.auth` where the token store is.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, runtime_checkable

from ..credentials import CredentialSource


@runtime_checkable
class AuthProvider(Protocol):
    """Supplies the auth headers for one request."""

    @property
    def source(self) -> CredentialSource:
        """Which credential kind this provider presents."""
        ...

    async def headers(self) -> Mapping[str, str]:
        """Headers to merge into the request.

        Async so an implementation may renew an expired token first. Callers
        must await this per request rather than caching the result, or a
        long-lived client will keep sending a stale token.
        """
        ...


class NoAuth:
    """For unauthenticated endpoints such as ``/health``."""

    @property
    def source(self) -> CredentialSource:
        return CredentialSource.NONE

    async def headers(self) -> Mapping[str, str]:
        return {}


class ApiKeyAuth:
    """``X-API-Key`` for ``/chat/api-converse``, the one API-key endpoint."""

    __slots__ = ("_api_key",)

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @property
    def source(self) -> CredentialSource:
        return CredentialSource.API_KEY

    async def headers(self) -> Mapping[str, str]:
        return {"X-API-Key": self._api_key}

    def __repr__(self) -> str:
        # Never render the key, not even truncated.
        return f"ApiKeyAuth(len={len(self._api_key)})"


class BearerAuth:
    """``Authorization: Bearer`` from an async token supplier.

    The supplier is expected to return a *currently valid* access token,
    refreshing if needed. Keeping that policy outside this class means the
    transport never has to know about refresh tokens, keyrings or Cognito.
    """

    __slots__ = ("_supply",)

    def __init__(self, supply: Callable[[], Awaitable[str]]) -> None:
        self._supply = supply

    @property
    def source(self) -> CredentialSource:
        return CredentialSource.SSO_SESSION

    async def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {await self._supply()}"}

    def __repr__(self) -> str:
        return "BearerAuth()"
