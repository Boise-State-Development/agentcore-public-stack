"""Helpers shared by the export-target endpoints.

A connector becomes an *export target* only when an admin maps it to an
export-target adapter. These helpers centralize the "resolve a connector to a
usable adapter + token" steps so the export flow stays consistent with the
connector status/consent routes — the write-side mirror of
`file_sources.service`.
"""

import logging
from typing import List, Tuple

from fastapi import HTTPException, status

from apis.shared.auth import User
from apis.shared.oauth.agentcore_identity import (
    CallbackUrlUnavailableError,
    TokenResult,
    WorkloadTokenUnavailableError,
    custom_parameters_for,
    get_agentcore_identity_client,
)
from apis.shared.oauth.models import OAuthProvider
from apis.shared.oauth.provider_repository import OAuthProviderRepository
from apis.shared.rbac.service import AppRoleService

from apis.app_api.export_targets.adapter import ExportTargetAdapter
from apis.app_api.export_targets.models import (
    ExportTargetAuthError,
    ExportTargetError,
    ExportTargetNotFoundError,
)
from apis.app_api.export_targets.registry import registry

logger = logging.getLogger(__name__)


def connector_visible_to_user(
    provider: OAuthProvider, user_role_ids: List[str]
) -> bool:
    """True when an enabled connector is usable by a user with these roles.

    An empty `allowed_roles` list means unrestricted access; a non-empty list
    grants access to users who share at least one AppRole id. Mirrors the
    connector catalog's visibility rule.
    """
    if not provider.enabled:
        return False
    if not provider.allowed_roles:
        return True
    return bool(set(provider.allowed_roles) & set(user_role_ids))


async def resolve_export_target(
    connector_id: str,
    current_user: User,
    provider_repo: OAuthProviderRepository,
    role_service: AppRoleService,
) -> Tuple[OAuthProvider, ExportTargetAdapter]:
    """Resolve a connector id to its provider record and export-target adapter.

    Raises `HTTPException` (404/403) when the connector is missing, disabled,
    not visible to the caller, not configured as an export target, or mapped
    to an adapter that is not shipped in this release.
    """
    provider = await provider_repo.get_provider(connector_id)
    if not provider or not provider.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connector '{connector_id}' not found",
        )

    permissions = await role_service.resolve_user_permissions(current_user)
    if not connector_visible_to_user(provider, permissions.app_roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this connector",
        )

    if not provider.export_target_adapter_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connector '{connector_id}' is not configured as an export target",
        )

    adapter = registry.get(provider.export_target_adapter_id)
    if adapter is None:
        # An admin mapped an adapter key that no longer ships in this release.
        # Indistinguishable from "not an export target" to the user.
        logger.error(
            "Connector %s maps to unknown export-target adapter '%s'",
            connector_id,
            provider.export_target_adapter_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connector '{connector_id}' is not configured as an export target",
        )
    return provider, adapter


async def resolve_export_target_token(
    provider: OAuthProvider, user_id: str
) -> TokenResult:
    """Fetch the user's OAuth token for an export-target connector.

    Returns a `TokenResult`: `access_token` is populated when the vault has a
    usable token, `authorization_url` when the user still needs to consent.

    `custom_parameters` is built with `force_authentication=True` so it matches
    the consent flow — AgentCore factors `customParameters` into whether
    `get_resource_oauth2_token` short-circuits to a vaulted token (see the
    file-source service for the full rationale). Pure read; `force_authentication`
    stays False on `get_token_for_user` itself.
    """
    identity = get_agentcore_identity_client()
    return await identity.get_token_for_user(
        provider_name=provider.provider_id,
        scopes=provider.scopes,
        user_id=user_id,
        custom_parameters=custom_parameters_for(
            provider.provider_type.value,
            provider.custom_parameters,
            force_authentication=True,
        ),
    )


async def require_export_target_token(provider: OAuthProvider, user_id: str) -> str:
    """Resolve a usable OAuth access token for an export-target connector.

    Turns the two non-token outcomes into `HTTPException`s the route layer can
    return unchanged:

    - the user has not completed OAuth consent -> 409 Conflict
    - AgentCore workload/callback context is unavailable -> 503

    Returns the bare access-token string on success.
    """
    try:
        result = await resolve_export_target_token(provider, user_id)
    except (WorkloadTokenUnavailableError, CallbackUrlUnavailableError) as err:
        logger.warning(
            "Export-target token resolution failed for %s: %s",
            provider.provider_id,
            err,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(err),
        )

    if result.requires_consent or not result.access_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Connector '{provider.provider_id}' is not connected. "
                "Complete the OAuth consent flow before saving to it."
            ),
        )
    return result.access_token


def http_error_for_export_target_error(err: ExportTargetError) -> HTTPException:
    """Map an export-target adapter error onto an HTTP response.

    - `ExportTargetAuthError` -> 403 (token rejected / missing scopes)
    - `ExportTargetNotFoundError` -> 404 (destination folder gone)
    - any other `ExportTargetError` -> 502 (the provider call itself failed)
    """
    if isinstance(err, ExportTargetAuthError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "The export target rejected the request. Reconnect the "
                "connector and try again."
            ),
        )
    if isinstance(err, ExportTargetNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The destination folder no longer exists.",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="The export target could not be reached. Try again shortly.",
    )
