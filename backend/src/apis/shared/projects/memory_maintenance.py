"""Starting and reading maintenance runs on a project's shared memory (Shared Projects 2.6).

A member with editor access starts a run here. This layer checks what only a
project knows (membership, role, archived), resolves the model and its prices
from the catalog, takes the space's one maintenance slot, writes the run
``queued`` and invokes the maintenance worker asynchronously. The worker
(``apis/shared/memory/maintenance/runner.py``) does the rest; nothing in this
file calls a model.

The model is the catalog's default unless ``MEMORY_MAINTENANCE_MODEL_ID`` names
another catalog row. Either way it must have a row: that is where its prices
come from, and a run is metered to the project like any task.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from apis.shared.audit.models import AuditAction
from apis.shared.auth.models import User

from .access import resolve_project_role
from .repository import ProjectRepository

logger = logging.getLogger(__name__)

FUNCTION_NAME_ENV = "MEMORY_MAINTENANCE_FUNCTION_NAME"
MODEL_ENV = "MEMORY_MAINTENANCE_MODEL_ID"
# Outlives the worker's 15-minute ceiling, so a worker that died holding the
# slot frees it on its own.
LOCK_LEASE_SECONDS = 20 * 60
RUNS_LISTED = 10


class MaintenanceRequestError(RuntimeError):
    """Refused. ``status_code`` is the HTTP status a route returns."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class MaintenanceModel:
    model_id: str
    input_price_per_million_tokens: float
    output_price_per_million_tokens: float


async def resolve_maintenance_model() -> MaintenanceModel:
    """The model a run uses, with its prices. Async: the catalog read is shared and cached."""
    from apis.shared.models.managed_models import get_default_managed_model, list_all_managed_models

    override = (os.environ.get(MODEL_ENV) or "").strip()
    if override:
        try:
            models = await list_all_managed_models()
        except Exception:
            logger.warning("memory-maintenance: could not read the model catalog", exc_info=True)
            models = []
        row = next((m for m in models if m.model_id == override and m.enabled), None)
        missing = f"The maintenance model {override} isn't an enabled model in the catalog."
    else:
        row = await get_default_managed_model()
        missing = "Memory maintenance needs a default model, and the model catalog doesn't have one."
    if row is None:
        raise MaintenanceRequestError(503, missing)
    if row.provider != "bedrock":
        raise MaintenanceRequestError(
            503, f"Memory maintenance needs a Bedrock model, and {row.model_id} isn't one. "
            "An admin can make a Bedrock model the catalog default."
        )
    return MaintenanceModel(
        model_id=row.model_id,
        input_price_per_million_tokens=row.input_price_per_million_tokens,
        output_price_per_million_tokens=row.output_price_per_million_tokens,
    )


def _new_run_id() -> str:
    """Sorts by creation time, so a space's runs list newest first with one query."""
    return f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"


def _invoke_worker(function_name: str, payload: dict) -> None:
    import boto3

    boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


class ProjectMemoryMaintenance:
    def __init__(
        self,
        repository: Optional[ProjectRepository] = None,
        memory: Any = None,
        audit: Any = None,
        invoke: Optional[Callable[[str, dict], None]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.repository = repository or ProjectRepository()
        self._memory = memory
        self.audit = audit
        self._invoke = invoke or _invoke_worker
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def memory(self):
        if self._memory is None:
            from apis.shared.memory.service import MemorySpaceService

            self._memory = MemorySpaceService()
        return self._memory

    def _space(self, project_id: str, user: User, *, start: bool):
        project, role = resolve_project_role(project_id, user.user_id, user.email, repository=self.repository)
        if project is None or role is None:
            raise MaintenanceRequestError(404, "You are not a member of this project.")
        if role not in ("owner", "editor"):
            raise MaintenanceRequestError(403, "Only the owner and editors can run memory maintenance.")
        if start and project.status != "active":
            raise MaintenanceRequestError(409, "This project is archived, so its memory is read-only.")
        if not project.shared_space_id:
            raise MaintenanceRequestError(409, "This project has no shared memory yet.")
        return project, project.shared_space_id

    def start(self, project_id: str, user: User, model: MaintenanceModel, *, slug: Optional[str] = None):
        """Queue a run over every file, or one ``slug``. Returns the queued run."""
        from apis.shared.memory.maintenance.runner import run_ttl
        from apis.shared.memory.models import MaintenanceRun

        project, space_id = self._space(project_id, user, start=True)
        function_name = (os.environ.get(FUNCTION_NAME_ENV) or "").strip()
        if not function_name:
            raise MaintenanceRequestError(503, "Memory maintenance isn't set up in this deployment.")
        repo = self.memory.repository
        if slug is not None and not any(e.slug == slug for e in repo.get_index(space_id).entries):
            raise MaintenanceRequestError(404, f"'{slug}' isn't in project memory.")

        now = self._clock()
        run = MaintenanceRun(
            run_id=_new_run_id(),
            space_id=space_id,
            project_id=project.project_id,
            slug=slug,
            requested_by=user.user_id,
            requested_by_email=(user.email or "").strip().lower(),
            requested_by_name=user.name or None,
            model_id=model.model_id,
            input_price_per_million_tokens=model.input_price_per_million_tokens,
            output_price_per_million_tokens=model.output_price_per_million_tokens,
            created_at=now.isoformat(),
        )
        if not repo.acquire_maintenance_lock(
            space_id, run.run_id, now=int(now.timestamp()), lease_seconds=LOCK_LEASE_SECONDS
        ):
            raise MaintenanceRequestError(409, "Maintenance is already running on this project's memory.")
        ttl = run_ttl("shared", now=now)
        try:
            repo.put_maintenance_run(run, ttl)
            self._invoke(function_name, {"spaceId": space_id, "runId": run.run_id})
        except Exception:
            logger.warning("memory-maintenance: could not start run %s", run.run_id, exc_info=True)
            run.state, run.error, run.finished_at = "failed", "Maintenance couldn't start.", now.isoformat()
            try:
                repo.put_maintenance_run(run, ttl)
            finally:
                repo.release_maintenance_lock(space_id, run.run_id)
            raise MaintenanceRequestError(503, "Maintenance couldn't start. Try again in a minute.")
        self._record(user, project, run)
        return run

    def list_runs(self, project_id: str, user: User, *, limit: int = RUNS_LISTED) -> List[Any]:
        """The project's recent runs, newest first (owner and editors)."""
        _, space_id = self._space(project_id, user, start=False)
        return [self._as_seen(r) for r in self.memory.repository.list_maintenance_runs(space_id, limit)]

    def get_run(self, project_id: str, user: User, run_id: str):
        _, space_id = self._space(project_id, user, start=False)
        run = self.memory.repository.get_maintenance_run(space_id, run_id)
        if run is None:
            raise MaintenanceRequestError(404, "Not found")
        return self._as_seen(run)

    def _as_seen(self, run: Any) -> Any:
        """A run still queued or running after its lease has expired is reported as failed.

        The worker was killed (a timeout, a crash) or never ran (the bootstrap
        image), and the slot is already free. The row itself is left alone: a
        late worker would refuse it anyway, since it only starts from queued.
        """
        if run.state not in ("queued", "running"):
            return run
        try:
            created = datetime.fromisoformat(run.created_at)
        except ValueError:
            return run
        if (self._clock() - created).total_seconds() <= LOCK_LEASE_SECONDS:
            return run
        return run.model_copy(update={"state": "failed", "error": "Maintenance didn't finish. Try running it again."})

    def _record(self, user: User, project: Any, run: Any) -> None:
        if self.audit is None:
            return
        from apis.shared.audit import TARGET_PROJECT

        after = {"runId": run.run_id}
        if run.slug:
            after["slug"] = run.slug
        self.audit.record(
            action=AuditAction.PROJECT_MEMORY_MAINTENANCE_STARTED, actor=user, target_type=TARGET_PROJECT,
            target_id=project.project_id, after=after,
        )
