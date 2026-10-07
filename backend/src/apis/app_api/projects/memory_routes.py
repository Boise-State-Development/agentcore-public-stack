"""Proposals to a project's shared memory (Shared Projects 2.5a).

Any member may propose a change; the owner and editors review it. A viewer
lists and reads only their own proposals. The review UI is the Memory tab
(2.8); until then these routes are the review surface.

All behind ``require_projects_user`` (404 while ``PROJECTS_ENABLED`` is off).
"""

from __future__ import annotations

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth.models import User
from apis.shared.directory import display_names
from apis.shared.memory.models import ArchivedItem, ItemProvenance, MemoryProposal
from apis.shared.memory.service import (
    MAX_PROPOSAL_NOTE_CHARS,
    MemoryProposalStateError,
    MemorySpaceConcurrencyError,
    MemorySpaceError,
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemoryValidationError,
)
from apis.shared.projects.memory_files import EditedItem, ProjectMemoryFiles
from apis.shared.projects.memory_proposals import ProjectMemoryProposals, ProposalProjectError

from .routes import _svc, require_projects_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/memory/proposals", tags=["projects"])


# ── models ──────────────────────────────────────────────────────────────


class CreateProposalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slug: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., description="The whole proposed file, in the same form a save takes")
    description: Optional[str] = None
    aliases: Optional[List[str]] = None


class ApproveProposalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    text: Optional[str] = Field(None, description="The reviewer's edited version; omitted applies it as proposed")
    description: Optional[str] = None
    note: Optional[str] = Field(None, max_length=MAX_PROPOSAL_NOTE_CHARS)


class RejectProposalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    note: Optional[str] = Field(None, max_length=MAX_PROPOSAL_NOTE_CHARS)


class ProposalResponse(BaseModel):
    """A proposal as a member sees it. People appear by email and name, never by user id."""

    model_config = ConfigDict(populate_by_name=True)

    proposal_id: str = Field(..., alias="proposalId")
    state: str
    slug: str
    text: str
    description: Optional[str] = None
    aliases: Optional[List[str]] = None
    base_version: int = Field(..., alias="baseVersion")
    tokens: Optional[int] = None
    proposed_by_email: str = Field(..., alias="proposedByEmail")
    proposed_by_name: Optional[str] = Field(None, alias="proposedByName")
    proposer_kind: str = Field(..., alias="proposerKind")
    created_at: str = Field(..., alias="createdAt")
    decided_by_email: Optional[str] = Field(None, alias="decidedByEmail")
    decided_by_name: Optional[str] = Field(None, alias="decidedByName")
    decided_at: Optional[str] = Field(None, alias="decidedAt")
    note: Optional[str] = None
    result_version: Optional[int] = Field(None, alias="resultVersion")
    edited: bool = False
    is_mine: bool = Field(..., alias="isMine")
    stale: Optional[bool] = Field(
        None, description="Pending and the file changed since: approve an edited version or reject it"
    )

    @classmethod
    def build(cls, p: MemoryProposal, caller: User, names: dict, stale: Optional[bool] = None) -> "ProposalResponse":
        return cls(
            proposal_id=p.proposal_id, state=p.state, slug=p.slug, text=p.text, description=p.description,
            aliases=p.aliases, base_version=p.base_version, tokens=p.tokens,
            proposed_by_email=p.proposer_email, proposed_by_name=names.get(p.proposer_email),
            proposer_kind=p.proposer_kind, created_at=p.created_at,
            decided_by_email=p.decided_by_email, decided_by_name=names.get(p.decided_by_email or ""),
            decided_at=p.decided_at, note=p.note, result_version=p.result_version, edited=p.edited,
            is_mine=p.proposer_id == caller.user_id, stale=stale,
        )


class ProposalDetailResponse(ProposalResponse):
    current_text: Optional[str] = Field(
        None, alias="currentText",
        description="Pending only: the file as it is now, items without frontmatter; null for a new file",
    )


class ProposalsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    proposals: List[ProposalResponse] = Field(..., description="Newest first")


class CreateProposalResponse(ProposalResponse):
    warnings: List[str] = Field(default_factory=list)


class ApproveProposalResponse(ProposalResponse):
    warnings: List[str] = Field(default_factory=list)


# ── plumbing ────────────────────────────────────────────────────────────


def _proposals() -> ProjectMemoryProposals:
    svc = _svc()
    return ProjectMemoryProposals(repository=svc.repository, audit=svc.audit)


def _translate(e: Exception) -> HTTPException:
    if isinstance(e, ProposalProjectError):
        return HTTPException(status_code=e.status_code, detail=str(e))
    if isinstance(e, MemorySpacePermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    if isinstance(e, MemorySpaceNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if isinstance(e, (MemorySpaceConcurrencyError, MemoryProposalStateError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    if isinstance(e, MemoryValidationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


_ERRORS = (ProposalProjectError, MemorySpaceError)


def _names(*proposals: MemoryProposal) -> dict:
    emails = {p.proposer_email for p in proposals} | {p.decided_by_email for p in proposals if p.decided_by_email}
    return display_names({e for e in emails if e})


# ── routes ──────────────────────────────────────────────────────────────


@router.get("", response_model=ProposalsResponse, response_model_by_alias=True)
def list_proposals(
    project_id: str,
    state: Optional[Literal["pending", "approved", "rejected", "withdrawn"]] = Query(None),
    user: User = Depends(require_projects_user),
) -> ProposalsResponse:
    """Editors and the owner see every proposal; anyone else sees their own."""
    try:
        rows = _proposals().list_with_staleness(project_id, user, state=state)
    except _ERRORS as e:
        raise _translate(e)
    names = _names(*(p for p, _ in rows))
    return ProposalsResponse(proposals=[ProposalResponse.build(p, user, names, stale=stale) for p, stale in rows])


@router.post("", response_model=CreateProposalResponse, response_model_by_alias=True, status_code=201)
def create_proposal(
    project_id: str, body: CreateProposalRequest, user: User = Depends(require_projects_user)
) -> CreateProposalResponse:
    """Propose a change for an editor to review. Checked like a save, so a bad file is a 400 now."""
    try:
        proposal, warnings = _proposals().propose(
            project_id, user, body.slug, body.text, description=body.description, aliases=body.aliases,
        )
    except _ERRORS as e:
        raise _translate(e)
    base = ProposalResponse.build(proposal, user, _names(proposal), stale=False)
    return CreateProposalResponse(**base.model_dump(), warnings=warnings)


@router.get("/{proposal_id}", response_model=ProposalDetailResponse, response_model_by_alias=True)
def get_proposal(project_id: str, proposal_id: str, user: User = Depends(require_projects_user)) -> ProposalDetailResponse:
    """One proposal; while pending, with the file's current text for a side-by-side review."""
    proposals = _proposals()
    try:
        proposal, stale = proposals.get(project_id, user, proposal_id)
        current = proposals.current_text(project_id, user, proposal.slug) if proposal.state == "pending" else None
    except _ERRORS as e:
        raise _translate(e)
    base = ProposalResponse.build(
        proposal, user, _names(proposal), stale=stale if proposal.state == "pending" else None
    )
    return ProposalDetailResponse(**base.model_dump(), current_text=current)


@router.post("/{proposal_id}/approve", response_model=ApproveProposalResponse, response_model_by_alias=True)
def approve_proposal(
    project_id: str, proposal_id: str, body: ApproveProposalRequest, user: User = Depends(require_projects_user)
) -> ApproveProposalResponse:
    """Editor+. Without ``text``, a proposal whose file has changed since is a 409."""
    try:
        proposal, result = _proposals().approve(
            project_id, user, proposal_id, text=body.text, description=body.description, note=body.note,
        )
    except _ERRORS as e:
        raise _translate(e)
    base = ProposalResponse.build(proposal, user, _names(proposal))
    return ApproveProposalResponse(**base.model_dump(), warnings=result.warnings)


@router.post("/{proposal_id}/reject", response_model=ProposalResponse, response_model_by_alias=True)
def reject_proposal(
    project_id: str, proposal_id: str, body: RejectProposalRequest, user: User = Depends(require_projects_user)
) -> ProposalResponse:
    try:
        proposal = _proposals().reject(project_id, user, proposal_id, note=body.note)
    except _ERRORS as e:
        raise _translate(e)
    return ProposalResponse.build(proposal, user, _names(proposal))


@router.post("/{proposal_id}/withdraw", response_model=ProposalResponse, response_model_by_alias=True)
def withdraw_proposal(project_id: str, proposal_id: str, user: User = Depends(require_projects_user)) -> ProposalResponse:
    """The proposer takes back their own pending proposal."""
    try:
        proposal = _proposals().withdraw(project_id, user, proposal_id)
    except _ERRORS as e:
        raise _translate(e)
    return ProposalResponse.build(proposal, user, _names(proposal))


# ── files, pins and the archive (2.5a-2) ────────────────────────────────

files_router = APIRouter(prefix="/projects/{project_id}/memory", tags=["projects"])

Scope = Literal["project", "mine"]


class MemoryFileItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    anchor: str
    text: str
    pinned: bool = False
    provenance: Optional[ItemProvenance] = None


class MemoryFileResponse(BaseModel):
    """A memory file as items, each with its pin and where it came from."""

    model_config = ConfigDict(populate_by_name=True)

    slug: str
    description: str = ""
    version: int = 0
    tokens: Optional[int] = None
    items: List[MemoryFileItem]
    people: dict = Field(default_factory=dict, description="Display names for the emails in provenance")


class PinRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope: Scope = "project"
    slug: str = Field(..., min_length=1, max_length=128)
    anchor: str = Field(..., min_length=1, max_length=32)


class PinsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slug: str
    pinned: List[str]


class ArchiveResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    items: List[ArchivedItem] = Field(..., description="Newest first; only those still restorable")
    people: dict = Field(default_factory=dict, description="Display names for the emails in the rows")


class RestoreResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slug: str
    version: int


class EditedItemRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    anchor: Optional[str] = Field(None, max_length=32, description="The anchor it was read with; omit for a new item")
    text: str


class SaveFileRequest(BaseModel):
    """A file as the Memory tab's editor sends it (§4.4's structured body)."""

    model_config = ConfigDict(populate_by_name=True)

    items: List[EditedItemRequest] = Field(..., max_length=500)
    description: Optional[str] = Field(None, description="Omit to keep the current one")
    aliases: Optional[List[str]] = Field(None, description="Omit to keep the current ones")
    base_version: Optional[int] = Field(
        None, alias="baseVersion", ge=0, description="The version opened; 0 for a new file. A mismatch is a 409"
    )


class SaveFileResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slug: str
    version: int
    tokens: Optional[int] = None
    item_count: Optional[int] = Field(None, alias="itemCount")
    warnings: List[str] = Field(default_factory=list)
    over_soft_threshold: bool = Field(False, alias="overSoftThreshold")
    removed_anchors: List[str] = Field(default_factory=list, alias="removedAnchors")
    indexed: Optional[str] = Field(
        None, description="A new file's index line: added, already_linked or over_budget; null for an existing file"
    )


class IndexRequest(BaseModel):
    content: str = Field(..., max_length=64_000)


class IndexResponse(BaseModel):
    content: str


def _files() -> ProjectMemoryFiles:
    svc = _svc()
    return ProjectMemoryFiles(repository=svc.repository, audit=svc.audit)


def _emails(provenance: dict) -> set:
    found = set()
    for p in provenance.values():
        for value in (p.added_by, p.updated_by, p.proposed_by, p.approved_by, p.restored_by):
            if value:
                found.add(value)
    return found


@files_router.get("/files/{slug:path}", response_model=MemoryFileResponse, response_model_by_alias=True)
def read_memory_file(
    project_id: str, slug: str, scope: Scope = Query("project"), user: User = Depends(require_projects_user)
) -> MemoryFileResponse:
    """One file's items, pins and provenance (viewer+)."""
    try:
        ref, items, provenance = _files().read(project_id, user, scope, slug)
    except _ERRORS as e:
        raise _translate(e)
    pinned = set(ref.pinned)
    return MemoryFileResponse(
        slug=ref.slug,
        description=ref.description,
        version=ref.version,
        tokens=ref.tokens,
        items=[
            MemoryFileItem(anchor=i.anchor, text=i.text, pinned=i.anchor in pinned, provenance=provenance.get(i.anchor))
            for i in items
        ],
        people=display_names(_emails(provenance)),
    )


@files_router.post("/pins", response_model=PinsResponse, response_model_by_alias=True)
def pin_item(project_id: str, body: PinRequest, user: User = Depends(require_projects_user)) -> PinsResponse:
    """Pin an item, so no save can drop it until it's unpinned (editor+)."""
    try:
        pinned = _files().set_pinned(project_id, user, body.scope, body.slug, body.anchor, pinned=True)
    except _ERRORS as e:
        raise _translate(e)
    return PinsResponse(slug=body.slug, pinned=pinned)


@files_router.delete("/pins", response_model=PinsResponse, response_model_by_alias=True)
def unpin_item(
    project_id: str,
    slug: str = Query(..., min_length=1, max_length=128),
    anchor: str = Query(..., min_length=1, max_length=32),
    scope: Scope = Query("project"),
    user: User = Depends(require_projects_user),
) -> PinsResponse:
    """Unpin an item (editor+). Query parameters, because a slug may contain "/"."""
    try:
        pinned = _files().set_pinned(project_id, user, scope, slug, anchor, pinned=False)
    except _ERRORS as e:
        raise _translate(e)
    return PinsResponse(slug=slug, pinned=pinned)


@files_router.get("/archive", response_model=ArchiveResponse, response_model_by_alias=True)
def list_archive(
    project_id: str, scope: Scope = Query("project"), user: User = Depends(require_projects_user)
) -> ArchiveResponse:
    """Items that left a file and can still be restored (viewer+)."""
    try:
        items = _files().archive(project_id, user, scope)
    except _ERRORS as e:
        raise _translate(e)
    emails = {i.archived_by for i in items if i.archived_by}
    for i in items:
        if i.provenance:
            emails |= _emails({i.anchor: i.provenance})
    return ArchiveResponse(items=items, people=display_names(emails))


@files_router.post("/archive/{archive_id}/restore", response_model=RestoreResponse, response_model_by_alias=True)
def restore_archived_item(
    project_id: str, archive_id: str, scope: Scope = Query("project"), user: User = Depends(require_projects_user)
) -> RestoreResponse:
    """Put an archived item back at the end of its file, with its anchor and provenance (editor+)."""
    try:
        result = _files().restore(project_id, user, scope, archive_id)
    except _ERRORS as e:
        raise _translate(e)
    return RestoreResponse(slug=result.ref.slug, version=result.ref.version)


@files_router.put("/files/{slug:path}", response_model=SaveFileResponse, response_model_by_alias=True)
def save_memory_file(
    project_id: str,
    slug: str,
    body: SaveFileRequest,
    scope: Scope = Query("project"),
    user: User = Depends(require_projects_user),
) -> SaveFileResponse:
    """Create or replace a file from its items (editor+ for project memory; any member in their own).

    Each item keeps the anchor it was read with, so its history follows it; an
    item left out goes to the archive. A 400's detail says what to fix.
    """
    try:
        result, indexed = _files().save(
            project_id, user, scope, slug,
            [EditedItem(text=i.text, anchor=i.anchor) for i in body.items],
            description=body.description, aliases=body.aliases, base_version=body.base_version,
        )
    except _ERRORS as e:
        raise _translate(e)
    return SaveFileResponse(
        slug=result.ref.slug,
        version=result.ref.version,
        tokens=result.ref.tokens,
        item_count=result.ref.item_count,
        warnings=result.warnings,
        over_soft_threshold=result.over_soft_threshold,
        removed_anchors=result.removed_anchors,
        indexed=indexed,
    )


@files_router.delete("/files/{slug:path}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory_file(
    project_id: str, slug: str, scope: Scope = Query("project"), user: User = Depends(require_projects_user)
) -> None:
    """Delete a file; its items go to the archive and its index line goes too (editor+ / your own)."""
    try:
        _files().delete(project_id, user, scope, slug)
    except _ERRORS as e:
        raise _translate(e)


@files_router.put("/index", response_model=IndexResponse)
def save_memory_index(
    project_id: str, body: IndexRequest, scope: Scope = Query("project"), user: User = Depends(require_projects_user)
) -> IndexResponse:
    """Replace a scope's MEMORY.md, its links checked (editor+ / your own)."""
    try:
        _files().save_index(project_id, user, scope, body.content)
    except _ERRORS as e:
        raise _translate(e)
    return IndexResponse(content=body.content)
