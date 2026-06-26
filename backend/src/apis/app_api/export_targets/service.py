"""FastAPI helpers for the export-target endpoints.

A connector becomes an *export target* only when an admin maps it to an
export-target adapter. These helpers wrap the boundary-free core
(`apis.shared.export_targets.service`) with the FastAPI `HTTPException` mapping
the route layer returns directly — the write-side mirror of
`file_sources.service`.

`connector_visible_to_user` and `resolve_export_target_token` are re-exported
from the shared core so existing imports of this module keep working; the
agent-side `save_conversation` tool imports them from `apis.shared` instead.
"""

import logging

from fastapi import HTTPException, status

from apis.shared.auth import User
from apis.shared.oauth.agentcore_identity import (
    CallbackUrlUnavailableError,
    WorkloadTokenUnavailableError,
)
from apis.shared.oauth.models import OAuthProvider
from apis.shared.oauth.provider_repository import OAuthProviderRepository
from apis.shared.rbac.service import AppRoleService
from apis.shared.export_targets.adapter import ExportTargetAdapter
from apis.shared.export_targets.models import (
    ExportTargetAuthError,
    ExportTargetError,
    ExportTargetNotFoundError,
)
from apis.shared.export_targets.registry import registry
from apis.shared.export_targets.service import (
    connector_visible_to_user,
    resolve_export_target_token,
)

logger = logging.getLogger(__name__)

__all__ = [
    "connector_visible_to_user",
    "resolve_export_target_token",
    "resolve_export_target",
    "require_export_target_token",
    "http_error_for_export_target_error",
    "registry",
]


async def resolve_export_target(
    connector_id: str,
    current_user: User,
    provider_repo: OAuthProviderRepository,
    role_service: AppRoleService,
) -> tuple[OAuthProvider, ExportTargetAdapter]:
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
