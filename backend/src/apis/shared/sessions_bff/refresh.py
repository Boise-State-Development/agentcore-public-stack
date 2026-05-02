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
        self._client_secret: Optional[str] = None
        self._secret_lock = Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._app_client_id and self._secret_arn)

    def _resolve_client_secret(self) -> str:
        if self._client_secret is not None:
            return self._client_secret
        with self._secret_lock:
            if self._client_secret is not None:
                return self._client_secret
            sm = self._secrets_manager or boto3.client(
                "secretsmanager", region_name=self._region
            )
            response = sm.get_secret_value(SecretId=self._secret_arn)
            value = response.get("SecretString") or ""
            # Phase 1 stores the secret as a plain string; tolerate a JSON
            # `{"clientSecret": "..."}` wrapper too in case CDK changes.
            if value.startswith("{"):
                try:
                    parsed = json.loads(value)
                    value = parsed.get("clientSecret") or parsed.get("client_secret") or value
                except json.JSONDecodeError:
                    pass
            if not value:
                raise CognitoRefreshError("BFF client secret resolved to empty string")
            self._client_secret = value
            return value

    def _secret_hash(self, username: str) -> str:
        secret = self._resolve_client_secret()
        msg = (username + self._app_client_id).encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def _idp(self):
        if self._cognito_idp is not None:
            return self._cognito_idp
        return boto3.client("cognito-idp", region_name=self._region)

    def refresh(self, *, username: str, refresh_token: str) -> RefreshResult:
        """Call Cognito to exchange the refresh token for a fresh access
        token. Raises `CognitoRefreshError` on any failure."""
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
