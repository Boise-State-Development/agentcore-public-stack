"""Unattended bearer minting for headless runs (Unknown 1).

The AgentCore Runtime is provisioned with a **Cognito customJwtAuthorizer**
(`inference-agentcore-construct.ts`: discovery URL = the user pool,
`allowedClients` = [BFF app client]). Spike probes against dev-ai proved:

- A platform **workload access token** (`GetWorkloadAccessTokenForUserId`)
  is NOT accepted as the `/invocations` bearer — it is an opaque encrypted
  blob, not a JWT; the gateway rejects it with
  ``403 {"message": "OAuth authorization failed: Failed to parse token"}``.
- **SigV4** (`invoke_agent_runtime`) is also rejected once a JWT authorizer
  is configured: ``AccessDeniedException: Authorization method mismatch``.

So the only front door is a real Cognito access token for the owning user,
minted by the platform. :class:`CognitoRefreshBearerAuth` implements the
zero-infra-change path: exchange the user's stored BFF refresh token
(`REFRESH_TOKEN_AUTH` + SECRET_HASH — the exact machinery
`SessionRefreshMiddleware` already uses) for a fresh 1-hour access token.
The pool does not rotate refresh tokens (CDK default), so the mint does not
disturb the user's live browser sessions.

The workload identity is still essential — but one layer down: *inside* the
runtime, connector tokens are minted from the vault via
`GetWorkloadAccessTokenForUserId` keyed by the `sub` of the bearer we send
(`apis/shared/oauth/agentcore_identity.py`). The front-door bearer and the
vault leg are two different trust boundaries; the spike brief's "try first"
path conflated them.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Protocol

import boto3

from apis.shared.sessions_bff.refresh import CognitoRefreshClient, CognitoRefreshError

logger = logging.getLogger(__name__)


class HeadlessAuthError(RuntimeError):
    """No bearer could be minted for the requested user.

    For a scheduled trigger this should pause the schedule (analogous to
    KB-sync's ``paused_reauth``) — the user must log in again to renew the
    grant.
    """


class BearerAuthStrategy(Protocol):
    """Seam between the runner and however a bearer is obtained.

    Phase A can add strategies (e.g. a dedicated headless-grant record, or
    an M2M client + trusted `user_id` payload) without touching the runner.
    """

    async def mint_bearer_for_user(self, user_id: str) -> str: ...


class StaticBearerAuth:
    """Wrap an already-obtained token (tests; callers with a live token)."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def mint_bearer_for_user(self, user_id: str) -> str:
        return self._token


class CognitoRefreshBearerAuth:
    """Mint a per-owner access token from the user's stored refresh token.

    Reads the newest BFF session row for ``user_id`` and runs the standard
    Cognito refresh exchange. Requires the caller's IAM principal to read
    the BFF sessions table and the app-client secret — app-api's role
    already holds both grants.

    Spike-scoped caveats (Phase A must address):
    - The BFF sessions table is keyed by ``session_id`` only; finding a
      user's row is a filtered ``Scan``. Fine at spike scale; Phase A needs
      a ``user_id`` GSI or (better) a dedicated headless-grant record that
      stores a refresh token minted at opt-in time with its own lifecycle.
    - The grant inherits BFF session lifetime (~30-day absolute cap +
      row TTL): a user who hasn't logged in recently cannot be run
      headlessly. That is arguably the *right* governance default, but it
      must be an explicit product decision, not an accident.
    """

    def __init__(
        self,
        *,
        sessions_table_name: Optional[str] = None,
        refresh_client: Optional[CognitoRefreshClient] = None,
        region: Optional[str] = None,
    ) -> None:
        self._table_name = sessions_table_name or os.environ.get(
            "BFF_SESSIONS_TABLE_NAME", ""
        )
        self._refresh_client = refresh_client or CognitoRefreshClient()
        self._region = region or os.environ.get("AWS_REGION", "us-west-2")

    def _newest_session_row(self, user_id: str) -> Optional[dict]:
        if not self._table_name:
            raise HeadlessAuthError("BFF_SESSIONS_TABLE_NAME is not configured")
        table = boto3.resource("dynamodb", region_name=self._region).Table(
            self._table_name
        )
        from boto3.dynamodb.conditions import Attr

        rows: list[dict] = []
        kwargs = {"FilterExpression": Attr("user_id").eq(user_id)}
        while True:
            page = table.scan(**kwargs)
            rows.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        if not rows:
            return None
        return max(rows, key=lambda r: int(r.get("last_seen_at") or 0))

    async def mint_bearer_for_user(self, user_id: str) -> str:
        row = await asyncio.to_thread(self._newest_session_row, user_id)
        if row is None:
            raise HeadlessAuthError(
                f"No stored BFF session for user {user_id}; the user must "
                "log in before headless runs can act as them."
            )
        try:
            refreshed = await self._refresh_client.refresh(
                username=str(row["username"]),
                refresh_token=str(row["cognito_refresh_token"]),
            )
        except CognitoRefreshError as exc:
            raise HeadlessAuthError(
                f"Cognito refused the refresh exchange for user {user_id}: {exc}"
            ) from exc
        if refreshed.refresh_token != str(row["cognito_refresh_token"]):
            # Rotation is off on this pool; if it is ever enabled this mint
            # would invalidate the user's browser session unless persisted.
            logger.warning(
                "Cognito rotated the refresh token during a headless mint for "
                "user %s — the stored BFF session row is now stale. Enable "
                "rotation-aware persistence before using this strategy on a "
                "rotating pool.",
                user_id,
            )
        return refreshed.access_token
