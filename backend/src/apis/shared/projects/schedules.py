"""Schedules that run in a project (Shared Projects 3.2).

A project schedule is an ordinary scheduled prompt (``apis.shared.scheduled_prompts``)
with ``projectId`` set and the project's harness as its agent. It lives in its
creator's partition and runs as them, because the headless grant it runs on is
theirs; the project keeps a ``SCHEDULE#`` pointer so members can list it, and a
``SCHEDULE_RUN#`` row per run so they can see how it has gone.

Running needs what creating needed: an active project and a creator who is still
an editor or the owner. :func:`run_block_reason` is that one check. It runs in the
dispatcher before every run, and on resume. The project service also pauses
schedules the moment one of those stops holding (a member removed, demoted, or
the project archived), so a schedule never waits for its next due time to stop,
and the dispatcher check is the backstop when that write is missed.

One sync API for both callers: the dispatcher and worker Lambdas call it directly,
and app-api's async routes reach it through ``asyncio.to_thread``. The schedule
store's functions are ``async`` in name only (they make blocking boto3 calls), so
:func:`_wait` runs them to completion here.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Awaitable, Iterable, List, Optional, TypeVar

from apis.shared.auth.models import User
from apis.shared.feature_flags import projects_enabled
from apis.shared.scheduled_prompts import service as schedules
from apis.shared.scheduled_prompts.models import ScheduledPrompt

from .access import resolve_project_role
from .models import ROLE_RANK, Project, ProjectSchedulePointer, ProjectScheduleRun, ScheduleRunStatus, normalize_email
from .repository import SCHEDULE_RUN_RETENTION_DAYS, ProjectRepository

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Why a project schedule stopped. Stored as the schedule's ``stateReason`` and
# sent in the ``project_schedule_paused`` notification; the SPA words them.
REASON_PROJECT_ARCHIVED = "project_archived"
REASON_PROJECT_DELETED = "project_deleted"
REASON_MEMBER_REMOVED = "member_removed"
REASON_NOT_EDITOR = "not_editor"
REASON_PROJECTS_DISABLED = "projects_disabled"

#: The role a schedule's creator needs, to create it and for it to keep running.
SCHEDULE_ROLE = "editor"


def _wait(awaitable: Awaitable[T]) -> T:
    """Run one of the schedule store's coroutines from sync code (no loop in this thread)."""
    return asyncio.run(awaitable)  # type: ignore[arg-type]


def _system_actor() -> User:
    """The actor on a pause nobody pressed a button for (the dispatcher's)."""
    return User(email="", user_id="system", name="Scheduled runs", roles=[])


def run_block_reason(schedule: ScheduledPrompt, repository: Optional[ProjectRepository] = None) -> Optional[str]:
    """Why this project schedule may not run now, or ``None`` when it may.

    The same rule as creating one: Projects switched on, the project there and
    active, and the creator still an editor or the owner.
    """
    if not schedule.project_id:
        return None
    if not projects_enabled():
        return REASON_PROJECTS_DISABLED
    project, role = resolve_project_role(
        schedule.project_id, schedule.user_id, schedule.owner_email, repository=repository
    )
    if project is None:
        return REASON_PROJECT_DELETED
    if project.status != "active":
        return REASON_PROJECT_ARCHIVED
    if role is None:
        return REASON_MEMBER_REMOVED
    if ROLE_RANK[role] < ROLE_RANK[SCHEDULE_ROLE]:
        return REASON_NOT_EDITOR
    return None


def next_run_at(schedule: ScheduledPrompt) -> str:
    return schedules.compute_next_run_at(
        schedule.cadence,
        schedule.hour_local,
        schedule.timezone,
        weekday=schedule.weekday,
        interval_minutes=schedules.interval_to_minutes(schedule.interval_value, schedule.interval_unit),
    )


class ProjectSchedules:
    """The project side of schedules: pointers, run rows, and pausing on a change of standing."""

    def __init__(self, repository: Optional[ProjectRepository] = None, notifications=None):
        self.repository = repository or ProjectRepository()
        self._notifications = notifications

    @property
    def notifications(self):
        if self._notifications is None:
            from apis.shared.notifications.service import NotificationService

            self._notifications = NotificationService()
        return self._notifications

    # ── reading ─────────────────────────────────────────────────────────

    def list(self, project_id: str) -> List[ScheduledPrompt]:
        """The project's schedules, read live from their creators' partitions. Oldest first."""
        pointers = self.repository.list_schedules(project_id)
        found = _wait(schedules.get_scheduled_prompts([(p.owner_id, p.schedule_id) for p in pointers]))
        # A schedule whose pointer outlived it (deleted from the personal list
        # before the pointer write) is simply not listed.
        mine = [s for s in found if s.project_id == project_id]
        return sorted(mine, key=lambda s: s.created_at)

    def get(self, project_id: str, schedule_id: str) -> Optional[ScheduledPrompt]:
        pointer = self.repository.get_schedule(project_id, schedule_id)
        if pointer is None:
            return None
        schedule = _wait(schedules.get_scheduled_prompt(pointer.owner_id, schedule_id))
        return schedule if schedule is not None and schedule.project_id == project_id else None

    def runs(self, project_id: str, schedule_id: str, limit: int = 20) -> List[ProjectScheduleRun]:
        return self.repository.list_schedule_runs(project_id, schedule_id, limit)

    # ── writing ─────────────────────────────────────────────────────────

    def add_pointer(self, schedule: ScheduledPrompt) -> None:
        self.repository.put_schedule(
            ProjectSchedulePointer(
                project_id=schedule.project_id,
                schedule_id=schedule.schedule_id,
                owner_id=schedule.user_id,
                owner_email=normalize_email(schedule.owner_email or ""),
                created_at=schedule.created_at,
            )
        )

    def delete(self, schedule: ScheduledPrompt) -> None:
        """Delete the schedule and its project rows. Pointer last, so a failure leaves it listed for a retry."""
        _wait(schedules.delete_scheduled_prompt(schedule.user_id, schedule.schedule_id))
        if schedule.project_id:
            self.repository.delete_schedule(schedule.project_id, schedule.schedule_id)

    def record_run(
        self, schedule: ScheduledPrompt, status: ScheduleRunStatus, reason: Optional[str] = None
    ) -> None:
        """A run row for members (best-effort: a missed row costs history, never the run)."""
        if not schedule.project_id:
            return
        from apis.shared.timestamps import utc_now_iso

        try:
            self.repository.put_schedule_run(
                ProjectScheduleRun(
                    project_id=schedule.project_id,
                    schedule_id=schedule.schedule_id,
                    run_id=uuid.uuid4().hex[:12],
                    status=status,
                    finished_at=utc_now_iso(),
                    reason=reason,
                ),
                ttl=int(time.time()) + SCHEDULE_RUN_RETENTION_DAYS * 86400,
            )
        except Exception:
            logger.warning("Could not record a run of schedule %s", schedule.schedule_id, exc_info=True)

    def pause(
        self,
        schedule: ScheduledPrompt,
        reason: str,
        *,
        project: Optional[Project] = None,
        actor: Optional[User] = None,
        notify: bool = True,
    ) -> bool:
        """Pause an active project schedule for ``reason`` and say so. Returns whether it paused.

        A schedule its creator already paused is left alone: its reason is theirs,
        and resuming re-checks the rule anyway. The creator and the project's
        owner are told, except whoever caused it.
        """
        if schedule.state != "active":
            return False
        paused = _wait(
            schedules.set_schedule_state(schedule.user_id, schedule.schedule_id, "paused_error", state_reason=reason)
        )
        if not paused:
            return False
        self.record_run(schedule, "paused", reason)
        if notify:
            self._notify_paused(schedule, reason, project=project, actor=actor or _system_actor())
        logger.info("Paused project schedule %s (%s)", schedule.schedule_id, reason)
        return True

    def pause_where(
        self,
        project: Project,
        reason: str,
        *,
        owner_email: Optional[str] = None,
        actor: Optional[User] = None,
        notify: bool = True,
    ) -> int:
        """Pause the project's active schedules, or only those ``owner_email`` created. Best-effort."""
        target = normalize_email(owner_email) if owner_email else None
        try:
            pointers = [
                p for p in self.repository.list_schedules(project.project_id)
                if target is None or p.owner_email == target
            ]
            if not pointers:
                return 0
            found = _wait(schedules.get_scheduled_prompts([(p.owner_id, p.schedule_id) for p in pointers]))
        except Exception:
            logger.warning("Could not read schedules of %s to pause them (%s)", project.project_id, reason, exc_info=True)
            return 0
        paused = 0
        for schedule in found:
            try:
                if self.pause(schedule, reason, project=project, actor=actor, notify=notify):
                    paused += 1
            except Exception:
                logger.warning("Could not pause schedule %s (%s)", schedule.schedule_id, reason, exc_info=True)
        return paused

    def resume_archived(self, project: Project) -> int:
        """After a restore: resume what archiving paused, where the creator may still run it."""
        try:
            found = self.list(project.project_id)
        except Exception:
            logger.warning("Could not read schedules of %s to resume them", project.project_id, exc_info=True)
            return 0
        resumed = 0
        for schedule in found:
            if schedule.state != "paused_error" or schedule.state_reason != REASON_PROJECT_ARCHIVED:
                continue
            reason = run_block_reason(schedule, self.repository)
            if reason is not None:
                # Still blocked, now for something only a person can fix: say so.
                _wait(schedules.set_schedule_state(
                    schedule.user_id, schedule.schedule_id, "paused_error", state_reason=reason
                ))
                continue
            try:
                _wait(schedules.set_schedule_state(
                    schedule.user_id, schedule.schedule_id, "active", next_run_at=next_run_at(schedule)
                ))
                resumed += 1
            except Exception:
                logger.warning("Could not resume schedule %s", schedule.schedule_id, exc_info=True)
        return resumed

    def delete_all(self, project_id: str) -> int:
        """For a project purge: every schedule that runs in it, then its pointers."""
        deleted = 0
        for pointer in self.repository.list_schedules(project_id):
            _wait(schedules.delete_scheduled_prompt(pointer.owner_id, pointer.schedule_id))
            deleted += 1
        return deleted

    # ── notifications ───────────────────────────────────────────────────

    def _notify_paused(self, schedule: ScheduledPrompt, reason: str, *, project: Optional[Project], actor: User) -> None:
        if project is None and schedule.project_id:
            project = self.repository.get_project(schedule.project_id)
        recipients: Iterable[str] = {
            e for e in (schedule.owner_email, project.owner_email if project else None) if e
        }
        try:
            self.notifications.notify_many(
                recipients,
                kind="project_schedule_paused",
                actor=actor,
                project_id=schedule.project_id,
                project_name=project.name if project else None,
                payload={
                    "scheduleId": schedule.schedule_id,
                    "label": schedule.label,
                    "reason": reason,
                    "createdBy": normalize_email(schedule.owner_email or ""),
                },
            )
        except Exception:
            logger.warning("Could not send the pause notice for schedule %s", schedule.schedule_id, exc_info=True)
