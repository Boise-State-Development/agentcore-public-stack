"""A project's settings: its harness's instructions, model, tools and skills (PR-1.5a).

These routes are the harness's only write path: the agent routes refuse a harness
(``PROJECT_HARNESS_EDIT_MESSAGE``), because every save here cuts an ``AgentVersion``
(shared-projects §3.2), and that version list is the project's instruction history.

Authorization is the project's (viewer reads, editor writes, an archived project
is read-only). What the saver may *bind* is still their own: a tool, skill or
model they add is validated against their RBAC with the same
``validate_agent_write`` the agent designer uses. Only what a save adds is
checked, so a binding another member added earlier is never re-judged against
someone who merely edited the instructions.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from apis.app_api.agent_designer.services.binding_validation import validate_agent_write
from apis.shared.assistants.models import AgentBinding, AgentModelConfig, AgentVersion, Assistant
from apis.shared.assistants.service import (
    _get_assistant_cloud_without_ownership_check,
    update_assistant,
)
from apis.shared.assistants.version_diff import changed_fields, instructions_diff
from apis.shared.assistants.version_repository import (
    create_version,
    get_latest_version,
    get_version,
    list_versions,
)
from apis.shared.assistants.versions import snapshot_of
from apis.shared.audit import AuditAction
from apis.shared.auth.models import User
from apis.shared.projects.models import Project, ProjectRole
from apis.shared.projects.service import ProjectNotFoundError, ProjectService
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

TOOL = "tool"
SKILL = "skill"

# Stored on each version beside ``createdBy`` (a user id, which this API never
# returns), so history can name an editor who has since left the project.
CREATED_BY_EMAIL = "createdByEmail"

# What the project's settings history reports as changed. A version snapshot also
# carries the harness's name and description, which follow the project's own and
# change without a version (``ProjectService._sync_harness_identity``), so the first
# save after a rename would otherwise list "name" as something that save changed.
SETTINGS_FIELDS = ("instructions", "bindings", "model_settings")


def settings_changes(before: Optional[AgentVersion], after: AgentVersion) -> List[Tuple[str, object, object]]:
    return [change for change in changed_fields(before, after) if change[0] in SETTINGS_FIELDS]


@dataclass
class HarnessView:
    project: Project
    role: ProjectRole
    harness: Assistant
    version: Optional[int]

    @property
    def can_edit(self) -> bool:
        return self.role in ("owner", "editor") and self.project.status == "active"


@dataclass
class VersionDetail:
    version: AgentVersion
    previous: Optional[AgentVersion]


def created_by_email(version: AgentVersion) -> Optional[str]:
    return (version.model_extra or {}).get(CREATED_BY_EMAIL)


def _dumps(bindings: List[AgentBinding]) -> List[dict]:
    return [b.model_dump(by_alias=True) for b in bindings]


async def load_harness(project: Project) -> Assistant:
    """The project's harness record. Call only after authorizing the caller on the project."""
    table = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")
    harness = await _get_assistant_cloud_without_ownership_check(project.harness_agent_id, table)
    if harness is None:
        logger.error("Project %s has no harness agent %s", project.project_id, project.harness_agent_id)
        raise ProjectNotFoundError("This project's settings could not be found")
    return harness


class HarnessSettingsService:
    def __init__(self, projects: ProjectService):
        self.projects = projects

    async def _harness(self, project: Project) -> Assistant:
        return await load_harness(project)

    async def get(self, project_id: str, user: User) -> HarnessView:
        project, role = self.projects.authorize(project_id, user, "viewer")
        harness = await self._harness(project)
        latest = await get_latest_version(harness.assistant_id)
        return HarnessView(project, role, harness, latest.version if latest else None)

    async def update(
        self,
        project_id: str,
        user: User,
        *,
        instructions: Optional[str] = None,
        model_settings: Optional[AgentModelConfig] = None,
        kind: Optional[str] = None,
        bindings: Optional[List[AgentBinding]] = None,
    ) -> HarnessView:
        """Apply one settings change and cut a version for it. A no-op cuts nothing.

        ``kind`` + ``bindings`` replace every binding of that kind and keep the rest
        (the memory space binding, the other kind), in their original order.
        """
        project, role = self.projects.authorize(project_id, user, "editor", writable=True)
        harness = await self._harness(project)

        new_bindings = None
        added: List[AgentBinding] = []
        if kind is not None and bindings is not None:
            current = list(harness.bindings or [])
            wanted = [AgentBinding(kind=kind, ref=b.ref, config=b.config) for b in bindings]
            existing = _dumps([b for b in current if b.kind == kind])
            added = [b for b in wanted if b.model_dump(by_alias=True) not in existing]
            new_bindings = [b for b in current if b.kind != kind] + wanted
            if _dumps(new_bindings) == _dumps(current):
                new_bindings = None

        if instructions is not None and instructions == (harness.instructions or ""):
            instructions = None
        if model_settings is not None and harness.model_settings is not None and (
            model_settings.model_dump(by_alias=True) == harness.model_settings.model_dump(by_alias=True)
        ):
            model_settings = None

        if instructions is None and model_settings is None and new_bindings is None:
            latest = await get_latest_version(harness.assistant_id)
            return HarnessView(project, role, harness, latest.version if latest else None)

        await validate_agent_write(user, bindings=added or None, model_settings=model_settings)

        # The first save also records the state the project was created with, so the
        # first change in its history has something to be compared against.
        if await get_latest_version(harness.assistant_id) is None:
            await create_version(harness.assistant_id, snapshot_of(harness, created_at=harness.updated_at))

        updated = await update_assistant(
            assistant_id=harness.assistant_id,
            owner_id=harness.owner_id,
            instructions=instructions,
            model_settings=model_settings,
            bindings=new_bindings,
        )
        if updated is None:
            raise ProjectNotFoundError("This project's settings could not be found")

        snapshot = snapshot_of(updated, created_at=utc_now_iso(), created_by=user.user_id)
        snapshot = AgentVersion(**snapshot.model_dump(by_alias=True), **{CREATED_BY_EMAIL: user.email})
        version = await create_version(updated.assistant_id, snapshot)
        logger.info("Project %s settings saved as version %s", scrub_log(project_id), version.version)
        self._record(user, project_id, version.version, harness, instructions, model_settings, kind, new_bindings)
        return HarnessView(project, role, updated, version.version)

    def _record(self, user, project_id, version, before, instructions, model_settings, kind, new_bindings) -> None:
        """Audit what the save changed. Instruction text stays in the version history, not the trail."""
        at = {"version": version}
        if instructions is not None:
            self.projects.record(AuditAction.PROJECT_INSTRUCTIONS_UPDATED, user, project_id, after=at)
        if model_settings is not None:
            self.projects.record(
                AuditAction.PROJECT_MODEL_UPDATED, user, project_id,
                before={"modelId": before.model_settings.model_id if before.model_settings else None},
                after={**at, "modelId": model_settings.model_id},
            )
        if new_bindings is not None:
            action = AuditAction.PROJECT_TOOLS_UPDATED if kind == TOOL else AuditAction.PROJECT_SKILLS_UPDATED
            refs = lambda bindings: sorted(b.ref for b in bindings or [] if b.kind == kind)  # noqa: E731
            self.projects.record(
                action, user, project_id,
                before={"refs": refs(before.bindings)}, after={**at, "refs": refs(new_bindings)},
            )

    async def list_versions(self, project_id: str, user: User, limit: int) -> List[Tuple[AgentVersion, List[str]]]:
        """Newest first, each with the fields it changed from the one before it."""
        project, _ = self.projects.authorize(project_id, user, "viewer")
        versions = await list_versions(project.harness_agent_id, limit=limit + 1)
        return [
            (v, [field for field, _, _ in settings_changes(versions[i + 1] if i + 1 < len(versions) else None, v)])
            for i, v in enumerate(versions[:limit])
        ]

    async def get_version(self, project_id: str, user: User, number: int) -> VersionDetail:
        project, _ = self.projects.authorize(project_id, user, "viewer")
        version = await get_version(project.harness_agent_id, number) if number >= 1 else None
        if version is None:
            raise ProjectNotFoundError("That version does not exist")
        previous = await get_version(project.harness_agent_id, number - 1) if number > 1 else None
        return VersionDetail(version, previous)


def version_instructions_diff(detail: VersionDetail) -> List[str]:
    return instructions_diff(
        detail.previous,
        detail.version,
        fromfile=f"version {detail.previous.version}" if detail.previous else "",
        tofile=f"version {detail.version.version}",
    )
