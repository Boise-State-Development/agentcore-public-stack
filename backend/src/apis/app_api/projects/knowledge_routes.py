"""``/projects/{id}/knowledge`` — the project's Files (shared-projects §5, PR-1.5b).

A project's files are its harness Agent's documents, so this is a thin layer
over the document and web-crawl routes, authorized by the project instead:

  - **Writes** (upload, import, crawl, delete) need an editor on an active
    project, then call the agent's own route handler for the harness. That
    handler re-checks access through the harness's project delegation and keeps
    all of its logic (knowledge-base provisioning, the byte cap, cleanup).
  - **Reads** (list, status, download) need only a viewer. The agent routes are
    editor-only, which is right for an ordinary agent and wrong for a project
    whose viewers should see its files, so reads call the document service
    directly, with the harness's owner id the service keys on.

Every document says who added it (``addedByEmail``), resolved from the member
list, so a former member's files show no name rather than a stale one.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.app_api.documents import routes as document_routes
from apis.app_api.documents.models import (
    CreateDocumentRequest,
    DocumentResponse,
    DownloadUrlResponse,
    ExtractedChunksResponse,
    ImportDocumentsRequest,
    ReportUploadFailureRequest,
)
from apis.app_api.documents.services.document_service import get_document, list_assistant_documents
from apis.app_api.documents.services.storage_service import generate_download_url
from apis.app_api.web_sources import routes as crawl_routes
from apis.app_api.web_sources.models import ActiveCrawlsResponse, CrawlJob, StartCrawlRequest
from apis.shared.assistants.models import Assistant
from apis.shared.auth.models import User
from apis.shared.oauth.provider_repository import OAuthProviderRepository, get_provider_repository
from apis.shared.projects.models import Project, ProjectRole
from apis.shared.projects.service import ProjectError
from apis.shared.rbac.service import AppRoleService, get_app_role_service

from .harness_settings import load_harness
from .models import (
    ProjectDocumentResponse,
    ProjectDocumentsResponse,
    ProjectImportResponse,
    ProjectStartCrawlResponse,
    ProjectUploadUrlResponse,
)
from .routes import _svc, _translate, require_projects_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/knowledge", tags=["projects"])


def sharing_notice(project: Project) -> str:
    people = project.member_count + 1
    return (
        f"Everyone in {project.name} ({people} {'person' if people == 1 else 'people'}) can open this file, "
        "and the project's agent can use it to answer anyone in the project."
    )


async def _viewer(project_id: str, user: User) -> Tuple[Project, ProjectRole, Assistant, Dict[str, str]]:
    """Authorize a read; return the harness and a user id → email map of the people in the project."""
    try:
        project, role, members = _svc().list_members(project_id, user)
        harness = await load_harness(project)
    except ProjectError as e:
        raise _translate(e)
    emails = {m.user_id: m.email for m in members if m.user_id}
    emails[project.owner_id] = project.owner_email
    return project, role, harness, emails


async def _editor(project_id: str, user: User, *, writable: bool = True) -> Tuple[Project, Assistant]:
    """Authorize an editor; a write also needs the project to be active."""
    try:
        project, _ = _svc().authorize(project_id, user, "editor", writable=writable)
        harness = await load_harness(project)
    except ProjectError as e:
        raise _translate(e)
    return project, harness


def _document(doc, emails: Dict[str, str]) -> ProjectDocumentResponse:
    response = DocumentResponse.model_validate(doc.model_dump(by_alias=True))
    added_by = doc.added_by_user_id or doc.imported_by_user_id
    return ProjectDocumentResponse(**response.model_dump(), added_by_email=emails.get(added_by) if added_by else None)


async def _get_document(harness: Assistant, document_id: str):
    doc = await get_document(harness.assistant_id, document_id, harness.owner_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return doc


# ---- web crawl (editor) -------------------------------------------------
# Declared before ``/{document_id}`` so "crawls" is never read as a document id.


@router.post(
    "/crawl",
    response_model=ProjectStartCrawlResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_crawl(
    project_id: str, body: StartCrawlRequest, user: User = Depends(require_projects_user)
) -> ProjectStartCrawlResponse:
    project, harness = await _editor(project_id, user)
    result = await crawl_routes.start_crawl(harness.assistant_id, body, user)
    return ProjectStartCrawlResponse(**result.model_dump(), notice=sharing_notice(project))


@router.get("/crawls", response_model=ActiveCrawlsResponse, response_model_by_alias=True)
async def list_crawls(
    project_id: str,
    active: bool = Query(False),
    user: User = Depends(require_projects_user),
) -> ActiveCrawlsResponse:
    _, harness = await _editor(project_id, user, writable=False)
    return await crawl_routes.list_crawls(harness.assistant_id, active, user)


@router.get("/crawls/{crawl_id}", response_model=CrawlJob, response_model_by_alias=True)
async def get_crawl(project_id: str, crawl_id: str, user: User = Depends(require_projects_user)) -> CrawlJob:
    _, harness = await _editor(project_id, user, writable=False)
    return await crawl_routes.get_crawl(harness.assistant_id, crawl_id, user)


@router.delete("/crawls/{crawl_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_crawl(project_id: str, crawl_id: str, user: User = Depends(require_projects_user)) -> None:
    _, harness = await _editor(project_id, user)
    await crawl_routes.delete_crawl(harness.assistant_id, crawl_id, user)


# ---- reads (viewer) -----------------------------------------------------


@router.get("", response_model=ProjectDocumentsResponse, response_model_by_alias=True)
async def list_files(
    project_id: str,
    limit: Optional[int] = Query(None, ge=1, le=1000),
    next_token: Optional[str] = Query(None, alias="nextToken"),
    user: User = Depends(require_projects_user),
) -> ProjectDocumentsResponse:
    """The project's files, with who added each and the knowledge base's storage use."""
    project, role, harness, emails = await _viewer(project_id, user)
    docs, token = await list_assistant_documents(
        assistant_id=harness.assistant_id, owner_id=harness.owner_id, limit=limit, next_token=next_token
    )
    return ProjectDocumentsResponse(
        documents=[_document(d, emails) for d in docs],
        next_token=token,
        kb_usage=await document_routes._resolve_kb_usage(harness.assistant_id),
        can_edit=role in ("owner", "editor") and project.status == "active",
    )


@router.get("/{document_id}", response_model=ProjectDocumentResponse, response_model_by_alias=True)
async def get_file(
    project_id: str, document_id: str, user: User = Depends(require_projects_user)
) -> ProjectDocumentResponse:
    """One file's details and processing status."""
    _, _, harness, emails = await _viewer(project_id, user)
    return _document(await _get_document(harness, document_id), emails)


@router.get("/{document_id}/download", response_model=DownloadUrlResponse, response_model_by_alias=True)
async def download_file(
    project_id: str, document_id: str, user: User = Depends(require_projects_user)
) -> DownloadUrlResponse:
    """A short-lived download URL, for any member, unless the project's agent disables source downloads."""
    _, _, harness, _ = await _viewer(project_id, user)
    if not (getattr(harness, "show_citations", True) and getattr(harness, "allow_document_download", True)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Source-document download is disabled for this project.")
    doc = await _get_document(harness, document_id)
    expires_in = 3600
    url = await generate_download_url(s3_key=doc.s3_key, expires_in=expires_in)
    return DownloadUrlResponse(downloadUrl=url, filename=doc.filename, expiresIn=expires_in)


# ---- writes (editor, active project) -----------------------------------


@router.post("/upload-url", response_model=ProjectUploadUrlResponse, response_model_by_alias=True)
async def upload_file(
    project_id: str, body: CreateDocumentRequest, user: User = Depends(require_projects_user)
) -> ProjectUploadUrlResponse:
    """Start an upload: a presigned URL, plus the notice that every member can read the file."""
    project, harness = await _editor(project_id, user)
    result = await document_routes.generate_upload_url_endpoint(harness.assistant_id, body, user)
    return ProjectUploadUrlResponse(**result.model_dump(), notice=sharing_notice(project))


@router.post(
    "/import",
    response_model=ProjectImportResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
async def import_files(
    project_id: str,
    body: ImportDocumentsRequest,
    user: User = Depends(require_projects_user),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> ProjectImportResponse:
    """Import files from the caller's connected file source (their credentials, the project's files)."""
    project, harness = await _editor(project_id, user)
    result = await document_routes.import_documents(harness.assistant_id, body, user, provider_repo, role_service)
    return ProjectImportResponse(**result.model_dump(), notice=sharing_notice(project))


@router.post("/{document_id}/upload-failed", response_model=DocumentResponse, response_model_by_alias=True)
async def report_upload_failure(
    project_id: str,
    document_id: str,
    body: ReportUploadFailureRequest,
    user: User = Depends(require_projects_user),
) -> DocumentResponse:
    _, harness = await _editor(project_id, user)
    return await document_routes.report_upload_failure(harness.assistant_id, document_id, body, user)


@router.get("/{document_id}/chunks", response_model=ExtractedChunksResponse, response_model_by_alias=True)
async def get_file_chunks(
    project_id: str, document_id: str, user: User = Depends(require_projects_user)
) -> ExtractedChunksResponse:
    """What the knowledge base extracted from a file: an editor's tool for fixing a source."""
    _, harness = await _editor(project_id, user, writable=False)
    return await document_routes.get_document_chunks(harness.assistant_id, document_id, user)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(project_id: str, document_id: str, user: User = Depends(require_projects_user)) -> None:
    _, harness = await _editor(project_id, user)
    await document_routes.delete_document(harness.assistant_id, document_id, user)


__all__: List[str] = ["router", "sharing_notice"]
