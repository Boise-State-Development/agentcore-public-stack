"""Proposals to a project's shared memory, at project level (Shared Projects 2.5a).

:class:`~apis.shared.memory.service.MemorySpaceService` owns the ``PROPOSAL#``
rows and the rules any space would have: who may propose, review and withdraw,
and how an approval becomes a save. This layer adds what only a project knows:

- **The space.** A proposal always targets the project's shared space
  (``sharedSpaceId`` on Project META); a member's own space needs no review.
- **Archived.** An archived project takes no new proposals and no decisions
  (409). A proposer may still withdraw one.
- **Who hears about it.** A new proposal notifies the owner and every editor
  but the proposer (``project_proposal_pending``); a decision notifies the
  proposer (``project_proposal_decided``). Both go through
  ``NotificationService``, which never raises, so a failed inbox write never
  fails the proposal.
- **The trail.** With an ``audit`` service (app-api) a proposal and each
  decision are ``project.memory_*`` records. The Runtime passes none, as it
  does for agent saves (2.4b): the proposal row itself records who and when.

The Runtime's ``memory_propose`` tool and app-api's routes both call this.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from apis.shared.audit.models import AuditAction
from apis.shared.auth.models import User

from .access import resolve_project_role
from .models import Project, normalize_email
from .repository import ProjectRepository

logger = logging.getLogger(__name__)


class ProposalProjectError(RuntimeError):
    """Refused at project level. ``status_code`` is the HTTP status a route returns."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


NOT_A_MEMBER = "You are not a member of this project."
ARCHIVED = "This project is archived, so its memory is read-only."
NO_SHARED_MEMORY = "This project has no shared memory yet. Open its Memory once to set it up."


class ProjectMemoryProposals:
    def __init__(
        self,
        repository: Optional[ProjectRepository] = None,
        memory: Any = None,
        notifications: Any = None,
        audit: Any = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self._memory = memory
        self._notifications = notifications
        self.audit = audit

    @property
    def memory(self):
        if self._memory is None:
            from apis.shared.memory.service import MemorySpaceService

            self._memory = MemorySpaceService()
        return self._memory

    @property
    def notifications(self):
        if self._notifications is None:
            from apis.shared.notifications.service import NotificationService

            self._notifications = NotificationService()
        return self._notifications

    def _space(self, project_id: str, user: User, *, writable: bool) -> Tuple[Project, str]:
        project, role = resolve_project_role(project_id, user.user_id, user.email, repository=self.repository)
        if project is None or role is None:
            raise ProposalProjectError(404, NOT_A_MEMBER)
        if writable and project.status != "active":
            raise ProposalProjectError(409, ARCHIVED)
        if not project.shared_space_id:
            raise ProposalProjectError(409, NO_SHARED_MEMORY)
        return project, project.shared_space_id

    # ── propose ────────────────────────────────────────────────────────

    def propose(
        self,
        project_id: str,
        user: User,
        slug: str,
        text: str,
        *,
        description: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        proposer_kind: str = "member",
        source_session_id: Optional[str] = None,
    ):
        """Queue a change for review. Returns ``(proposal, warnings)``."""
        project, space_id = self._space(project_id, user, writable=True)
        proposal, warnings = self.memory.create_proposal(
            space_id, user.user_id, user.email, slug, text,
            description=description, aliases=aliases, proposer_kind=proposer_kind,
            source_session_id=source_session_id,
        )
        self.notifications.notify_many(
            self._reviewers(project),
            kind="project_proposal_pending",
            actor=user,
            project_id=project.project_id,
            project_name=project.name,
            payload={"proposalId": proposal.proposal_id, "slug": proposal.slug},
        )
        self._record(AuditAction.PROJECT_MEMORY_PROPOSED, user, project, proposal)
        return proposal, warnings

    def _reviewers(self, project: Project) -> List[str]:
        """The owner and every editor. Whoever proposed is skipped by ``notify_many``."""
        try:
            editors = [m.email for m in self.repository.list_members(project.project_id) if m.role == "editor"]
        except Exception:
            logger.warning("Could not list editors of %s to announce a proposal", project.project_id, exc_info=True)
            editors = []
        return [normalize_email(project.owner_email), *editors]

    # ── read ───────────────────────────────────────────────────────────

    def list(self, project_id: str, user: User, *, state: Optional[str] = None):
        project, space_id = self._space(project_id, user, writable=False)
        return self.memory.list_proposals(space_id, user.user_id, user.email, state=state)

    def list_with_staleness(self, project_id: str, user: User, *, state: Optional[str] = None):
        """``[(proposal, stale)]``, newest first. ``stale`` is None for a decided proposal.

        One manifest read covers every row, so a review queue costs the same as
        a list.
        """
        _, space_id = self._space(project_id, user, writable=False)
        proposals = self.memory.list_proposals(space_id, user.user_id, user.email, state=state)
        if not any(p.state == "pending" for p in proposals):
            return [(p, None) for p in proposals]
        hashes = {e.slug: e.content_hash for e in self.memory.repository.get_index(space_id).entries}
        rows = []
        for p in proposals:
            if p.state != "pending":
                rows.append((p, None))
                continue
            stale = hashes.get(p.slug, "") != p.base_content_hash
            if stale and p.kind == "compaction":
                rows.append(self._compaction_view(space_id, p))
            else:
                rows.append((p, stale))
        return rows

    def _compaction_view(self, space_id: str, proposal: Any):
        """A maintenance proposal on a file edited since: its changes applied to the file as it is now."""
        text, stale = self.memory.compaction_view(space_id, proposal)
        return proposal.model_copy(update={"text": text}), stale

    def current_text(self, project_id: str, user: User, slug: str) -> Optional[str]:
        """The file a proposal would change, as its items (no frontmatter), or None for a new file."""
        from apis.shared.memory.format import split_frontmatter
        from apis.shared.memory.service import MemoryEntryNotFoundError

        _, space_id = self._space(project_id, user, writable=False)
        try:
            text = self.memory.read_entry(space_id, user.user_id, user.email, slug)
        except MemoryEntryNotFoundError:
            return None
        return split_frontmatter(text, strict=False)[1]

    def get(self, project_id: str, user: User, proposal_id: str):
        """``(proposal, stale)``: stale means the file changed since it was proposed."""
        _, space_id = self._space(project_id, user, writable=False)
        proposal = self.memory.get_proposal(space_id, user.user_id, user.email, proposal_id)
        stale = proposal.state == "pending" and self.memory.proposal_is_stale(space_id, proposal)
        if stale and proposal.kind == "compaction":
            return self._compaction_view(space_id, proposal)
        return proposal, stale

    # ── decide ─────────────────────────────────────────────────────────

    def approve(
        self,
        project_id: str,
        user: User,
        proposal_id: str,
        *,
        text: Optional[str] = None,
        description: Optional[str] = None,
        note: Optional[str] = None,
        ops: Optional[List[int]] = None,
    ):
        """Apply it. Returns ``(proposal, save_result)``. ``ops`` picks a maintenance proposal's changes."""
        project, space_id = self._space(project_id, user, writable=True)
        proposal, result = self.memory.approve_proposal(
            space_id, user.user_id, user.email, proposal_id, text=text, description=description, note=note,
            ops=ops,
        )
        self._index_new_file(space_id, user, result)
        self._tell_proposer(project, user, proposal)
        self._record(AuditAction.PROJECT_MEMORY_PROPOSAL_APPROVED, user, project, proposal)
        return proposal, result

    def _index_new_file(self, space_id: str, user: User, result: Any) -> None:
        """A new file joins MEMORY.md as a direct save's would (the harness's ``memory_save`` does this too)."""
        if result.ref.version != 1:
            return
        from apis.shared.memory.hydration import PROJECT_MEMORY_MAX_TOKENS

        try:
            self.memory.add_index_link(
                space_id, user.user_id, user.email, result.ref, max_tokens=PROJECT_MEMORY_MAX_TOKENS
            )
        except Exception:
            logger.warning("Could not index approved file %s in %s", result.ref.slug, space_id, exc_info=True)

    def reject(self, project_id: str, user: User, proposal_id: str, *, note: Optional[str] = None):
        project, space_id = self._space(project_id, user, writable=True)
        proposal = self.memory.reject_proposal(space_id, user.user_id, user.email, proposal_id, note=note)
        self._tell_proposer(project, user, proposal)
        self._record(AuditAction.PROJECT_MEMORY_PROPOSAL_REJECTED, user, project, proposal)
        return proposal

    def withdraw(self, project_id: str, user: User, proposal_id: str):
        _, space_id = self._space(project_id, user, writable=False)
        return self.memory.withdraw_proposal(space_id, user.user_id, user.email, proposal_id)

    def _tell_proposer(self, project: Project, reviewer: User, proposal: Any) -> None:
        payload = {"proposalId": proposal.proposal_id, "slug": proposal.slug, "decision": proposal.state}
        if proposal.note:
            payload["note"] = proposal.note
        if proposal.edited:
            payload["edited"] = True
        self.notifications.notify(
            recipient_email=proposal.proposer_email,
            kind="project_proposal_decided",
            actor=reviewer,
            project_id=project.project_id,
            project_name=project.name,
            payload=payload,
        )

    def _record(self, action: str, user: User, project: Project, proposal: Any) -> None:
        if self.audit is None:
            return
        from apis.shared.audit import TARGET_PROJECT

        after = {"proposalId": proposal.proposal_id, "slug": proposal.slug}
        if proposal.kind == "compaction":
            after["kind"] = "compaction"
            if proposal.applied_ops is not None:
                after["appliedOps"] = len(proposal.applied_ops)
                after["totalOps"] = len(proposal.ops or [])
        if proposal.result_version is not None:
            after["version"] = proposal.result_version
        if proposal.edited:
            after["edited"] = True
        self.audit.record(
            action=action, actor=user, target_type=TARGET_PROJECT, target_id=project.project_id,
            after=after, reason=proposal.note,
        )
