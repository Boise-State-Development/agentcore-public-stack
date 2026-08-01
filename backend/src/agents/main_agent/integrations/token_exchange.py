"""
RFC 8693 token exchange for downstream APIs.

Trades the signed-in user's Cognito access token for a token-service JWT scoped
to one registered downstream application, so an MCP server can call an API that
already trusts token-service JWTs as the user — with no change on the target
API's side.

Why this lives in the agent runtime rather than the Gateway
----------------------------------------------------------
AgentCore Gateway cannot do this. Its outbound OAuth credential provider accepts
only CLIENT_CREDENTIALS and AUTHORIZATION_CODE (verified against the
`bedrock-agentcore-control` API model: `OAuthGrantType` has exactly those two
values, and the string "TOKEN_EXCHANGE" appears nowhere in the model). A
token-exchange grant is not expressible there.

Separately, a Gateway with `AWS_IAM` inbound auth never receives the user's
token, so it would have nothing to exchange even if the grant existed. The
exchange therefore happens here, where the user's token is already in hand, and
the result is forwarded to the MCP server exactly as `forward_auth_token` does
with the raw token.

Caching
-------
Exchanged tokens are short-lived by design (the token service caps them, 600s in
dev) and every downstream API call needs one, so they are cached per
(user, audience) and reused until shortly before expiry. The cache holds
credentials, so entries are keyed by user and never shared across users.

What this does NOT provide
--------------------------
Revocation does not propagate. The token service validates the subject token
offline (signature, issuer, expiry, token_use, client_id) and never asks Cognito
whether it is still live, so a Cognito token revoked before its expiry is still
exchangeable. Exposure is bounded by the token service's own lifetime cap.
Measured and documented on the token-service side; noted here because this is
where the tokens are minted from.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import boto3
import httpx

logger = logging.getLogger(__name__)

# Grant and token-type identifiers (RFC 8693 §2.1).
_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

# Refresh this far before expiry so a token is not spent on the wire as it dies.
_EXPIRY_SKEW_SECONDS = 30

# Floor on cache lifetime, in case the server returns a tiny expires_in.
_MIN_CACHE_SECONDS = 5

_HTTP_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class _CacheEntry:
    token: str
    expires_at: float  # monotonic seconds


class TokenExchangeError(Exception):
    """The exchange could not be completed."""


class TokenExchangeClient:
    """
    Exchanges Cognito access tokens for token-service JWTs.

    One instance per process. Thread-safe: the agent runtime serves concurrent
    turns, and without a lock two turns for the same user would each perform a
    network exchange and race to populate the cache.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        client_id: Optional[str] = None,
        secret_id: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        self._endpoint = endpoint or os.getenv("TOKEN_EXCHANGE_URL", "")
        self._client_id = client_id or os.getenv("TOKEN_EXCHANGE_CLIENT_ID", "")
        self._secret_id = secret_id or os.getenv("TOKEN_EXCHANGE_SECRET_ID", "")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")

        self._cache: Dict[Tuple[str, str], _CacheEntry] = {}
        self._lock = threading.Lock()
        self._client_secret: Optional[str] = None

    @property
    def configured(self) -> bool:
        """True when every value needed to perform an exchange is present."""
        return bool(self._endpoint and self._client_id and self._secret_id)

    def _load_client_secret(self) -> str:
        """
        Read the confidential-client secret from Secrets Manager.

        Cached for the life of the process: it is needed on every exchange and
        Secrets Manager is both rate-limited and billed per call. A rotated
        secret is therefore picked up on the next cold start.
        """
        if self._client_secret is not None:
            return self._client_secret

        try:
            client = boto3.client("secretsmanager", region_name=self._region)
            response = client.get_secret_value(SecretId=self._secret_id)
            payload = json.loads(response["SecretString"])
        except Exception as exc:
            raise TokenExchangeError(
                f"could not read exchange client secret '{self._secret_id}'"
            ) from exc

        secret = payload.get(self._client_id)
        if not secret:
            raise TokenExchangeError(
                f"no secret provisioned for exchange client '{self._client_id}'"
            )

        self._client_secret = secret
        return secret

    def _cache_key(self, user_id: str, audience: str) -> Tuple[str, str]:
        return (user_id, audience)

    def _cached(self, key: Tuple[str, str]) -> Optional[str]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            # Drop rather than serve a token that is about to be refused.
            self._cache.pop(key, None)
            return None
        return entry.token

    async def exchange(
        self,
        subject_token: str,
        audience: str,
        user_id: str,
    ) -> str:
        """
        Return a token-service JWT for `audience`, acting for the user who owns
        `subject_token`.

        Args:
            subject_token: the user's Cognito access token.
            audience: token-service applicationId of the target application.
            user_id: cache partition. Must identify the user, never be shared.

        Raises:
            TokenExchangeError: not configured, refused, or unreachable.
        """
        if not self.configured:
            raise TokenExchangeError(
                "token exchange is not configured (need TOKEN_EXCHANGE_URL, "
                "TOKEN_EXCHANGE_CLIENT_ID, TOKEN_EXCHANGE_SECRET_ID)"
            )
        if not subject_token:
            raise TokenExchangeError("no user token available to exchange")
        if not audience:
            raise TokenExchangeError("no audience configured for this tool")
        if not user_id:
            # Without a user id the cache cannot be partitioned, and a shared
            # entry would hand one user's token to another.
            raise TokenExchangeError("user_id is required to exchange a token")

        key = self._cache_key(user_id, audience)

        with self._lock:
            cached = self._cached(key)
        if cached:
            return cached

        client_secret = self._load_client_secret()

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as http:
                response = await http.post(
                    self._endpoint,
                    auth=(self._client_id, client_secret),
                    data={
                        "grant_type": _GRANT_TYPE,
                        "subject_token": subject_token,
                        "subject_token_type": _SUBJECT_TOKEN_TYPE,
                        "audience": audience,
                    },
                )
        except httpx.HTTPError as exc:
            raise TokenExchangeError(f"token exchange request failed: {exc}") from exc

        if response.status_code != 200:
            # The endpoint returns deliberately coarse errors; log what it gave
            # us but never the tokens involved.
            detail = ""
            try:
                body = response.json()
                detail = f"{body.get('error')}: {body.get('error_description')}"
            except Exception:
                detail = response.text[:200]
            raise TokenExchangeError(
                f"token exchange refused (HTTP {response.status_code}) {detail}"
            )

        body = response.json()
        token = body.get("access_token")
        if not token:
            raise TokenExchangeError("token exchange returned no access_token")

        expires_in = body.get("expires_in")
        try:
            lifetime = int(expires_in)
        except (TypeError, ValueError):
            # No usable expiry: cache for the minimum rather than indefinitely.
            lifetime = _MIN_CACHE_SECONDS + _EXPIRY_SKEW_SECONDS

        ttl = max(_MIN_CACHE_SECONDS, lifetime - _EXPIRY_SKEW_SECONDS)

        with self._lock:
            self._cache[key] = _CacheEntry(
                token=token, expires_at=time.monotonic() + ttl
            )

        logger.info(
            "Token exchange succeeded for audience=%s (cached %ss)", audience, ttl
        )
        return token

    def invalidate(self, user_id: str, audience: str) -> None:
        """Drop a cached token, e.g. after the target API rejects it."""
        with self._lock:
            self._cache.pop(self._cache_key(user_id, audience), None)


_client: Optional[TokenExchangeClient] = None
_client_lock = threading.Lock()


def get_token_exchange_client() -> TokenExchangeClient:
    """The process-wide exchange client."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = TokenExchangeClient()
    return _client
