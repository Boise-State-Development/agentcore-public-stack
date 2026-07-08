"""Unattended bearer minting for headless runs.

The AgentCore Runtime is provisioned with a **Cognito customJwtAuthorizer**
(`inference-agentcore-construct.ts`: discovery URL = the user pool,
`allowedClients` = [BFF app client]). Spike probes against dev-ai proved
(see docs/specs/harness-entrypoint-spike-findings.md, Unknown 1):

- A platform **workload access token** (`GetWorkloadAccessTokenForUserId`)
  is NOT accepted as the `/invocations` bearer — it is an opaque encrypted
  blob, not a JWT; the gateway rejects it with
  ``403 {"message": "OAuth authorization failed: Failed to parse token"}``.
- **SigV4** (`invoke_agent_runtime`) is also rejected once a JWT authorizer
  is configured: ``AccessDeniedException: Authorization method mismatch``.

So the only front door is a real Cognito access token for the owning user,
minted by the platform. :class:`CognitoRefreshBearerAuth` implements that:
exchange the refresh token pinned in the user's **headless-grant record**
(`apis.shared.harness.grants` — created when the user enables headless
runs, revocable, TTL-bounded) via `REFRESH_TOKEN_AUTH` + SECRET_HASH — the
exact machinery `SessionRefreshMiddleware` already runs for browser
sessions.

The minted token then works three layers deep: gateway JWT authorizer →
container `get_current_user_trusted` (`sub` → user_id, so memory/RBAC/
quota/session all resolve to the right user) → forwarded to forward-auth
MCP servers, which validate `client_id == BFF client`.

The workload identity is still essential — but one layer down: *inside* the
runtime, connector tokens are minted from the vault via
`GetWorkloadAccessTokenForUserId` keyed by the `sub` of the bearer we send
(`apis/shared/oauth/agentcore_identity.py`). The front-door bearer and the
vault leg are two different trust boundaries.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

from apis.shared.harness.grants import HeadlessGrantService
from apis.shared.sessions_bff.refresh import CognitoRefreshClient, CognitoRefreshError

logger = logging.getLogger(__name__)


class HeadlessAuthError(RuntimeError):
    """No bearer could be minted for the requested user.

    Raised when the user has no active headless grant (never enabled,
    revoked, or expired past the login-recency window) or when Cognito
    refuses the refresh exchange. For a scheduled trigger this should pause
    the schedule (analogous to KB-sync's ``paused_reauth``) — the user must
    log in and re-enable before headless runs can act as them again.
    """


class BearerAuthStrategy(Protocol):
    """Seam between the runner and however a bearer is obtained.

    Additional strategies (e.g. an M2M client + trusted `user_id` payload,
    were the runtime's authorizer ever reconfigured) can be added without
    touching the runner.
    """

    async def mint_bearer_for_user(self, user_id: str) -> str: ...


class StaticBearerAuth:
    """Wrap an already-obtained token (tests; callers with a live token)."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def mint_bearer_for_user(self, user_id: str) -> str:
        return self._token


class CognitoRefreshBearerAuth:
    """Mint a per-owner access token from the user's headless grant.

    Resolves the newest active :class:`~apis.shared.harness.grants.HeadlessGrant`
    for ``user_id`` (a GSI query — no table Scan) and runs the standard
    Cognito refresh exchange against its pinned refresh token. Requires the
    caller's IAM principal to read/write the BFF sessions table (where
    grants live) and the BFF app-client secret — app-api's role already
    holds both grants.

    Rotation-aware: if Cognito ever rotates the refresh token during a mint
    (rotation is off on this pool today), the replacement is persisted back
    onto the grant before the access token is returned, so the grant is
    never stranded holding a dead token.
    """

    def __init__(
        self,
        *,
        grants: Optional[HeadlessGrantService] = None,
        refresh_client: Optional[CognitoRefreshClient] = None,
    ) -> None:
        self._grants = grants or HeadlessGrantService()
        self._refresh_client = refresh_client or CognitoRefreshClient()

    async def mint_bearer_for_user(self, user_id: str) -> str:
        """Return a fresh Cognito access token for ``user_id``.

        Raises:
            HeadlessAuthError: no active grant, or Cognito refused the
                refresh exchange (token expired/revoked upstream).
        """
        grant = await self._grants.get_active_grant(user_id)
        if grant is None:
            raise HeadlessAuthError(
                f"No active headless grant for user {user_id}; the user must "
                "log in and enable headless runs before the platform can act "
                "as them."
            )
        try:
            refreshed = await self._refresh_client.refresh(
                username=grant.username,
                refresh_token=grant.cognito_refresh_token,
            )
        except CognitoRefreshError as exc:
            raise HeadlessAuthError(
                f"Cognito refused the refresh exchange for user {user_id} "
                f"(grant {grant.grant_id}): {exc}"
            ) from exc

        if refreshed.refresh_token != grant.cognito_refresh_token:
            logger.info(
                "Cognito rotated the refresh token during a headless mint for "
                "user %s; persisting the replacement onto grant %s",
                user_id,
                grant.grant_id,
            )
            await self._grants.persist_rotated_refresh_token(
                grant.grant_id, refreshed.refresh_token
            )

        await self._grants.record_use(grant.grant_id)
        return refreshed.access_token
