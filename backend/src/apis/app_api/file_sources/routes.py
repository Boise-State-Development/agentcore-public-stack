"""User-facing file-source endpoints: catalog, roots, browse, search.

A connector becomes a *file source* only once an admin maps it to a
file-source adapter (PR #367). These endpoints let a signed-in user discover
which of their connectors are usable as file sources and walk the provider's
folder tree so they can pick files to import into an assistant's RAG index.

Browse/search/roots are mounted under `/connectors/{provider_id}` — the same
namespace as the connector consent routes — because they operate on a
connector; the catalog lives at `/file-sources`. Like the connector routes,
they live on the app API: the AgentCore Runtime that fronts the inference API
only proxies `/invocations` and `/ping`, so custom paths are unreachable
there.
"""

import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.oauth.agentcore_identity import (
    CallbackUrlUnavailableError,
    WorkloadTokenUnavailableError,
)
from apis.shared.oauth.disconnect_repository import (
    OAuthDisconnectRepository,
    get_disconnect_repository,
)
from apis.shared.oauth.models import OAuthProvider
from apis.shared.oauth.provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)
from apis.shared.rbac.service import AppRoleService, get_app_role_service

from apis.app_api.file_sources.models import BrowseResult, FileSourceError, SourceRoot
from apis.app_api.file_sources.service import (
    connector_visible_to_user,
    http_error_for_file_source_error,
    require_file_source_token,
    resolve_file_source,
    resolve_file_source_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["file-sources"])


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class FileSourceConnector(BaseModel):
    """A connector the current user can use as a file source."""

    model_config = ConfigDict(populate_by_name=True)

    provider_id: str = Field(..., alias="providerId")
    display_name: str = Field(..., alias="displayName")
    icon_name: str = Field(..., alias="iconName")
    icon_data: Optional[str] = Field(None, alias="iconData")
    # True when AgentCore's vault holds a usable token for this user — the SPA
    # can browse straight away. False means it must run the consent flow first.
    connected: bool


class FileSourceListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_sources: List[FileSourceConnector] = Field(..., alias="fileSources")


class SourceRootsResponse(BaseModel):
    """The top-level browsing roots a file source exposes."""

    roots: List[SourceRoot]


async def _is_connected(
    provider: OAuthProvider,
    user_id: str,
    disconnect_repo: OAuthDisconnectRepository,
) -> bool:
    """Best-effort check of whether the user has a usable token.

    Mirrors `connector_status`: a prior disconnect wins over a still-valid
    vault entry. Workload/callback misconfiguration is treated as
    "not connected" rather than failing the whole catalog — the user gets the
    actionable 503 when they click Connect.
    """
    if await disconnect_repo.is_disconnected(user_id, provider.provider_id):
        return False
    try:
        result = await resolve_file_source_token(provider, user_id)
    except (WorkloadTokenUnavailableError, CallbackUrlUnavailableError) as err:
        logger.warning(
            "File-source connectivity check failed for %s: %s",
            provider.provider_id,
            err,
        )
        return False
    return not result.requires_consent


@router.get("/file-sources", response_model=FileSourceListResponse)
async def list_file_sources(
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
    disconnect_repo: OAuthDisconnectRepository = Depends(get_disconnect_repository),
) -> FileSourceListResponse:
    """List the connectors the current user can use as a file source.

    A connector qualifies when it is enabled, mapped to a file-source adapter,
    and visible to the user's roles. `connected` reflects whether the user
    already has a usable OAuth token, so the SPA can decide between "Browse"
    and "Connect".
    """
    permissions = await role_service.resolve_user_permissions(current_user)
    providers = await provider_repo.list_providers(enabled_only=True)
    file_sources = [
        p
        for p in providers
        if p.file_source_adapter_id
        and connector_visible_to_user(p, permissions.app_roles)
    ]

    connected_flags = await asyncio.gather(
        *(
            _is_connected(p, current_user.user_id, disconnect_repo)
            for p in file_sources
        )
    )

    return FileSourceListResponse(
        file_sources=[
            FileSourceConnector(
                provider_id=p.provider_id,
                display_name=p.display_name,
                icon_name=p.icon_name,
                icon_data=p.icon_data,
                connected=connected,
            )
            for p, connected in zip(file_sources, connected_flags)
        ]
    )


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------


@router.get(
    "/connectors/{provider_id}/roots",
    response_model=SourceRootsResponse,
)
async def list_file_source_roots(
    provider_id: str,
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> SourceRootsResponse:
    """List the top-level browsing roots for a file-source connector."""
    provider, adapter = await resolve_file_source(
        provider_id, current_user, provider_repo, role_service
    )
    access_token = await require_file_source_token(provider, current_user.user_id)
    try:
        roots = await adapter.list_roots(access_token)
    except FileSourceError as err:
        logger.warning("list_roots failed for connector %s: %s", provider_id, err)
        raise http_error_for_file_source_error(err)
    return SourceRootsResponse(roots=roots)


@router.get(
    "/connectors/{provider_id}/browse",
    response_model=BrowseResult,
)
async def browse_file_source(
    provider_id: str,
    folder_id: str = Query(
        ..., min_length=1, description="Folder (or root) id to list"
    ),
    cursor: Optional[str] = Query(None, description="Opaque pagination cursor"),
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> BrowseResult:
    """List one page of a folder's contents in a file-source connector."""
    provider, adapter = await resolve_file_source(
        provider_id, current_user, provider_repo, role_service
    )
    access_token = await require_file_source_token(provider, current_user.user_id)
    try:
        return await adapter.browse(access_token, folder_id, cursor)
    except FileSourceError as err:
        logger.warning("browse failed for connector %s: %s", provider_id, err)
        raise http_error_for_file_source_error(err)


@router.get(
    "/connectors/{provider_id}/search",
    response_model=BrowseResult,
)
async def search_file_source(
    provider_id: str,
    query: str = Query(..., min_length=1, description="Free-text search query"),
    cursor: Optional[str] = Query(None, description="Opaque pagination cursor"),
    current_user: User = Depends(get_current_user_from_session),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> BrowseResult:
    """Search a file-source connector by free-text query, one page at a time."""
    provider, adapter = await resolve_file_source(
        provider_id, current_user, provider_repo, role_service
    )
    access_token = await require_file_source_token(provider, current_user.user_id)
    try:
        return await adapter.search(access_token, query, cursor)
    except FileSourceError as err:
        logger.warning("search failed for connector %s: %s", provider_id, err)
        raise http_error_for_file_source_error(err)
