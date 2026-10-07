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
from apis.shared.memory.models import MemoryProposal
from apis.shared.memory.service import (
    MAX_PROPOSAL_NOTE_CHARS,
    MemoryProposalStateError,
    MemorySpaceConcurrencyError,
    MemorySpaceError,
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemoryValidationError,
)
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
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
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
