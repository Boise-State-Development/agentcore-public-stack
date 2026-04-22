"""AgentCore Identity integration for external MCP tool authorization.

Wraps `bedrock_agentcore.services.identity.IdentityClient` with a narrower,
platform-friendly surface for retrieving OAuth2 access tokens on behalf of a
user via the USER_FEDERATION (3LO) flow.

The client pulls the per-invocation workload identity token from
`BedrockAgentCoreContext`, which is populated by `AgentCoreContextMiddleware`
on the Inference API request path. No workload token has to be threaded
through function arguments.

Two results are possible when fetching a token:

1. A valid token exists in the AgentCore Token Vault for this user+provider
   → returned synchronously as `TokenResult(access_token=...)`.
2. The user has never consented (or consent has been revoked, or scopes have
   changed) → the caller receives `TokenResult(authorization_url=...)`. The
   URL must be surfaced to the user; after they complete the consent flow the
   frontend calls `CompleteResourceTokenAuthCommand` and the next tool call
   will hit case 1.

This module intentionally does not raise on "consent required" — it returns
a structured result because surfacing an auth URL is a normal, expected
outcome that flows through our SSE stream, not an error.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

from bedrock_agentcore.runtime import BedrockAgentCoreContext
from bedrock_agentcore.services.identity import IdentityClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenResult:
    """Result of a token fetch attempt.

    Exactly one of `access_token` or `authorization_url` will be populated.
    """

    access_token: Optional[str] = None
    authorization_url: Optional[str] = None

    @property
    def requires_consent(self) -> bool:
        return self.access_token is None and self.authorization_url is not None

    def __post_init__(self) -> None:
        if bool(self.access_token) == bool(self.authorization_url):
            raise ValueError(
                "TokenResult must have exactly one of access_token or authorization_url"
            )


class WorkloadTokenUnavailableError(RuntimeError):
    """Raised when no workload access token is present on the current context.

    This indicates the caller is running outside an AgentCore Runtime
    invocation, or the `AgentCoreContextMiddleware` was not applied.
    """


class AgentCoreIdentityClient:
    """Thin async-friendly wrapper around `IdentityClient` for 3LO tokens.

    The underlying `IdentityClient` is synchronous and uses boto3; callers
    should treat `get_token_for_user` as potentially blocking and run it via
    `asyncio.to_thread` when invoked from async code.
    """

    def __init__(self, region: Optional[str] = None):
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._client = IdentityClient(region=self._region)

    def get_token_for_user(
        self,
        *,
        provider_name: str,
        scopes: List[str],
        callback_url: Optional[str] = None,
        force_authentication: bool = False,
    ) -> TokenResult:
        """Fetch a user-federated OAuth2 access token for `provider_name`.

        Pulls the workload identity token from `BedrockAgentCoreContext`, so
        this must be called from inside an AgentCore Runtime invocation that
        has been processed by `AgentCoreContextMiddleware`.

        If the user has not consented (or re-consent is required), returns a
        `TokenResult` with `authorization_url` populated instead of raising.

        Args:
            provider_name: Credential provider name registered with AgentCore
                Identity (e.g. "google-workspace").
            scopes: OAuth2 scopes to request for this token.
            callback_url: OAuth2 return URL. Defaults to the callback URL on
                the current context (injected by Runtime via the
                `OAuth2CallbackUrl` header).
            force_authentication: If True, bypasses the token vault cache and
                forces the user through the consent flow again. Used for
                scope upgrades.

        Returns:
            `TokenResult` with either `access_token` or `authorization_url`.

        Raises:
            WorkloadTokenUnavailableError: No workload token on context.
        """
        workload_token = BedrockAgentCoreContext.get_workload_access_token()
        if not workload_token:
            raise WorkloadTokenUnavailableError(
                "No WorkloadAccessToken on context — ensure "
                "AgentCoreContextMiddleware is installed and this call "
                "runs inside an AgentCore Runtime invocation."
            )

        resolved_callback_url = (
            callback_url or BedrockAgentCoreContext.get_oauth2_callback_url()
        )

        captured_url: dict[str, Optional[str]] = {"url": None}

        def _capture_auth_url(url: str) -> None:
            captured_url["url"] = url

        token = self._client.get_token(
            provider_name=provider_name,
            scopes=scopes,
            agent_identity_token=workload_token,
            auth_flow="USER_FEDERATION",
            callback_url=resolved_callback_url,
            force_authentication=force_authentication,
            on_auth_url=_capture_auth_url,
        )

        # `get_token` returns either the token string or triggers on_auth_url
        # when consent is required. Guard both: if we captured a URL, surface
        # it as a TokenResult even if the SDK also returned a (stale) token.
        if captured_url["url"]:
            logger.info(
                "AgentCore Identity requires user consent for provider=%s",
                provider_name,
            )
            return TokenResult(authorization_url=captured_url["url"])

        if not token:
            raise RuntimeError(
                f"AgentCore Identity returned neither a token nor an "
                f"authorization URL for provider={provider_name}"
            )

        return TokenResult(access_token=token)


_default_client: Optional[AgentCoreIdentityClient] = None


def get_agentcore_identity_client() -> AgentCoreIdentityClient:
    """Return the process-wide `AgentCoreIdentityClient` singleton."""
    global _default_client
    if _default_client is None:
        _default_client = AgentCoreIdentityClient()
    return _default_client
