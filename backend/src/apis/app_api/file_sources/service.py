"""Helpers shared by the user-facing file-source endpoints.

A connector becomes a *file source* only when an admin maps it to a
file-source adapter. These helpers centralize the "resolve a connector to a
usable adapter + token" steps so the browse/search routes and the document
import flow stay consistent with each other and with the connector
status/consent routes.
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

from apis.app_api.file_sources.adapter import FileSourceAdapter
from apis.app_api.file_sources.models import (
    FileSourceAuthError,
    FileSourceError,
    FileSourceNotFoundError,
)
from apis.app_api.file_sources.registry import registry

logger = logging.getLogger(__name__)


def connector_visible_to_user(
    provider: OAuthProvider, user_role_ids: List[str]
) -> bool:
    """True when an enabled connector is usable by a user with these roles.

    An empty `allowed_roles` list means unrestricted access; a non-empty
    list grants access to users who share at least one AppRole id. Mirrors
    the visibility rule the connector catalog route applies.
    """
    if not provider.enabled:
        return False
    if not provider.allowed_roles:
        return True
    return bool(set(provider.allowed_roles) & set(user_role_ids))


async def resolve_file_source(
    connector_id: str,
    current_user: User,
    provider_repo: OAuthProviderRepository,
    role_service: AppRoleService,
) -> Tuple[OAuthProvider, FileSourceAdapter]:
    """Resolve a connector id to its provider record and file-source adapter.

    Raises `HTTPException` (404/403) when the connector is missing, disabled,
    not visible to the caller, not configured as a file source, or mapped to
    an adapter that is not shipped in this release. Request-context only —
    the async import task does its own resolution.
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

    if not provider.file_source_adapter_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connector '{connector_id}' is not configured as a file source",
        )

    adapter = registry.get(provider.file_source_adapter_id)
    if adapter is None:
        # An admin mapped an adapter key that no longer ships in this release.
        # Indistinguishable from "not a file source" to the user.
        logger.error(
            "Connector %s maps to unknown file-source adapter '%s'",
            connector_id,
            provider.file_source_adapter_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Connector '{connector_id}' is not configured as a file source",
        )
    return provider, adapter


async def resolve_file_source_token(
    provider: OAuthProvider, user_id: str
) -> TokenResult:
    """Fetch the user's OAuth token for a file-source connector.

    Returns a `TokenResult`: `access_token` is populated when the vault has a
    usable token, `authorization_url` when the user still needs to consent.

    `custom_parameters` is built with `force_authentication=True` so it matches
    the consent flow. AgentCore factors `customParameters` into whether
    `get_resource_oauth2_token` short-circuits to a vaulted token: connector
    consent always runs through `initiate_consent`, which sends Google's
    `prompt=consent` extra — so a retrieval call that omits it is treated as a
    different request and reports consent-required even though a usable token
    is vaulted. This is a pure read; `force_authentication` stays False on
    `get_token_for_user` itself.
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


async def require_file_source_token(provider: OAuthProvider, user_id: str) -> str:
    """Resolve a usable OAuth access token for a file-source connector.

    Wraps `resolve_file_source_token` and turns its two non-token outcomes
    into `HTTPException`s the route layer can return unchanged:

    - the user has not completed OAuth consent -> 409 Conflict
    - AgentCore workload/callback context is unavailable -> 503

    Returns the bare access-token string on success. The browse/search and
    import flows all require a real token, so there is no caller that wants
    the `authorization_url` branch.
    """
    try:
        result = await resolve_file_source_token(provider, user_id)
    except (WorkloadTokenUnavailableError, CallbackUrlUnavailableError) as err:
        logger.warning(
            "File-source token resolution failed for %s: %s",
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
                "Complete the OAuth consent flow before using it as a file source."
            ),
        )
    return result.access_token


def http_error_for_file_source_error(err: FileSourceError) -> HTTPException:
    """Map a file-source adapter error onto an HTTP response.

    - `FileSourceAuthError` -> 403 (token rejected / missing scopes)
    - `FileSourceNotFoundError` -> 404 (file or folder gone)
    - any other `FileSourceError` -> 502 (the provider call itself failed)
    """
    if isinstance(err, FileSourceAuthError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "The file source rejected the request. Reconnect the "
                "connector and try again."
            ),
        )
    if isinstance(err, FileSourceNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested file or folder no longer exists.",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="The file source could not be reached. Try again shortly.",
    )
