"""Cognito refresh-token exchange for the BFF Token Handler.

The middleware delegates here when a session record's access token is within
the refresh leeway window. We use `cognito-idp:InitiateAuth` with
`AuthFlow=REFRESH_TOKEN_AUTH`. Since the BFF app client is *confidential*
(provisioned with `generateSecret: true` in Phase 1 CDK), every InitiateAuth
call must include `SECRET_HASH = Base64( HMAC-SHA256( client_secret,
username + client_id ) )`.

The client secret is fetched from Secrets Manager once per process and
cached. If Cognito returns an error, callers should treat the session as
revoked and clear the cookie — the most common cause is rotation killing
the previous refresh token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Optional

import boto3

logger = logging.getLogger(__name__)


# Module-level cache for the BFF client secret. The Phase 3 token-exchange
# route and `CognitoRefreshClient` both read it; sharing one cache means one
# Secrets Manager call per process, regardless of which path warms it first.
_client_secret_cache: Optional[str] = None
_client_secret_lock = Lock()


def resolve_bff_client_secret(
    *,
    secret_arn: Optional[str] = None,
    region: Optional[str] = None,
    secrets_manager_client: Optional[object] = None,
) -> str:
    """Fetch and cache the BFF Cognito app client secret.

    Tolerates both the plain-string format Phase 1 CDK writes today and a
    `{"clientSecret": "..."}` JSON wrapper in case the CDK shape changes.
    """
    global _client_secret_cache
    if _client_secret_cache is not None:
        return _client_secret_cache
    with _client_secret_lock:
        if _client_secret_cache is not None:
            return _client_secret_cache
        arn = secret_arn or os.environ.get("COGNITO_BFF_APP_CLIENT_SECRET_ARN") or ""
        if not arn:
            raise CognitoRefreshError("BFF client secret ARN is not configured")
        sm_region = (
            region
            or os.environ.get("COGNITO_REGION")
            or os.environ.get("AWS_REGION")
            or "us-west-2"
        )
        sm = secrets_manager_client or boto3.client(
            "secretsmanager", region_name=sm_region
        )
        response = sm.get_secret_value(SecretId=arn)
        value = response.get("SecretString") or ""
        if value.startswith("{"):
            try:
                parsed = json.loads(value)
                value = parsed.get("clientSecret") or parsed.get("client_secret") or value
            except json.JSONDecodeError:
                logger.debug(
                    "BFF client secret looked like JSON but failed to decode; using raw SecretString value"
                )
        if not value:
            raise CognitoRefreshError("BFF client secret resolved to empty string")
        _client_secret_cache = value
        return value


def _reset_secret_cache_for_tests() -> None:
    global _client_secret_cache
    with _client_secret_lock:
        _client_secret_cache = None


@dataclass
class RefreshResult:
    access_token: str
    # Cognito does not always rotate refresh tokens — fall back to the prior
    # one when the response omits it.
    refresh_token: str
    id_token: Optional[str]
    access_token_exp: int  # epoch seconds


class CognitoRefreshError(Exception):
    """Raised when Cognito refuses to refresh the session.

    Always treat as terminal: the cookie should be cleared and the user
    re-authenticated.
    """


class CognitoRefreshClient:
    """Holds cached SDK + secret references for the refresh path."""

    def __init__(
        self,
        *,
        app_client_id: Optional[str] = None,
        app_client_secret_arn: Optional[str] = None,
        region: Optional[str] = None,
        cognito_idp_client: Optional[object] = None,
        secrets_manager_client: Optional[object] = None,
    ) -> None:
        self._app_client_id = app_client_id or os.environ.get("COGNITO_BFF_APP_CLIENT_ID") or ""
        self._secret_arn = (
            app_client_secret_arn
            or os.environ.get("COGNITO_BFF_APP_CLIENT_SECRET_ARN")
            or ""
        )
        self._region = (
            region
            or os.environ.get("COGNITO_REGION")
            or os.environ.get("AWS_REGION")
            or "us-west-2"
        )
        self._cognito_idp = cognito_idp_client
        self._secrets_manager = secrets_manager_client

    @property
    def enabled(self) -> bool:
        return bool(self._app_client_id and self._secret_arn)

    def _resolve_client_secret(self) -> str:
        return resolve_bff_client_secret(
            secret_arn=self._secret_arn,
            region=self._region,
            secrets_manager_client=self._secrets_manager,
        )

    def _secret_hash(self, username: str) -> str:
        secret = self._resolve_client_secret()
        msg = (username + self._app_client_id).encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def _idp(self):
        if self._cognito_idp is not None:
            return self._cognito_idp
        return boto3.client("cognito-idp", region_name=self._region)

    def _refresh_sync(self, *, username: str, refresh_token: str) -> RefreshResult:
        """Synchronous Cognito refresh exchange.

        This is the raw boto3 path — kept private so callers can't invoke it
        directly from the event loop. Use :meth:`refresh` instead, which
        offloads this call via ``asyncio.to_thread`` so the uvicorn event
        loop stays responsive (and so other sessions' ``get_session_lock``
        acquisitions can still progress while ours is held).
        """
        if not self.enabled:
            raise CognitoRefreshError("BFF refresh client is not configured")

        try:
            response = self._idp().initiate_auth(
                AuthFlow="REFRESH_TOKEN_AUTH",
                ClientId=self._app_client_id,
                AuthParameters={
                    "REFRESH_TOKEN": refresh_token,
                    "SECRET_HASH": self._secret_hash(username),
                },
            )
        except Exception as exc:
            logger.warning("BFF Cognito refresh failed for %s: %s", username, exc)
            raise CognitoRefreshError(str(exc)) from exc

        result = response.get("AuthenticationResult") or {}
        access_token = result.get("AccessToken")
        if not access_token:
            raise CognitoRefreshError("Cognito refresh returned no access token")
        expires_in = int(result.get("ExpiresIn", 3600))
        return RefreshResult(
            access_token=access_token,
            # Cognito only returns RefreshToken when rotation kicks in.
            refresh_token=result.get("RefreshToken") or refresh_token,
            id_token=result.get("IdToken"),
            access_token_exp=int(time.time()) + expires_in,
        )

    async def refresh(self, *, username: str, refresh_token: str) -> RefreshResult:
        """Exchange the refresh token for a fresh access token, off the loop.

        Offloads the synchronous boto3 ``initiate_auth`` call via
        ``asyncio.to_thread`` so the event loop keeps scheduling other
        coroutines while Cognito is in flight. Critically, this matters
        while the per-session ``get_session_lock(session_id)`` is held —
        unrelated sessions' locks must remain acquirable on the loop.

        The exception contract and :class:`RefreshResult` return shape are
        identical to :meth:`_refresh_sync`: ``CognitoRefreshError`` is
        raised on any Cognito failure and should be treated as terminal.
        """
        return await asyncio.to_thread(
            self._refresh_sync,
            username=username,
            refresh_token=refresh_token,
        )
