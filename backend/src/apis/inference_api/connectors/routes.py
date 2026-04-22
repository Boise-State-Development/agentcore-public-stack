"""User-initiated OAuth consent for connectors.

Lives on the inference API because the AgentCore Runtime injects the
workload access token via `AgentCoreContextMiddleware` on every request
proxied through `InvokeAgentRuntime`. `IdentityClient.get_token` reads
that token from `BedrockAgentCoreContext`, which is only populated here
— never on the app API.

Flow: the settings page posts to `/connectors/{id}/initiate-consent`.
If AgentCore already has a valid token for this user + provider, we
return `{connected: true}` so the UI can show a success state. If
consent is required, AgentCore hands us back an authorization URL and we
forward it for the frontend popup.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from agents.main_agent.integrations.agentcore_identity import (
    WorkloadTokenUnavailableError,
    get_agentcore_identity_client,
)
from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.auth.models import User
from apis.shared.oauth import session_cache
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


class InitiateConsentResponse(BaseModel):
    """Either a pending consent URL or a confirmation of existing access."""

    connected: bool = False
    authorization_url: str | None = None


class CompleteConsentRequest(BaseModel):
    """Body for finalizing a consent flow after the popup returns."""

    session_uri: str
    provider_id: str | None = None


class CompleteConsentResponse(BaseModel):
    ok: bool = True


def _is_visible_to_user(provider, user_role_ids: list[str]) -> bool:
    if not provider.enabled:
        return False
    if not provider.allowed_roles:
        return True
    return bool(set(provider.allowed_roles) & set(user_role_ids))


@router.post(
    "/{provider_id}/initiate-consent",
    response_model=InitiateConsentResponse,
)
async def initiate_consent(
    provider_id: str,
    current_user: User = Depends(get_current_user_trusted),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> InitiateConsentResponse:
    """Start (or verify) AgentCore consent for the given provider."""
    provider = await provider_repo.get_provider(provider_id)
    if not provider or not provider.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connector '{provider_id}' not found",
        )

    permissions = await role_service.resolve_user_permissions(current_user)
    if not _is_visible_to_user(provider, permissions.app_roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this connector",
        )

    identity = get_agentcore_identity_client()
    try:
        result = await identity.get_token_for_user(
            provider_name=provider.provider_id,
            scopes=provider.scopes,
            user_id=current_user.user_id,
            # No custom_state: AgentCore appears to treat its presence as a
            # signal to start a fresh flow, never short-circuiting to the
            # cached token. The frontend passes provider_id via the
            # callback URL query string so /oauth-complete still knows
            # which provider resolved.
        )
    except WorkloadTokenUnavailableError as err:
        # Only happens when the route is called outside an AgentCore Runtime
        # invocation (e.g. local dev without the runtime proxy). Surface a
        # clear error instead of a 500.
        logger.warning("Consent initiation attempted without workload context: %s", err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "AgentCore workload context is not available. In prod, this "
                "endpoint runs under the runtime proxy; locally, set "
                "AGENTCORE_RUNTIME_WORKLOAD_NAME to enable the mint fallback."
            ),
        )

    if result.requires_consent:
        # Record the session_uri AgentCore embedded in the authorize URL so
        # `complete_consent` can prove this user is the one who started the
        # flow. We soft-fail when the URL doesn't carry an identifier we
        # recognise — AgentCore's own binding still applies in that case,
        # and logs capture the shape change for investigation.
        session_uri = session_cache.extract_session_uri(result.authorization_url or "")
        if session_uri:
            session_cache.remember(current_user.user_id, session_uri)
        else:
            logger.warning(
                "Could not extract session_uri from AgentCore authorization URL "
                "for user=%s provider=%s; skipping server-side session tracking",
                current_user.user_id,
                provider_id,
            )
        return InitiateConsentResponse(authorization_url=result.authorization_url)
    return InitiateConsentResponse(connected=True)


@router.post(
    "/complete-consent",
    response_model=CompleteConsentResponse,
)
async def complete_consent(
    body: CompleteConsentRequest,
    current_user: User = Depends(get_current_user_trusted),
) -> CompleteConsentResponse:
    """Finalize an OAuth consent flow after the popup redirects home.

    AgentCore's `/identities/oauth2/authorize` redirect comes back with the
    same `request_uri` it was initiated with (as `session_id` on our landing
    page). Until we call `CompleteResourceTokenAuth` with that URI and the
    user's identity, AgentCore treats the flow as unfinished and the token
    vault stays empty — the next `GetResourceOauth2Token` call returns a
    fresh authorization URL.

    Returns `ok: true` on success; errors from AgentCore bubble up as 502.

    Authorization: the submitted `session_uri` must have been remembered
    for `current_user` at `initiate_consent`. This is defence-in-depth on
    top of AgentCore's own `userIdentifier`-bound check, so that a
    leaked `session_uri` cannot be driven to completion under a
    different user's identity here.
    """
    if not session_cache.consume(current_user.user_id, body.session_uri):
        logger.warning(
            "Rejecting complete_consent: session_uri not issued to user=%s provider=%s",
            current_user.user_id,
            body.provider_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This consent session was not initiated by you, or it has "
                "expired. Start the connection again from Settings."
            ),
        )

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

    logger.info(
        "Completed OAuth consent for user=%s provider=%s",
        current_user.user_id,
        body.provider_id,
    )
    return CompleteConsentResponse(ok=True)
