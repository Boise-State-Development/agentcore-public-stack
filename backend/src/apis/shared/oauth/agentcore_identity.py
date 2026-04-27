"""AgentCore Identity integration for OAuth2 user-federated tokens.

Wraps `bedrock_agentcore.services.identity.IdentityClient` with a narrower,
platform-friendly surface for retrieving OAuth2 access tokens on behalf of a
user via the USER_FEDERATION (3LO) flow.

Used by both the inference API (agent-loop tool gating, external MCP tool
calls) and the app API (settings-page connector status / consent flows).
Lives in `apis/shared/oauth/` so neither API has to reach into the other's
package.

Both APIs mint user-scoped workload access tokens against a shared platform
workload identity (defined in InfrastructureStack, exported as
`/<projectPrefix>/oauth/platform-workload-identity-name`). They cannot
share the runtime's auto-created identity — that one is service-linked and
only mintable from inside the runtime container — so we own a separate
identity that both APIs act as via `GetWorkloadAccessTokenForUserId`.

`AGENTCORE_RUNTIME_WORKLOAD_NAME` (env var, name kept for diff hygiene)
points at this shared identity. When unset, `_resolve_workload_token`
falls back to the runtime-injected `BedrockAgentCoreContext` token — used
by tests and legacy code paths.

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
from typing import Dict, List, Optional

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreContext
from bedrock_agentcore.services.identity import IdentityClient, TokenPoller

logger = logging.getLogger(__name__)


class _ConsentRequired(Exception):
    """Internal marker — raised by `_ShortCircuitPoller` once AgentCore hands
    us an auth URL, so we can return it to the caller without waiting for
    the user to actually complete consent."""


class _ShortCircuitPoller(TokenPoller):
    """Skip the SDK's default poll loop.

    The default poller hits `GetResourceOauth2Token` on a timer until the
    user finishes consent (up to several minutes). We only care about the
    URL — our caller returns it to the frontend, which drives the popup
    flow on its own. Raising immediately short-circuits the wait.
    """

    async def poll_for_token(self) -> str:
        raise _ConsentRequired()

# Name of the shared platform workload identity. Both inference-api and
# app-api mint user-scoped workload tokens against this identity via
# `bedrock-agentcore:GetWorkloadAccessTokenForUserId`, so they share a
# single OAuth token vault. The env var name is historical (it pre-dates
# the shared-identity design when it pointed at the runtime's own
# workload) — kept to avoid churning every deployment workflow at once.
_RUNTIME_WORKLOAD_ENV = "AGENTCORE_RUNTIME_WORKLOAD_NAME"

# Same shape as above for the OAuth2 callback URL. App-api receives an
# `OAuth2CallbackUrl` header from the frontend on settings-page connector
# calls. Inference-api does not — the AgentCore Runtime gateway strips
# custom request headers before they reach the container, so the frontend's
# header on /invocations never arrives and `get_oauth2_callback_url()`
# returns None. Both APIs therefore set this env var via CDK
# (`<frontend-url>/oauth-complete`) as the runtime-stripped-header fallback
# for the agent loop's consent flow. Local dev sets the same var to
# `http://localhost:4200/oauth-complete`.
_CALLBACK_URL_ENV = "AGENTCORE_LOCAL_OAUTH_CALLBACK_URL"


def _vendor_baseline_params(provider_type: Optional[str]) -> Dict[str, str]:
    """Hardcoded params AgentCore Identity *requires* for a given vendor.

    Per the AgentCore Identity authentication docs
    (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-authentication.html),
    Google must receive `access_type=offline` to issue a refresh token —
    without it the vault entry expires after ~1 hour with no refresh
    path. This is non-negotiable: it always wins over admin-supplied
    extras to prevent an admin from accidentally turning it off.
    """
    if not provider_type:
        return {}
    if provider_type.lower() == "google":
        return {"access_type": "offline"}
    return {}


def custom_parameters_for(
    provider_type: Optional[str],
    admin_extras: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, str]]:
    """Build the `customParameters` payload AgentCore Identity wants forwarded.

    Merges admin-supplied extras (e.g. Google `hd=mycorp.com` for domain
    restriction, `prompt=consent` for stricter UX) with the hardcoded
    vendor baseline. Baseline keys win on conflict — admins cannot turn
    off a documented requirement.

    Returns None when the merged result would be empty, so callers can
    pass the value through to the SDK unconditionally without sending
    an empty `customParameters` map.
    """
    baseline = _vendor_baseline_params(provider_type)
    merged = {**(admin_extras or {}), **baseline}
    return merged or None


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


class CallbackUrlUnavailableError(RuntimeError):
    """Raised when no OAuth2 callback URL can be resolved for an authorize call.

    Surfaced instead of silently passing `None` to the SDK, which builds an
    authorize URL whose redirect never reaches `/oauth-complete` — the user
    finishes consent at the provider but the token is never persisted to
    AgentCore's vault, so the next request prompts them to consent again.
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
        self._control_client = boto3.client("bedrock-agentcore", region_name=self._region)

    async def get_token_for_user(
        self,
        *,
        provider_name: str,
        scopes: List[str],
        callback_url: Optional[str] = None,
        force_authentication: bool = False,
        user_id: Optional[str] = None,
        custom_state: Optional[str] = None,
        custom_parameters: Optional[Dict[str, str]] = None,
    ) -> TokenResult:
        """Fetch a user-federated OAuth2 access token for `provider_name`.

        In production both APIs mint a fresh workload token against the
        shared platform workload identity (configured via
        `AGENTCORE_RUNTIME_WORKLOAD_NAME`), bound to `user_id`. When the
        env var is unset, falls back to the runtime-injected
        `BedrockAgentCoreContext` token — used by tests.

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
            user_id: User identifier for workload-token minting. Required
                when `AGENTCORE_RUNTIME_WORKLOAD_NAME` is set (the
                production path).

        Returns:
            `TokenResult` with either `access_token` or `authorization_url`.

        Raises:
            WorkloadTokenUnavailableError: No token on context and the
                local-dev fallback is unavailable (env var unset, user_id
                missing, or IAM denies the mint call).
            CallbackUrlUnavailableError: No callback URL on context and the
                local-dev fallback env var is unset.
        """
        workload_token = self._resolve_workload_token(user_id)
        resolved_callback_url = self._resolve_callback_url(callback_url, provider_name)

        captured_url: dict[str, Optional[str]] = {"url": None}

        def _capture_auth_url(url: str) -> None:
            captured_url["url"] = url

        try:
            sdk_kwargs = dict(
                provider_name=provider_name,
                scopes=scopes,
                agent_identity_token=workload_token,
                auth_flow="USER_FEDERATION",
                callback_url=resolved_callback_url,
                force_authentication=force_authentication,
                on_auth_url=_capture_auth_url,
                token_poller=_ShortCircuitPoller(),
            )
            if custom_state is not None:
                sdk_kwargs["custom_state"] = custom_state
            if custom_parameters:
                sdk_kwargs["custom_parameters"] = custom_parameters
            token = await self._client.get_token(**sdk_kwargs)
        except _ConsentRequired:
            # Expected path when consent is required: the SDK invoked
            # on_auth_url and then handed off to our poller, which raises.
            token = None

        # If we captured a URL, return it — even if the SDK also returned
        # a (stale) token, consent-required is the authoritative signal.
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

    def _resolve_callback_url(
        self, explicit: Optional[str], provider_name: str
    ) -> str:
        """Pick the OAuth2 callback URL and tag it with `provider_id`.

        Resolution order: explicit arg → request-scoped context (frontend
        header on app-api; absent on inference-api because the runtime
        strips custom headers) → `AGENTCORE_LOCAL_OAUTH_CALLBACK_URL` env
        var (set by CDK on both API tasks; also used in local dev). Raises
        `CallbackUrlUnavailableError` when none is available — passing None
        to the SDK silently breaks consent.

        AgentCore's redirect doesn't echo any provider hint, so we append
        `provider_id` as a query param so `/oauth-complete` can dismiss the
        right pending consent entry.
        """
        from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

        base = (
            explicit
            or BedrockAgentCoreContext.get_oauth2_callback_url()
            or os.environ.get(_CALLBACK_URL_ENV)
        )
        if not base:
            raise CallbackUrlUnavailableError(
                "No OAuth2 callback URL available. App-api expects the "
                "`OAuth2CallbackUrl` header from the frontend; inference-api "
                f"reads {_CALLBACK_URL_ENV} (set by CDK) because the runtime "
                "gateway strips custom headers. For local dev export "
                f"{_CALLBACK_URL_ENV}=http://localhost:4200/oauth-complete."
            )

        parsed = urlparse(base)
        existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
        existing.setdefault("provider_id", provider_name)
        return urlunparse(parsed._replace(query=urlencode(existing)))

    def _resolve_workload_token(self, user_id: Optional[str]) -> str:
        """Return a workload access token bound to the configured workload.

        When `AGENTCORE_RUNTIME_WORKLOAD_NAME` is set we always mint a fresh
        token against that workload via `GetWorkloadAccessTokenForUserId`,
        regardless of whether a context token is present. The runtime
        injects its own workload token (bound to its auto-created
        service-linked identity), but vault entries are keyed by workload
        — so using the runtime-injected token here would fragment the vault
        across services. Both APIs mint against the same shared platform
        workload identity instead, so app-api's settings-page consent flow
        and the runtime's agent loop see the same vaulted tokens.

        When the env var is not set (tests, legacy code paths), fall back
        to the runtime-injected context token if present, otherwise raise.
        """
        workload_name = os.environ.get(_RUNTIME_WORKLOAD_ENV)
        if workload_name:
            if not user_id:
                raise WorkloadTokenUnavailableError(
                    f"{_RUNTIME_WORKLOAD_ENV} is set but no user_id was "
                    "provided. Workload token minting requires a user_id."
                )
            logger.info(
                "Minting workload access token for user=%s workload=%s",
                user_id,
                workload_name,
            )
            response = self._control_client.get_workload_access_token_for_user_id(
                workloadName=workload_name,
                userId=user_id,
            )
            minted_token = response.get("workloadAccessToken")
            if not minted_token:
                raise WorkloadTokenUnavailableError(
                    "GetWorkloadAccessTokenForUserId returned no token"
                )
            return minted_token

        context_token = BedrockAgentCoreContext.get_workload_access_token()
        if context_token:
            return context_token

        raise WorkloadTokenUnavailableError(
            f"No WorkloadAccessToken on context and {_RUNTIME_WORKLOAD_ENV} "
            "is unset. Set the env var to the shared platform workload "
            "identity name (exported as "
            "/<projectPrefix>/oauth/platform-workload-identity-name)."
        )


_default_client: Optional[AgentCoreIdentityClient] = None


def get_agentcore_identity_client() -> AgentCoreIdentityClient:
    """Return the process-wide `AgentCoreIdentityClient` singleton."""
    global _default_client
    if _default_client is None:
        _default_client = AgentCoreIdentityClient()
    return _default_client
