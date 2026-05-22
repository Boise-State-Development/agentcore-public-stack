"""User-facing connector routes: catalog, status, consent, disconnect.

The settings page calls these endpoints to render the connector list with
"Connected" badges, kick off OAuth consent flows, finalize them after the
popup returns, and disconnect a previously-granted connector.

Lives on the app API rather than the inference API because the app API is
reachable at arbitrary HTTP paths via its ALB. The AgentCore Runtime that
fronts the inference API only proxies `/invocations` and `/ping`, so custom
paths like `/connectors/{id}/status` get a 404 from AWS before reaching the
container.

The IdentityClient mints workload access tokens against the shared
platform workload identity (configured via `AGENTCORE_RUNTIME_WORKLOAD_NAME`,
populated from `/<projectPrefix>/oauth/platform-workload-identity-name`).
The runtime's auto-created workload identity is service-linked and only
mintable from inside the runtime container, so we own a separate identity
that both APIs share. That keeps the OAuth vault unified — tokens vaulted
from the settings page here are visible to the agent loop on inference-api.

The frontend supplies `OAuth2CallbackUrl` as an explicit header on these
calls; `AgentCoreContextMiddleware` (installed on app-api in `main.py`)
bridges it onto `BedrockAgentCoreContext` for the IdentityClient to read.
"""

import logging
import os
from functools import lru_cache
from typing import List, Optional

import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.oauth.agentcore_identity import (
    CallbackUrlUnavailableError,
    WorkloadTokenUnavailableError,
    custom_parameters_for,
    get_agentcore_identity_client,
)
from apis.shared.oauth.disconnect_repository import (
    OAuthDisconnectRepository,
    get_disconnect_repository,
)
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType
from apis.shared.oauth.provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)
from apis.shared.rbac.service import AppRoleService, get_app_role_service

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _agentcore_control_client():
    """Process-wide bedrock-agentcore control-plane client.

    Cached so `complete_consent` doesn't reconstruct the boto3 client
    (and re-resolve credentials) on every request.
    """
    region = os.environ.get("AWS_REGION", "us-west-2")
    return boto3.client("bedrock-agentcore", region_name=region)


router = APIRouter(prefix="/connectors", tags=["connectors"])


# ---------------------------------------------------------------------------
# Catalog (list)
# ---------------------------------------------------------------------------


class UserConnector(BaseModel):
    """Connector as visible to a signed-in user.

    Drops admin-only fields (ARN, callback URL, role allow-list) and keeps
    only what the settings page needs to render.
    """

    provider_id: str
    display_name: str
    provider_type: OAuthProviderType
    icon_name: str
    icon_data: Optional[str] = None
    scopes: List[str]


class UserConnectorListResponse(BaseModel):
    connectors: List[UserConnector]


def _visible_to_user(provider: OAuthProvider, user_role_ids: List[str]) -> bool:
    """True when the user is allowed to use this connector.

    An empty `allowed_roles` list means unrestricted access. A non-empty
    list grants access to users who share at least one AppRole ID.
    """
    if not provider.enabled:
        return False
    if not provider.allowed_roles:
        return True
    return bool(set(provider.allowed_roles) & set(user_role_ids))


@router.get("/", response_model=UserConnectorListResponse)
async def list_connectors(
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> UserConnectorListResponse:
    """List enabled connectors available to the current user."""
    permissions = await role_service.resolve_user_permissions(current_user)
    providers = await provider_repo.list_providers(enabled_only=True)
    visible = [p for p in providers if _visible_to_user(p, permissions.app_roles)]
    return UserConnectorListResponse(
        connectors=[
            UserConnector(
                provider_id=p.provider_id,
                display_name=p.display_name,
                provider_type=p.provider_type,
                icon_name=p.icon_name,
                icon_data=p.icon_data,
                scopes=p.scopes,
            )
            for p in visible
        ]
    )


# ---------------------------------------------------------------------------
# Per-provider consent / status / disconnect
# ---------------------------------------------------------------------------


class InitiateConsentResponse(BaseModel):
    """Either a pending consent URL or a confirmation of existing access."""

    connected: bool = False
    authorization_url: Optional[str] = None


class ConnectorStatusResponse(BaseModel):
    """Whether the caller has a usable token in AgentCore's vault.

    Side-effect-free: unlike `initiate-consent`, this endpoint discards
    the authorization URL when consent is required, and does NOT remember
    the session_uri server-side. Use it from listing UIs that need a
    "Connected" badge without committing the user to a consent flow.
    """

    connected: bool = False


class CompleteConsentRequest(BaseModel):
    """Body for finalizing a consent flow after the popup returns."""

    session_uri: str
    # Constrain to the same shape as OAuthProviderCreate.provider_id so a
    # malicious client can't smuggle newlines or control characters into the
    # log lines that echo this field (CWE-117 / log injection). Pydantic
    # rejects mismatches at parse time with a 422.
    provider_id: Optional[str] = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$"
    )


class CompleteConsentResponse(BaseModel):
    ok: bool = True


async def _resolve_visible_provider(
    provider_id: str,
    current_user: User,
    provider_repo: OAuthProviderRepository,
    role_service: AppRoleService,
) -> OAuthProvider:
    """Fetch a provider and 404/403 if it isn't visible to the caller.

    Centralizes the lookup so `initiate_consent` and `connector_status`
    use identical visibility rules.
    """
    provider = await provider_repo.get_provider(provider_id)
    if not provider or not provider.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connector '{provider_id}' not found",
        )

    permissions = await role_service.resolve_user_permissions(current_user)
    if not _visible_to_user(provider, permissions.app_roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this connector",
        )
    return provider


@router.post(
    "/{provider_id}/initiate-consent",
    response_model=InitiateConsentResponse,
)
async def initiate_consent(
    provider_id: str,
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
    disconnect_repo: OAuthDisconnectRepository = Depends(get_disconnect_repository),
) -> InitiateConsentResponse:
    """Start (or verify) AgentCore consent for the given provider."""
    provider = await _resolve_visible_provider(
        provider_id, current_user, provider_repo, role_service
    )

    # If the user previously disconnected, force a fresh consent flow even
    # though AgentCore's vault still holds an unexpired token — they
    # explicitly opted out, and re-using the cached entry would silently
    # undo that.
    force_auth = await disconnect_repo.is_disconnected(
        current_user.user_id, provider.provider_id
    )

    identity = get_agentcore_identity_client()
    try:
        result = await identity.get_token_for_user(
            provider_name=provider.provider_id,
            scopes=provider.scopes,
            user_id=current_user.user_id,
            force_authentication=force_auth,
            # initiate_consent is by definition a "walk me through consent"
            # path — the user clicked Connect/Reconnect after seeing they
            # were not connected. For Google this means we must send
            # `prompt=consent`, otherwise Google sees a previously-consented
            # user, skips the consent screen, and re-issues an access_token
            # WITHOUT a refresh_token. Vault then expires after ~1h and the
            # user is back here. Decoupled from `force_auth` (which only
            # toggles the SDK's vault-bypass) so the silent-refresh fast
            # path elsewhere is unaffected.
            custom_parameters=custom_parameters_for(
                provider.provider_type.value,
                provider.custom_parameters,
                force_authentication=True,
            ),
            # No custom_state: AgentCore appears to treat its presence as a
            # signal to start a fresh flow, never short-circuiting to the
            # cached token. The frontend passes provider_id via the
            # callback URL query string so /oauth-complete still knows
            # which provider resolved.
        )
    except WorkloadTokenUnavailableError as err:
        # AGENTCORE_RUNTIME_WORKLOAD_NAME isn't set, or the IAM mint call
        # was denied. Surface a clear error instead of a 500.
        logger.warning("Consent initiation without workload context: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "AgentCore workload context unavailable. Set "
                "AGENTCORE_RUNTIME_WORKLOAD_NAME to the shared platform "
                "workload identity and grant the task role "
                "bedrock-agentcore:GetWorkloadAccessTokenForUserId."
            ),
        )
    except CallbackUrlUnavailableError as err:
        # Frontend is expected to send the OAuth2CallbackUrl header on this
        # path; if the header is missing AND the env-var fallback is unset,
        # tell the caller exactly what to fix.
        logger.warning("Consent initiation missing callback URL: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(err),
        )

    if result.requires_consent:
        return InitiateConsentResponse(authorization_url=result.authorization_url)
    return InitiateConsentResponse(connected=True)


@router.get(
    "/{provider_id}/status",
    response_model=ConnectorStatusResponse,
)
async def connector_status(
    provider_id: str,
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
    disconnect_repo: OAuthDisconnectRepository = Depends(get_disconnect_repository),
) -> ConnectorStatusResponse:
    """Report whether AgentCore's vault has a usable token for this caller.

    Side-effect-free read: when the vault is empty we discard the
    authorization URL the SDK returns. The settings page uses this to
    decorate the list with a "Connected" badge without committing the
    user to a flow.

    GET so it's cache-friendly and idempotent. The HTTP status only
    reflects request validity (401/403/404/503); whether the user is
    *connected* is in the response body.
    """
    provider = await _resolve_visible_provider(
        provider_id, current_user, provider_repo, role_service
    )

    # User just disconnected — they're not connected, regardless of what
    # AgentCore's vault still holds. This avoids a misleading "Connected"
    # badge between disconnect and the next re-consent.
    if await disconnect_repo.is_disconnected(
        current_user.user_id, provider.provider_id
    ):
        return ConnectorStatusResponse(connected=False)

    identity = get_agentcore_identity_client()
    try:
        result = await identity.get_token_for_user(
            provider_name=provider.provider_id,
            scopes=provider.scopes,
            user_id=current_user.user_id,
            # Match the customParameters `initiate_consent` used to grant the
            # token. AgentCore factors `customParameters` into whether
            # `get_resource_oauth2_token` short-circuits to a vaulted token, so
            # a status read that omits Google's `prompt=consent` is treated as
            # a fresh request and reports consent-required even when a usable
            # token is vaulted. Pure read — `get_token_for_user` itself stays
            # at `force_authentication=False`.
            custom_parameters=custom_parameters_for(
                provider.provider_type.value,
                provider.custom_parameters,
                force_authentication=True,
            ),
        )
    except WorkloadTokenUnavailableError as err:
        logger.warning("Status check without workload context: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(err),
        )
    except CallbackUrlUnavailableError as err:
        logger.warning("Status check missing callback URL: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(err),
        )

    return ConnectorStatusResponse(connected=not result.requires_consent)


@router.post(
    "/complete-consent",
    response_model=CompleteConsentResponse,
)
async def complete_consent(
    body: CompleteConsentRequest,
    current_user: User = Depends(get_current_user_from_session),
    disconnect_repo: OAuthDisconnectRepository = Depends(get_disconnect_repository),
) -> CompleteConsentResponse:
    """Finalize an OAuth consent flow after the popup redirects home.

    AgentCore's `/identities/oauth2/authorize` redirect comes back with the
    same `request_uri` it was initiated with (as `session_id` on our landing
    page). Until we call `CompleteResourceTokenAuth` with that URI and the
    user's identity, AgentCore treats the flow as unfinished and the token
    vault stays empty — the next `GetResourceOauth2Token` call returns a
    fresh authorization URL.

    Returns `ok: true` on success; errors from AgentCore bubble up as 502.

    Authorization: the inbound session (`current_user`) is verified by
    `get_current_user_from_session`, and we pass that user's id as `userIdentifier` to
    AgentCore. AgentCore's own binding rejects a completion attempt whose
    `userIdentifier` doesn't match the identity that initiated the session,
    so a leaked `session_uri` cannot be redeemed under a different user.
    """
    control = _agentcore_control_client()

    try:
        control.complete_resource_token_auth(
            userIdentifier={"userId": current_user.user_id},
            sessionUri=body.session_uri,
        )
    except Exception as err:
        logger.error(
            "CompleteResourceTokenAuth failed for user=%s provider=%s: %s",
            current_user.user_id,
            body.provider_id,
            err,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to finalize OAuth consent: {err}",
        )

    # Successful re-consent supersedes any prior disconnect — clear the
    # durable flag so subsequent status checks report the user as connected
    # without waiting for the agent loop to warm the cache.
    if body.provider_id:
        await disconnect_repo.clear_disconnected(
            current_user.user_id, body.provider_id
        )

    logger.info(
        "Completed OAuth consent for user=%s provider=%s",
        current_user.user_id,
        body.provider_id,
    )
    return CompleteConsentResponse(ok=True)


@router.delete(
    "/{provider_id}/connection",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def disconnect_connector(
    provider_id: str,
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
    disconnect_repo: OAuthDisconnectRepository = Depends(get_disconnect_repository),
):
    """Best-effort disconnect for the caller's connection to this provider.

    AgentCore Identity exposes no per-user vault-delete API, so we cannot
    actually destroy the user's stored token. What we can do: persist the
    disconnect intent in DynamoDB so every replica's agent loop and
    `/status` endpoint reads the same state — the next attempt to use the
    connector triggers a fresh consent flow with `force_authentication=True`,
    which makes AgentCore replace the vault entry rather than reuse it.

    The inference-api process keeps a per-replica in-memory token cache as
    an optimization. We don't clear it from here — that cache is in another
    process, unreachable across the network — but the consent hook on the
    inference side reads `disconnect_repo` on every gate call, so the
    next tool invocation rejects the cached token regardless.

    The existing vault entry stays valid at the upstream provider until it
    expires naturally or the user revokes the application from their
    provider account (e.g. https://myaccount.google.com/connections). This
    is documented as part of the disconnect UX.
    """
    provider = await _resolve_visible_provider(
        provider_id, current_user, provider_repo, role_service
    )

    await disconnect_repo.mark_disconnected(
        current_user.user_id, provider.provider_id
    )
    logger.info(
        "Marked connector for re-consent on next use: user=%s provider=%s",
        current_user.user_id,
        provider.provider_id,
    )
    return None
