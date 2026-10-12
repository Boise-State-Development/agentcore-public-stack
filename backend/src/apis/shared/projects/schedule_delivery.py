"""Delivering a scheduled run to its project (Shared Projects 3.2b).

A project schedule set to deliver to the project shares each completed run's task
with the project, exactly as its creator would from the Share dialog: a snapshot of
the conversation, listed under "Shared with the project" with the note
"Scheduled: <label>", where members open it and the project's assistant reads it
through ``shared_conversations_list`` / ``shared_conversation_read`` (2.5c). So a weekly digest can
build on last week's.

Runs in the scheduled-runs worker, which can't import app-api: the snapshot goes
through ``apis.shared.shares.writer`` (the writer app-api's shares use) and the
conversation is read with ``get_messages``, the same reader.

Bounded: a schedule keeps only its newest :func:`runs_kept` deliveries shared.
Older ones are unshared (row, body and pointer), so a daily schedule can't bury
what people share by hand, nor fill the assistant's 50-task listing with itself.
The creator's own tasks are never touched.

Outputs (3.2d): a schedule that also publishes its outputs shares each artifact
the delivered run made with the project, as its creator would from the artifact's
Share dialog (3.3), and unshares its previous run's, so Outputs hold the latest
edition. Only shares this schedule made are unshared; an artifact the creator
shared by hand stays.

Sync, like ``apis.shared.projects.schedules``: the worker calls it through
``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from apis.shared.auth.models import User
from apis.shared.scheduled_prompts.models import ScheduledPrompt

from .models import Project, normalize_email
from .repository import ProjectRepository
from .schedules import _wait, run_block_reason

logger = logging.getLogger(__name__)

DEFAULT_RUNS_KEPT = 10
#: The share's note; the SPA and the assistant both read it.
NOTE_PREFIX = "Scheduled: "
_NOTE_MAX = 280


def runs_kept() -> int:
    """How many of one schedule's delivered runs stay shared (``PROJECTS_SCHEDULE_RUNS_KEPT``)."""
    try:
        return max(1, int(os.environ.get("PROJECTS_SCHEDULE_RUNS_KEPT", DEFAULT_RUNS_KEPT)))
    except ValueError:
        return DEFAULT_RUNS_KEPT


def _creator(schedule: ScheduledPrompt) -> User:
    return User(email=normalize_email(schedule.owner_email or ""), user_id=schedule.user_id, name="", roles=[])


class ScheduleDelivery:
    """Shares a completed run's task with its project. Injectable stores for tests."""

    def __init__(
        self,
        repository: Optional[ProjectRepository] = None,
        table=None,
        snapshot_store=None,
        artifacts_table=None,
        notifications=None,
        audit=None,
    ):
        self.repository = repository or ProjectRepository()
        self._table = table
        self._snapshot_store = snapshot_store
        self._artifacts_table = artifacts_table
        self._notifications = notifications
        self._audit = audit

    # Built on first use: most runs aren't delivered.
    @property
    def table(self):
        if self._table is None:
            from apis.shared.shares.writer import shares_table

            self._table = shares_table()
        return self._table

    @property
    def snapshot_store(self):
        if self._snapshot_store is None:
            from apis.shared.shares.snapshot_store import get_share_snapshot_store

            self._snapshot_store = get_share_snapshot_store()
        return self._snapshot_store

    def deliver(self, schedule: ScheduledPrompt, session_id: str) -> Optional[str]:
        """Share the run with the project. Returns the share id, or None when it wasn't shared.

        Never raises: a run that completed stays completed whatever happens here.
        """
        if schedule.deliver_to != "project" or not schedule.project_id:
            return None
        try:
            return self._deliver(schedule, session_id)
        except Exception:
            logger.error(
                "Could not deliver run %s of schedule %s to project %s",
                session_id, schedule.schedule_id, schedule.project_id, exc_info=True,
            )
            return None

    def _deliver(self, schedule: ScheduledPrompt, session_id: str) -> Optional[str]:
        from apis.shared.sessions.messages import get_messages
        from apis.shared.sessions.metadata import get_session_metadata
        from apis.shared.shares.writer import write_share

        # The run started minutes ago; the creator may have lost their standing since.
        reason = run_block_reason(schedule, self.repository)
        if reason is not None:
            logger.info("Not delivering schedule %s: %s", schedule.schedule_id, reason)
            return None
        if self.table is None or not self.snapshot_store.enabled:
            logger.warning("Share storage isn't configured; schedule %s's run stays private", schedule.schedule_id)
            return None
        project = self.repository.get_project(schedule.project_id)
        metadata = _wait(get_session_metadata(session_id=session_id, user_id=schedule.user_id))
        prefs = getattr(metadata, "preferences", None) if metadata else None
        if getattr(prefs, "project_id", None) != schedule.project_id:
            # The harness tags its sessions with the project; anything else ran elsewhere.
            logger.warning("Run %s of schedule %s isn't a task in its project; not sharing it", session_id, schedule.schedule_id)
            return None
        messages = _wait(get_messages(session_id=session_id, user_id=schedule.user_id))
        artifacts = self._artifacts(schedule, session_id)
        note = (NOTE_PREFIX + schedule.label)[:_NOTE_MAX]
        item = write_share(
            table=self.table,
            snapshot_store=self.snapshot_store,
            projects=self.repository,
            session_id=session_id,
            owner_id=schedule.user_id,
            owner_email=normalize_email(schedule.owner_email or ""),
            access_level="project",
            metadata=metadata.model_dump(by_alias=True, exclude_none=True),
            messages=[m.model_dump(by_alias=True, exclude_none=True) for m in messages.messages],
            artifacts=artifacts,
            tool_summaries=messages.tool_summaries,
            project_id=schedule.project_id,
            note=note,
            schedule_id=schedule.schedule_id,
        )
        notified = self._notify(schedule, project, item) if schedule.notify_members else 0
        self._record(schedule, item, notified)
        self._prune(schedule)
        if schedule.publish_outputs:
            self._publish_outputs(schedule, session_id, artifacts)
        logger.info("Delivered run %s of schedule %s as share %s", session_id, schedule.schedule_id, item["share_id"])
        return item["share_id"]

    def _publish_outputs(self, schedule: ScheduledPrompt, session_id: str, artifacts: List[Dict[str, Any]]) -> None:
        """Share the run's artifacts with the project, then retire the previous run's. Best-effort."""
        from apis.shared.shares.artifact_shares import write_share_rows, output_pointer, delete_share_rows
        from apis.shared.shares.artifacts import artifacts_table

        table = self._artifacts_table if self._artifacts_table is not None else artifacts_table()
        if table is None:
            logger.warning("The artifacts table isn't configured; schedule %s publishes nothing", schedule.schedule_id)
            return
        published: set = set()
        for head in artifacts:
            now = datetime.now(timezone.utc).isoformat()
            share = {
                "share_id": str(uuid.uuid4()),
                "artifact_id": head["artifact_id"],
                "version": int(head["version"]),
                "owner_id": schedule.user_id,
                "owner_email": normalize_email(schedule.owner_email or ""),
                "access_level": "project",
                "title": head.get("title", ""),
                "content_type": head.get("content_type") or "text/html; charset=utf-8",
                "session_id": session_id,
                "project_id": schedule.project_id,
                "schedule_id": schedule.schedule_id,
                "created_at": now,
                "updated_at": now,
            }
            try:
                write_share_rows(table, share)
            except Exception:
                logger.warning("Could not publish artifact %s of schedule %s", head["artifact_id"], schedule.schedule_id, exc_info=True)
                continue
            try:
                self.repository.put_output(output_pointer(share))
            except Exception:
                # Unlisted, it is a share nobody could find: undo it, as a hand share would be.
                logger.warning("Could not list artifact %s in project %s; unsharing it", head["artifact_id"], schedule.project_id, exc_info=True)
                try:
                    delete_share_rows(table, share)
                except Exception:
                    logger.warning("Could not unshare artifact %s", head["artifact_id"], exc_info=True)
                continue
            published.add(head["artifact_id"])
            self._record_output(schedule, share)
        if published:
            # A run that made nothing leaves the last edition in place.
            self._retire_outputs(schedule, table, keep=published)

    def _retire_outputs(self, schedule: ScheduledPrompt, table: Any, *, keep: set) -> None:
        """Unshare this schedule's earlier outputs, rebuilding each pointer from what remains. Best-effort."""
        from apis.shared.shares.artifact_shares import delete_share_rows, shares_for_artifact, sync_project_output

        try:
            outputs = [
                o for o in self.repository.list_outputs(schedule.project_id)
                if o.schedule_id == schedule.schedule_id and o.artifact_id not in keep
            ]
            for output in outputs:
                for share in shares_for_artifact(table, schedule.user_id, output.artifact_id):
                    # Only what this schedule shared: never a share the creator made by hand.
                    if share.get("schedule_id") == schedule.schedule_id and share.get("project_id") == schedule.project_id:
                        delete_share_rows(table, share)
                sync_project_output(table, schedule.user_id, output.artifact_id, schedule.project_id, repository=self.repository)
        except Exception:
            logger.warning("Could not retire the earlier outputs of schedule %s", schedule.schedule_id, exc_info=True)

    def _record_output(self, schedule: ScheduledPrompt, share: Dict[str, Any]) -> None:
        """``project.output_shared`` by the creator, marked as a schedule's."""
        try:
            if self._audit is None:
                from apis.shared.audit import get_audit_service

                self._audit = get_audit_service()
            from apis.shared.audit import TARGET_PROJECT, AuditAction

            self._audit.record(
                action=AuditAction.PROJECT_OUTPUT_SHARED,
                actor=_creator(schedule),
                target_type=TARGET_PROJECT,
                target_id=schedule.project_id,
                after={
                    "artifactId": share["artifact_id"],
                    "version": share["version"],
                    "title": share["title"],
                    "scheduleId": schedule.schedule_id,
                },
                reason="schedule",
            )
        except Exception:
            logger.warning("Could not record an output of schedule %s", schedule.schedule_id, exc_info=True)

    def _artifacts(self, schedule: ScheduledPrompt, session_id: str) -> List[Dict[str, Any]]:
        """The run's artifacts at HEAD; best-effort, as for any share."""
        from apis.shared.shares.artifacts import artifacts_table, session_artifact_heads

        try:
            table = self._artifacts_table if self._artifacts_table is not None else artifacts_table()
            return session_artifact_heads(table, user_id=schedule.user_id, session_id=session_id) if table else []
        except Exception:
            logger.warning("Could not snapshot the artifacts of run %s; sharing without them", session_id, exc_info=True)
            return []

    def _notify(self, schedule: ScheduledPrompt, project: Optional[Project], item: Dict[str, Any]) -> int:
        """Every member and the owner, minus the creator: the same notice a hand share sends."""
        if project is None:
            return 0
        if self._notifications is None:
            from apis.shared.notifications.service import NotificationService

            self._notifications = NotificationService()
        recipients = {normalize_email(m.email) for m in self.repository.list_members(project.project_id)}
        recipients.add(normalize_email(project.owner_email))
        payload = {"shareId": item["share_id"], "title": item.get("title") or "", "note": item.get("note")}
        return self._notifications.notify_many(
            recipients,
            kind="project_task_shared",
            actor=_creator(schedule),
            project_id=project.project_id,
            project_name=project.name,
            payload=payload,
        )

    def _record(self, schedule: ScheduledPrompt, item: Dict[str, Any], notified: int) -> None:
        """``project.task_shared`` on the project's trail, by the creator, marked as a schedule's."""
        try:
            if self._audit is None:
                from apis.shared.audit import get_audit_service

                self._audit = get_audit_service()
            from apis.shared.audit import TARGET_PROJECT, AuditAction

            self._audit.record(
                action=AuditAction.PROJECT_TASK_SHARED,
                actor=_creator(schedule),
                target_type=TARGET_PROJECT,
                target_id=schedule.project_id,
                after={
                    "shareId": item["share_id"],
                    "title": item.get("title") or "",
                    "scheduleId": schedule.schedule_id,
                    "notified": notified,
                    "hasNote": True,
                },
                reason="schedule",
            )
        except Exception:
            logger.warning("Could not record the delivery of schedule %s", schedule.schedule_id, exc_info=True)

    def _prune(self, schedule: ScheduledPrompt) -> None:
        """Unshare this schedule's deliveries past the newest :func:`runs_kept`. Best-effort."""
        from apis.shared.shares.writer import delete_share

        try:
            delivered = sorted(
                (t for t in self.repository.list_shared_tasks(schedule.project_id) if t.schedule_id == schedule.schedule_id),
                key=lambda t: t.shared_at,
                reverse=True,
            )
            for task in delivered[runs_kept():]:
                row = self.table.get_item(Key={"share_id": task.share_id}).get("Item")
                # Only a share this schedule made: never a share the creator made by hand.
                if row and row.get("schedule_id") == schedule.schedule_id and row.get("owner_id") == schedule.user_id:
                    delete_share(self.table, self.snapshot_store, row)
                self.repository.delete_shared_task(schedule.project_id, task.session_id)
        except Exception:
            logger.warning("Could not unshare older runs of schedule %s", schedule.schedule_id, exc_info=True)
