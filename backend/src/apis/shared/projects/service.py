"""Shared Projects service: permissions and orchestration (shared-projects §3.1, §5).

Every operation resolves the caller's project role first, through the same
:func:`~apis.shared.projects.access.resolve_project_role` the harness Agent uses,
so a project and its harness can never disagree about who may do what.

Roles:

  - **owner** (on META) — everything, including settings, archive/purge and transfer.
  - **editor** — edit the project; manage members (never the owner) while
    ``settings.editorsManageMembers`` is on, which is the default.
  - **viewer** — read.

A caller with no role gets "not found", never "forbidden": whether a project id
exists is not something a non-member is told.

An archived project is read-only. The only write it accepts is the owner
restoring it, and only an archived project can be purged — archive first, so a
delete is always two deliberate steps.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from apis.shared.audit import TARGET_PROJECT, AuditAction, AuditRecord, AuditService, get_audit_service
from apis.shared.auth.models import User
from apis.shared.notifications import NotificationKind, NotificationService
from apis.shared.timestamps import utc_now_iso

from .access import resolve_project_role
from .harness import AssistantsHarnessGateway, HarnessGateway
from .models import (
    ROLE_RANK,
    MemberRole,
    Project,
    ProjectMember,
    ProjectRole,
    ProjectSettings,
    ProjectStatus,
    SharedTask,
    normalize_email,
)
from .repository import ProjectRepository, ProjectWriteConflict

logger = logging.getLogger(__name__)

NAME_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 2000
MAX_EMAILS_PER_REQUEST = 200
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProjectError(RuntimeError):
    """Base error; routes map it to 400."""


class ProjectNotFoundError(ProjectError):
    """404 — also what a non-member is told about a project that exists."""


class ProjectPermissionError(ProjectError):
    """403 — a member whose role is too low."""


class ProjectConflictError(ProjectError):
    """409 — archived, full, raced, or a transition the state does not allow."""


def max_members() -> int:
    """Membership cap per project (``PROJECTS_MAX_MEMBERS``, default 200)."""
    try:
        return max(1, int(os.environ.get("PROJECTS_MAX_MEMBERS", "200")))
    except ValueError:
        return 200


def editors_manage_members_default() -> bool:
    """Default for a new project's ``editorsManageMembers`` (default on)."""
    return os.environ.get("PROJECTS_EDITORS_MANAGE_MEMBERS_DEFAULT", "").strip().lower() != "false"


def is_valid_email(email: str) -> bool:
    """The shape check every invite path applies (not deliverability)."""
    return bool(_EMAIL_RE.match(email))


def _new_project_id() -> str:
    return f"prj_{uuid.uuid4().hex}"


def _clean_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ProjectError("A project needs a name")
    if len(cleaned) > NAME_MAX_LENGTH:
        raise ProjectError(f"Project names are limited to {NAME_MAX_LENGTH} characters")
    return cleaned


def _clean_description(description: Optional[str]) -> str:
    cleaned = (description or "").strip()
    if len(cleaned) > DESCRIPTION_MAX_LENGTH:
        raise ProjectError(f"Project descriptions are limited to {DESCRIPTION_MAX_LENGTH} characters")
    return cleaned


@dataclass
class AddMembersResult:
    """Outcome of a bulk invite, one bucket per reason, in request order."""

    added: List[ProjectMember] = field(default_factory=list)
    already_members: List[str] = field(default_factory=list)
    invalid: List[str] = field(default_factory=list)
    over_capacity: List[str] = field(default_factory=list)


class ProjectService:
    def __init__(
        self,
        repository: Optional[ProjectRepository] = None,
        harness: Optional[HarnessGateway] = None,
        audit: Optional[AuditService] = None,
        notifications: Optional[NotificationService] = None,
    ):
        self.repository = repository or ProjectRepository()
        self.harness = harness or AssistantsHarnessGateway()
        self.audit = audit or get_audit_service()
        self.notifications = notifications or NotificationService(table_name=self.repository.table_name)

    # ── trail ───────────────────────────────────────────────────────────

    def record(self, action: str, actor: User, project_id: str, **details) -> None:
        """One ``project.*`` audit record. Never raises (``AuditService.record``)."""
        self.audit.record(action=action, actor=actor, target_type=TARGET_PROJECT, target_id=project_id, **details)

    def _notify(self, kind: NotificationKind, recipient: str, actor: User, project: Project, **payload) -> None:
        self.notifications.notify(
            recipient_email=recipient,
            kind=kind,
            actor=actor,
            project_id=project.project_id,
            project_name=project.name,
            payload=payload,
        )

    def list_audit(
        self, project_id: str, user: User, *, limit: int, after: Optional[str] = None
    ) -> Tuple[List[AuditRecord], Optional[str]]:
        """The project's audit trail, newest first, for its editors.

        ``after`` is the sort key the previous page ended on. The partition is
        rebuilt from ``project_id``, so a cursor can never page into another target.
        """
        self._require(project_id, user, "editor")
        return self.audit_trail(project_id, limit=limit, after=after)

    def audit_trail(
        self, project_id: str, *, limit: int, after: Optional[str] = None
    ) -> Tuple[List[AuditRecord], Optional[str]]:
        """A project's trail with no permission check: for callers that have made their own."""
        if not self.audit.configured:
            return [], None
        start = {"PK": f"AUDIT#{TARGET_PROJECT}#{project_id}", "SK": after} if after else None
        records, last = self.audit.repository.list_for_target(TARGET_PROJECT, project_id, limit=limit, cursor=start)
        return records, (last or {}).get("SK")

    # ── permission ──────────────────────────────────────────────────────

    def resolve_permission(self, project_id: str, user: User) -> Tuple[Optional[Project], Optional[ProjectRole]]:
        return resolve_project_role(project_id, user.user_id, user.email, repository=self.repository)

    def _require(
        self, project_id: str, user: User, min_role: ProjectRole, *, writable: bool = False
    ) -> Tuple[Project, ProjectRole]:
        project, role = self.resolve_permission(project_id, user)
        if project is None or role is None:
            raise ProjectNotFoundError("Project not found")
        if ROLE_RANK[role] < ROLE_RANK[min_role]:
            raise ProjectPermissionError(f"This requires the {min_role} role on the project")
        if writable and project.status == "archived":
            raise ProjectConflictError("This project is archived. Restore it to make changes.")
        return project, role

    def authorize(
        self, project_id: str, user: User, min_role: ProjectRole, *, writable: bool = False
    ) -> Tuple[Project, ProjectRole]:
        """The role check, for app-api surfaces that act on a part of the project (its harness)."""
        return self._require(project_id, user, min_role, writable=writable)

    def _require_member_manager(self, project_id: str, user: User) -> Tuple[Project, ProjectRole]:
        project, role = self._require(project_id, user, "editor", writable=True)
        if role == "editor" and not project.settings.editors_manage_members:
            raise ProjectPermissionError("Only the project owner can manage members of this project")
        return project, role

    # ── administration (admin.projects) ─────────────────────────────────

    def admin_list_projects(self, *, limit: int, after: Optional[str] = None) -> Tuple[List[Project], Optional[str]]:
        return self.repository.scan_projects(limit, after)

    def admin_get_project(self, project_id: str) -> Project:
        project = self.repository.get_project(project_id)
        if project is None:
            raise ProjectNotFoundError("Project not found")
        return project

    def admin_set_status(self, project_id: str, admin: User, status: ProjectStatus, reason: Optional[str] = None) -> Project:
        """Archive or restore any project, whoever owns it. Recorded with the admin as actor."""
        project = self.admin_get_project(project_id)
        if project.status == status:
            return project
        try:
            saved = self.repository.put_project(
                project.model_copy(update={"status": status, "updated_at": utc_now_iso()}),
                expected_version=project.version,
            )
        except ProjectWriteConflict as e:
            raise ProjectConflictError("The project changed at the same time. Reload and try again.") from e
        self._record_update(admin, project, saved, reason=reason or "admin")
        return saved

    # ── projects ────────────────────────────────────────────────────────

    def get_project(self, project_id: str, user: User) -> Tuple[Project, ProjectRole]:
        return self._require(project_id, user, "viewer")

    def list_projects(self, user: User, include_archived: bool = False) -> List[Tuple[Project, ProjectRole]]:
        """Owned ∪ shared-in, most recently updated first."""
        results: dict[str, Tuple[Project, ProjectRole]] = {
            p.project_id: (p, "owner") for p in self.repository.list_owned(user.user_id)
        }
        memberships = {
            m.project_id: m for m in self.repository.list_memberships(user.email or "") if m.project_id not in results
        }
        for project_id, project in self.repository.batch_get_projects(memberships).items():
            # Ownership may have moved to this user since the member row was read.
            role: ProjectRole = "owner" if project.owner_id == user.user_id else memberships[project_id].role
            results[project_id] = (project, role)

        listed = [r for r in results.values() if include_archived or r[0].status == "active"]
        return sorted(listed, key=lambda r: r[0].updated_at, reverse=True)

    async def create_project(self, user: User, name: str, description: Optional[str] = None) -> Project:
        """META + hidden harness Agent, or neither.

        The harness is created first so META can point at it. If META then
        fails, the harness is deleted; a harness whose project never existed
        resolves no role for anyone, so even a failed rollback leaves nothing
        reachable.
        """
        clean_name = _clean_name(name)
        clean_description = _clean_description(description)
        project_id = _new_project_id()

        harness_agent_id = await self.harness.create(
            project_id=project_id,
            owner_id=user.user_id,
            owner_name=user.name or user.email,
            name=clean_name,
            description=clean_description,
        )

        now = utc_now_iso()
        project = Project(
            project_id=project_id,
            name=clean_name,
            description=clean_description,
            owner_id=user.user_id,
            owner_email=normalize_email(user.email),
            harness_agent_id=harness_agent_id,
            settings=ProjectSettings(editors_manage_members=editors_manage_members_default()),
            created_at=now,
            updated_at=now,
        )
        try:
            self.repository.create_project(project)
        except Exception:
            logger.error("Project %s META write failed; rolling back harness %s", project_id, harness_agent_id)
            try:
                await self.harness.delete(harness_agent_id)
            except Exception:
                logger.error("Rollback of harness %s failed; it is unreachable (no project)", harness_agent_id, exc_info=True)
            raise
        logger.info("Created project %s with harness %s", project_id, harness_agent_id)
        self.record(AuditAction.PROJECT_CREATED, user, project_id, after={"name": clean_name})
        return project

    def update_project(
        self,
        project_id: str,
        user: User,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        editors_manage_members: Optional[bool] = None,
        status: Optional[ProjectStatus] = None,
    ) -> Tuple[Project, ProjectRole]:
        """Editors change name and description; settings and status are the owner's.

        An archived project accepts exactly one change: the owner restoring it.
        """
        project, role = self._require(project_id, user, "editor")

        owner_only = editors_manage_members is not None or status is not None
        if owner_only and role != "owner":
            raise ProjectPermissionError("Only the project owner can change settings or archive the project")

        restoring = project.status == "archived" and status == "active"
        if project.status == "archived" and not restoring:
            raise ProjectConflictError("This project is archived. Restore it to make changes.")
        if restoring and (name is not None or description is not None or editors_manage_members is not None):
            raise ProjectError("Restore the project first, then edit it")

        updates: dict = {}
        if name is not None:
            updates["name"] = _clean_name(name)
        if description is not None:
            updates["description"] = _clean_description(description)
        if editors_manage_members is not None:
            updates["settings"] = project.settings.model_copy(
                update={"editors_manage_members": editors_manage_members}
            )
        if status is not None:
            updates["status"] = status
        if not updates:
            return project, role

        updates["updated_at"] = utc_now_iso()
        try:
            saved = self.repository.put_project(project.model_copy(update=updates), expected_version=project.version)
        except ProjectWriteConflict as e:
            raise ProjectConflictError("The project changed while you were editing it. Reload and try again.") from e
        self._record_update(user, project, saved)
        return saved, role

    def _record_update(self, actor: User, before: Project, after: Project, reason: Optional[str] = None) -> None:
        if before.status != after.status:
            action = AuditAction.PROJECT_ARCHIVED if after.status == "archived" else AuditAction.PROJECT_RESTORED
            self.record(action, actor, after.project_id, reason=reason)
        fields = {
            "name": (before.name, after.name),
            "description": (before.description, after.description),
            "editorsManageMembers": (
                before.settings.editors_manage_members, after.settings.editors_manage_members
            ),
        }
        changed = sorted(k for k, (old, new) in fields.items() if old != new)
        if changed:
            self.record(
                AuditAction.PROJECT_UPDATED, actor, after.project_id, changes=changed,
                before={k: fields[k][0] for k in changed}, after={k: fields[k][1] for k in changed},
            )

    async def purge_project(self, project_id: str, user: User) -> None:
        """Hard delete. Owner only, archived only.

        Harness first, rows last: if anything fails part-way the project row is
        still there, so the owner can retry, and the harness delete tolerates an
        already-deleted id.
        """
        project, _ = self._require(project_id, user, "owner")
        if project.status != "archived":
            raise ProjectConflictError("Archive the project before deleting it")
        await self.harness.delete(project.harness_agent_id)
        deleted = self.repository.delete_project_rows(project_id)
        logger.info("Purged project %s (%d rows, harness %s)", project_id, deleted, project.harness_agent_id)
        self.record(AuditAction.PROJECT_DELETED, user, project_id, before={"name": project.name})

    # ── tasks ───────────────────────────────────────────────────────────

    def list_shared_tasks(self, project_id: str, user: User) -> List[SharedTask]:
        """Tasks members have shared to the project, most recently shared first.

        Unpaginated: there is at most one pointer per shared task, so the list is
        bounded by how much the project has shared, not by its history.
        """
        self._require(project_id, user, "viewer")
        return sorted(self.repository.list_shared_tasks(project_id), key=lambda t: t.shared_at, reverse=True)

    # ── members ─────────────────────────────────────────────────────────

    def list_members(self, project_id: str, user: User) -> Tuple[Project, ProjectRole, List[ProjectMember]]:
        project, role = self._require(project_id, user, "viewer")
        members = sorted(self.repository.list_members(project_id), key=lambda m: m.email)
        return project, role, members

    def add_members(self, project_id: str, user: User, emails: List[str], role: MemberRole) -> AddMembersResult:
        project, _ = self._require_member_manager(project_id, user)
        if len(emails) > MAX_EMAILS_PER_REQUEST:
            raise ProjectError(f"Add at most {MAX_EMAILS_PER_REQUEST} people at a time")

        result = AddMembersResult()
        cap = max_members()
        for raw in dict.fromkeys(normalize_email(e) for e in emails if e and e.strip()):
            if not is_valid_email(raw):
                result.invalid.append(raw)
                continue
            if raw == project.owner_email:
                result.already_members.append(raw)
                continue
            if result.over_capacity:
                result.over_capacity.append(raw)
                continue
            now = utc_now_iso()
            member = ProjectMember(
                project_id=project_id,
                email=raw,
                role=role,
                invited_by=user.user_id,
                created_at=now,
                updated_at=now,
            )
            try:
                self.repository.add_member(member, max_members=cap, now=now)
                result.added.append(member)
                self.record(AuditAction.PROJECT_MEMBER_ADDED, user, project_id, after={"email": raw, "role": role})
                self._notify("project_invited", raw, user, project, role=role)
            except ProjectWriteConflict as e:
                if 0 in e.failed:
                    result.already_members.append(raw)
                    continue
                current = self.repository.get_project(project_id)
                if current is None:
                    raise ProjectNotFoundError("Project not found") from e
                if current.status != "active":
                    raise ProjectConflictError("This project is archived. Restore it to make changes.") from e
                result.over_capacity.append(raw)
        return result

    def update_member_role(self, project_id: str, user: User, email: str, role: MemberRole) -> ProjectMember:
        project, _ = self._require_member_manager(project_id, user)
        target = normalize_email(email)
        if target == project.owner_email:
            raise ProjectError("The owner's role can't be changed. Transfer ownership instead.")
        current = self.repository.get_member(project_id, target)
        updated = self.repository.update_member_role(project_id, target, role, utc_now_iso())
        if updated is None:
            raise ProjectNotFoundError("That person is not a member of this project")
        if current is not None and current.role != role:
            self.record(
                AuditAction.PROJECT_MEMBER_ROLE_CHANGED, user, project_id, changes=["role"],
                before={"email": target, "role": current.role}, after={"email": target, "role": role},
            )
            self._notify("project_role_changed", target, user, project, role=role)
        return updated

    def remove_member(self, project_id: str, user: User, email: str) -> None:
        """Remove someone else. To remove yourself, :meth:`leave`."""
        target = normalize_email(email)
        if target == normalize_email(user.email or ""):
            return self.leave(project_id, user)
        project, _ = self._require_member_manager(project_id, user)
        if target == project.owner_email:
            raise ProjectError("The owner can't be removed. Transfer ownership first.")
        member = self.repository.get_member(project_id, target)
        self._delete_member(project_id, target)
        self.record(
            AuditAction.PROJECT_MEMBER_REMOVED, user, project_id,
            before={"email": target, "role": member.role if member else None},
        )
        self._notify("project_removed", target, user, project)

    def leave(self, project_id: str, user: User) -> None:
        project, role = self._require(project_id, user, "viewer")
        if role == "owner":
            raise ProjectConflictError("The owner can't leave. Transfer ownership or delete the project.")
        email = normalize_email(user.email)
        self._delete_member(project_id, email)
        self.record(
            AuditAction.PROJECT_MEMBER_REMOVED, user, project_id, before={"email": email, "role": role}, reason="left"
        )

    def _delete_member(self, project_id: str, email: str) -> None:
        try:
            self.repository.remove_member(project_id, email, utc_now_iso())
        except ProjectWriteConflict as e:
            raise ProjectNotFoundError("That person is not a member of this project") from e

    def transfer_ownership(self, project_id: str, user: User, new_owner_email: str) -> Project:
        """Hand the project to an existing editor; the old owner becomes an editor."""
        project, _ = self._require(project_id, user, "owner", writable=True)
        target = normalize_email(new_owner_email)
        member = self.repository.get_member(project_id, target)
        if member is None:
            raise ProjectNotFoundError("That person is not a member of this project")
        if member.role != "editor":
            raise ProjectConflictError("Ownership can only go to an editor. Make them an editor first.")
        if not member.user_id:
            raise ProjectConflictError(
                "They haven't opened this project yet. Ownership can be transferred once they have."
            )
        try:
            self.repository.transfer_ownership(project, member, utc_now_iso())
        except ProjectWriteConflict as e:
            raise ProjectConflictError("The project changed during the transfer. Reload and try again.") from e
        logger.info("Project %s ownership transferred to %s", project_id, member.user_id)
        self.record(
            AuditAction.PROJECT_TRANSFERRED, user, project_id, changes=["ownerEmail"],
            before={"ownerEmail": project.owner_email}, after={"ownerEmail": member.email},
        )
        self._notify("project_ownership_transferred", member.email, user, project)
        transferred = self.repository.get_project(project_id)
        if transferred is None:
            raise ProjectNotFoundError("Project not found")
        return transferred
