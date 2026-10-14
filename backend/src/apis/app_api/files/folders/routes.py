"""Folder attachment endpoints (``docs/specs/drive-folder-attachments.md`` §4.2).

    POST   /sessions/{session_id}/folders                       attach a folder
    GET    /sessions/{session_id}/folders/{folder_attach_id}    poll the row
    DELETE /sessions/{session_id}/folders/{folder_attach_id}    remove it and its files
    POST   /sessions/{session_id}/folders/{folder_attach_id}/resume   re-run a lost walk
    POST   /sessions/{session_id}/folders/{folder_attach_id}/refresh  bring the files up to date
    GET    /sessions/{session_id}/folders                       the conversation's folders

All 404 to a signed-in user while ``FOLDER_ATTACHMENTS_ENABLED`` is off
(401 unauthenticated regardless). Attach and resume resolve
the connector exactly as ``GET /connectors/{id}/browse`` does (404 unknown or
not a file source, 403 not visible to the user, 409 needs consent, 503
identity unavailable), then hand the adapter and the user's vaulted token to
the expansion task. Rows are owner-scoped by the table's partition key and
checked against the session in the path.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.feature_flags import folder_attachments_enabled
from apis.shared.files.folder_attachments import FolderAttachment
from apis.shared.oauth.provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)
from apis.shared.rbac.service import AppRoleService, get_app_role_service

from apis.app_api.file_sources.service import (
    require_file_source_token,
    resolve_file_source,
)
from apis.app_api.files.folders.service import (
    FolderAttachmentService,
    get_folder_attachment_service,
)

logger = logging.getLogger(__name__)


async def require_folder_attachments_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """The signed-in user, or 404 while the flag is off.

    Authentication runs first (the ``memory_spaces`` pattern), so an
    unauthenticated call is a 401 whatever the flag, and the surface only
    reads as absent to a signed-in user while it is off.
    """
    if not folder_attachments_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


router = APIRouter(prefix="/sessions", tags=["folder-attachments"])


class AttachFolderRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    connector_id: str = Field(..., alias="connectorId", min_length=1)
    folder_id: str = Field(..., alias="folderId", min_length=1, description="Provider folder (or root) id")
    folder_name: str = Field(..., alias="folderName", min_length=1, max_length=512)
    folder_path: Optional[str] = Field(None, alias="folderPath", max_length=2048, description="Breadcrumb path, display only")


class FolderAttachmentResponse(FolderAttachment):
    """The row, plus ``stale`` for an ``expanding`` row whose task is gone."""

    stale: bool = Field(False, description="expanding, but untouched past FOLDER_EXPANSION_TIMEOUT: call resume")
    ttl: Optional[int] = Field(None, exclude=True)

    @classmethod
    def from_row(cls, row: FolderAttachment) -> "FolderAttachmentResponse":
        return cls(**row.model_dump(), stale=row.is_stale())


@router.post("/{session_id}/folders", response_model=FolderAttachmentResponse, response_model_by_alias=True)
async def attach_folder(
    session_id: str,
    request: AttachFolderRequest,
    user: User = Depends(require_folder_attachments_user),
    service: FolderAttachmentService = Depends(get_folder_attachment_service),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> FolderAttachmentResponse:
    """Attach a file-source folder to a conversation and start expanding it.

    Returns the row at once with ``status: expanding``; poll ``GET`` for
    progress. The folder's files become session files as they are imported.
    """
    provider, adapter = await resolve_file_source(request.connector_id, user, provider_repo, role_service)
    access_token = await require_file_source_token(provider, user.user_id)
    folder = await service.attach(
        user,
        session_id,
        connector_id=provider.provider_id,
        connector_name=provider.display_name,
        provider_folder_id=request.folder_id,
        folder_name=request.folder_name,
        folder_path=request.folder_path,
        adapter=adapter,
        access_token=access_token,
    )
    return FolderAttachmentResponse.from_row(folder)


@router.get(
    "/{session_id}/folders/{folder_attach_id}",
    response_model=FolderAttachmentResponse,
    response_model_by_alias=True,
)
async def get_folder_attachment(
    session_id: str,
    folder_attach_id: str,
    user: User = Depends(require_folder_attachments_user),
    service: FolderAttachmentService = Depends(get_folder_attachment_service),
) -> FolderAttachmentResponse:
    """The folder row: status, counts and the files attached so far."""
    folder = await service.get(user.user_id, session_id, folder_attach_id)
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder attachment not found")
    return FolderAttachmentResponse.from_row(folder)


@router.delete("/{session_id}/folders/{folder_attach_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder_attachment(
    session_id: str,
    folder_attach_id: str,
    user: User = Depends(require_folder_attachments_user),
    service: FolderAttachmentService = Depends(get_folder_attachment_service),
) -> Response:
    """Remove the folder and every file it attached (S3, sidecars, index)."""
    if not await service.remove(user.user_id, session_id, folder_attach_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder attachment not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{session_id}/folders/{folder_attach_id}/resume",
    response_model=FolderAttachmentResponse,
    response_model_by_alias=True,
)
async def resume_folder_attachment(
    session_id: str,
    folder_attach_id: str,
    user: User = Depends(require_folder_attachments_user),
    service: FolderAttachmentService = Depends(get_folder_attachment_service),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> FolderAttachmentResponse:
    """Re-run the walk for a row whose task was lost (an app-api restart) or
    stopped short (consent, quota), skipping files already attached.

    409 while this process is still expanding the folder.
    """
    folder = await service.get(user.user_id, session_id, folder_attach_id)
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder attachment not found")
    if service.is_running(folder_attach_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This folder is still being expanded")
    provider, adapter = await resolve_file_source(folder.connector_id, user, provider_repo, role_service)
    access_token = await require_file_source_token(provider, user.user_id)
    row = await service.resume(folder, adapter, access_token)
    return FolderAttachmentResponse.from_row(row)


class FolderAttachmentListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    folders: list[FolderAttachmentResponse]


@router.get(
    "/{session_id}/folders",
    response_model=FolderAttachmentListResponse,
    response_model_by_alias=True,
)
async def list_folder_attachments(
    session_id: str,
    user: User = Depends(require_folder_attachments_user),
    service: FolderAttachmentService = Depends(get_folder_attachment_service),
) -> FolderAttachmentListResponse:
    """The caller's folders in this conversation, newest first. The SPA finds a
    reloaded message's folder here: the manifest it parses names the folder but
    carries no id."""
    rows = await service.list_for_session(user.user_id, session_id)
    return FolderAttachmentListResponse(folders=[FolderAttachmentResponse.from_row(row) for row in rows])


@router.post(
    "/{session_id}/folders/{folder_attach_id}/refresh",
    response_model=FolderAttachmentResponse,
    response_model_by_alias=True,
)
async def refresh_folder_attachment(
    session_id: str,
    folder_attach_id: str,
    user: User = Depends(require_folder_attachments_user),
    service: FolderAttachmentService = Depends(get_folder_attachment_service),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> FolderAttachmentResponse:
    """Re-list the folder in its source and bring its files up to date
    (spec §4.6): changed files are replaced in place under the same upload id,
    new ones imported, and ones gone from the folder marked
    ``removedFromSource``. Returns the row ``expanding`` with ``refreshing``
    set; poll ``GET`` until it settles.

    409 while this process is still expanding or refreshing the folder.
    """
    folder = await service.get(user.user_id, session_id, folder_attach_id)
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder attachment not found")
    if service.is_running(folder_attach_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This folder is still being expanded")
    provider, adapter = await resolve_file_source(folder.connector_id, user, provider_repo, role_service)
    access_token = await require_file_source_token(provider, user.user_id)
    row = await service.refresh(folder, adapter, access_token)
    return FolderAttachmentResponse.from_row(row)
