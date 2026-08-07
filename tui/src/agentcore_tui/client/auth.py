"""Per-request authentication.

A protocol rather than a helper function because the credential kinds behave
differently at request time, and a transport should not have to know which it
holds. Both current kinds happen to be constant strings, but the protocol keeps
:meth:`AuthProvider.headers` async so a future provider that has to renew
something before the request goes out can do so without changing every caller.

Two providers carry real credentials:

======================= ================================ ========================
``ApiKeyAuth``          ``X-API-Key``                    ``/chat/api-converse``
``SessionAuth``         ``Authorization: BFF <sealed>``   everything else
======================= ================================ ========================

A previous ``BearerAuth`` was removed with this module's Phase 2 rewiring. It
existed for a CLI that minted its own Cognito tokens, and that design was
reverted in #850: a CLI-minted token reaches external MCP servers which may pin
`client_id` to the BFF app client. The replacement never mints a token at all,
so there is no bearer path left to keep a seam open for. ``AuthProvider`` is the
seam; a provider is not.
"""

from __future__ import annotations

from collections.abc import Mapping
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


class SessionAuth:
    """``Authorization: BFF <sealed>`` — a real BFF session, obtained by the
    device-authorization flow in :mod:`agentcore_tui.client.device_auth`.

    This is the credential that reaches the session-authenticated surface of
    app-api: ``/sessions``, ``/models``, ``/tools``, and the tool-using agent
    behind ``POST /chat/stream``.

    Deliberately *not* a :class:`BearerAuth` with a different scheme, for a
    reason that is the whole point of the design: the value is a sealed session
    envelope minted by app-api, not a token minted by Cognito. The client cannot
    read it, cannot refresh it, and never talks to an identity provider. So
    there is no async work to do here — unlike ``BearerAuth`` this needs no
    supplier, and the value is a constant for its lifetime.

    Renewal is the server's job and happens as a side effect of use:
    ``SessionRefreshMiddleware`` slides the DynamoDB TTL on the header path just
    as it does for a cookie, so an active CLI keeps its session alive. When the
    session does lapse the server 401s and the user signs in again; there is no
    refresh token to rotate.

    The sealed value is a credential equivalent to being signed in, so it is
    never rendered — see :meth:`__repr__`, and note that the transport layer's
    logging is redacted for the same reason.
    """

    __slots__ = ("_sealed",)

    #: The scheme app-api's middleware looks for. Must match
    #: `sealed_session_from_header` in `apis/shared/middleware/session_refresh.py`.
    SCHEME = "BFF"

    def __init__(self, sealed_session: str) -> None:
        self._sealed = sealed_session

    @property
    def source(self) -> CredentialSource:
        return CredentialSource.BFF_SESSION

    async def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"{self.SCHEME} {self._sealed}"}

    def __repr__(self) -> str:
        # Never render the session, not even truncated: it is a bearer
        # credential for the user's whole account.
        return f"SessionAuth(len={len(self._sealed)})"
