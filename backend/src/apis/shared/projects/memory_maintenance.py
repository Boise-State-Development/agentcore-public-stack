"""Starting, reading and undoing maintenance runs on a project's memory (Shared Projects 2.6).

Two scopes, as on the Memory page. ``project`` is the shared memory: the owner
or an editor starts a run, and it ends in compaction proposals for an editor to
review (2.6a). ``mine`` is the caller's own memory in the project (2.6b): any
member may tidy their own, nobody else can, and the worker saves the changes
straight away. The run row keeps what changed, and :meth:`ProjectMemoryMaintenance.undo`
puts the files back from the run's snapshot within the personal archive
retention (``MEMORY_PERSONAL_ARCHIVE_RETENTION_DAYS``, 30 days).

This layer checks what only a project knows (membership, role, archived),
resolves the model and its prices from the catalog, takes the space's one
maintenance slot, writes the run ``queued`` and invokes the maintenance worker
asynchronously. The worker (``apis/shared/memory/maintenance/runner.py``) does
the rest; nothing in this file calls a model.

The model is the catalog's default unless ``MEMORY_MAINTENANCE_MODEL_ID`` names
another catalog row. Either way it must have a row: that is where its prices
come from, and a run is metered to the project like any task.

**Undo, per file.** A file is put back only while it is exactly what the run
wrote (its content hash), so an edit made since the run is never thrown away;
that file is reported as changed instead, and its History still has the version
from before the run. A file deleted since is reported as missing. A restore is
a new version (``reason: restore``), never a rewrite of history, and the items
it brings back leave the archive with the provenance they had.

**A split is undone whole** (2.6c). The file it came from is put back, and the
file it made taken away again, only while *both* are exactly as the run wrote
them; otherwise both stay as they are. Items that come back from the new file
keep their provenance (without the move), and the pointer item the split left
goes without an archive row, since nothing it said is lost.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Literal, Optional, Tuple

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

# The Memory page's scopes: the project's shared memory, or the caller's own.
MaintenanceScope = Literal["project", "mine"]
_SPACE_SCOPES = {"project": "shared", "mine": "personal_in_project"}


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

    def _space(self, project_id: str, user: User, *, start: bool, scope: MaintenanceScope = "project") -> Tuple[Any, str]:
        project, role = resolve_project_role(project_id, user.user_id, user.email, repository=self.repository)
        if project is None or role is None:
            raise MaintenanceRequestError(404, "You are not a member of this project.")
        if scope == "project" and role not in ("owner", "editor"):
            raise MaintenanceRequestError(403, "Only the owner and editors can run memory maintenance.")
        if start and project.status != "active":
            raise MaintenanceRequestError(409, "This project is archived, so its memory is read-only.")
        if scope == "mine":
            # Always the caller's own space: nobody can address another member's.
            space_id = self.repository.get_personal_space_id(project.project_id, user.user_id)
            if not space_id:
                raise MaintenanceRequestError(409, "You don't have anything in your own memory here yet.")
            return project, space_id
        if not project.shared_space_id:
            raise MaintenanceRequestError(409, "This project has no shared memory yet.")
        return project, project.shared_space_id

    def start(
        self,
        project_id: str,
        user: User,
        model: MaintenanceModel,
        *,
        slug: Optional[str] = None,
        scope: MaintenanceScope = "project",
    ):
        """Queue a run over every file, or one ``slug``. Returns the queued run."""
        from apis.shared.memory.maintenance.runner import run_ttl
        from apis.shared.memory.models import MaintenanceRun

        project, space_id = self._space(project_id, user, start=True, scope=scope)
        function_name = (os.environ.get(FUNCTION_NAME_ENV) or "").strip()
        if not function_name:
            raise MaintenanceRequestError(503, "Memory maintenance isn't set up in this deployment.")
        repo = self.memory.repository
        if slug is not None and not any(e.slug == slug for e in repo.get_index(space_id).entries):
            where = "your memory" if scope == "mine" else "project memory"
            raise MaintenanceRequestError(404, f"'{slug}' isn't in {where}.")
        space_scope = _SPACE_SCOPES[scope]

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
            scope=space_scope,
        )
        if not repo.acquire_maintenance_lock(
            space_id, run.run_id, now=int(now.timestamp()), lease_seconds=LOCK_LEASE_SECONDS
        ):
            where = "your memory" if scope == "mine" else "this project's memory"
            raise MaintenanceRequestError(409, f"Maintenance is already running on {where}.")
        ttl = run_ttl(space_scope, now=now)
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
        if scope == "project":
            # A member's own memory is theirs and isn't audited (2.8b).
            self._record(user, project, run)
        return run

    def list_runs(
        self, project_id: str, user: User, *, scope: MaintenanceScope = "project", limit: int = RUNS_LISTED
    ) -> List[Any]:
        """Recent runs on one scope, newest first (shared: owner and editors; mine: the member)."""
        _, space_id = self._space(project_id, user, start=False, scope=scope)
        return [self._as_seen(r) for r in self.memory.repository.list_maintenance_runs(space_id, limit)]

    def get_run(self, project_id: str, user: User, run_id: str, *, scope: MaintenanceScope = "project"):
        _, space_id = self._space(project_id, user, start=False, scope=scope)
        run = self.memory.repository.get_maintenance_run(space_id, run_id)
        if run is None:
            raise MaintenanceRequestError(404, "Not found")
        return self._as_seen(run)

    def undoable_until(self, run: Any) -> Optional[str]:
        """When an applied run stops being undoable, or None if it can't be undone at all."""
        from apis.shared.memory.service import archive_retention_days

        if run.space_scope != "personal_in_project" or run.state != "done" or run.undone_at:
            return None
        if not any(r.outcome == "applied" for r in run.results):
            return None
        try:
            created = datetime.fromisoformat(run.created_at)
        except ValueError:
            return None
        return (created + timedelta(days=archive_retention_days(run.space_scope))).isoformat()

    def undo(self, project_id: str, user: User, run_id: str):
        """Put back every file a run on the caller's own memory changed, as the rule above says.

        Takes the space's maintenance slot for the duration, so an undo never
        overlaps a run or another undo. Returns the run, with each applied
        file's ``undo`` outcome.
        """
        from apis.shared.memory.maintenance.runner import run_ttl

        project, space_id = self._space(project_id, user, start=True, scope="mine")
        repo = self.memory.repository
        run = repo.get_maintenance_run(space_id, run_id)
        if run is None or run.requested_by != user.user_id:
            raise MaintenanceRequestError(404, "Not found")
        if run.undone_at:
            raise MaintenanceRequestError(409, "This tidy-up was already undone.")
        until = self.undoable_until(run)
        if until is None:
            raise MaintenanceRequestError(409, "This run didn't change anything that can be undone.")
        now = self._clock()
        if now.isoformat() >= until:
            raise MaintenanceRequestError(409, "This tidy-up is too old to undo. Each file's History still has its earlier versions.")
        lock = f"undo-{run_id}"
        if not repo.acquire_maintenance_lock(space_id, lock, now=int(now.timestamp()), lease_seconds=LOCK_LEASE_SECONDS):
            raise MaintenanceRequestError(409, "Maintenance is running on your memory. Undo once it has finished.")
        try:
            snapshot = {e.slug: e for e in (run.snapshot.entries if run.snapshot else [])}
            made_from: dict = {}
            for result in run.results:
                if result.outcome == "created" and result.split_from:
                    made_from.setdefault(result.split_from, []).append(result)
            for result in run.results:
                if result.outcome == "applied":
                    made = made_from.get(result.slug, [])
                    result.undo, result.undo_version = self._undo_file(
                        space_id, user, run, result, snapshot.get(result.slug), made
                    )
                    for new in made:
                        new.undo = self._undo_created(space_id, new, restored=result.undo == "restored")
            run.undone_at = now.isoformat()
            run.undone_by = (user.email or "").strip().lower()
            repo.put_maintenance_run(run, run_ttl(run.space_scope, now=datetime.fromisoformat(run.created_at)))
        finally:
            repo.release_maintenance_lock(space_id, lock)
        logger.info(
            "memory-maintenance: undo run=%s space=%s outcomes=%s",
            run_id, space_id, sorted((r.slug, r.undo) for r in run.results if r.undo),
        )
        return run

    def _undo_file(
        self, space_id: str, user: User, run: Any, result: Any, before: Any, made: Optional[List[Any]] = None
    ) -> Tuple[str, Optional[int]]:
        """``(outcome, version)`` for one applied file. ``made`` are the files its splits created."""
        from apis.shared.memory.format import parse_file
        from apis.shared.memory.models import ItemProvenance
        from apis.shared.memory.service import MemorySpaceConcurrencyError, MemorySpaceError, SaveContext
        from apis.shared.projects.memory_files import EditedItem, render_items_for_save

        memory = self.memory
        entries = {e.slug: e for e in memory.repository.get_index(space_id).entries}
        current = entries.get(result.slug)
        if current is None or before is None:
            return "missing", None
        if current.content_hash != result.content_hash:
            return "changed", None
        # A split comes undone whole, or not at all.
        if any(entries.get(new.slug) is None or entries[new.slug].content_hash != new.content_hash for new in made or []):
            return "changed", None
        try:
            parsed = parse_file(memory.store.get(before.s3_key).decode("utf-8"))
            present = {i.anchor for i in memory._current_file(current, result.slug).items}
            returning = [i.anchor for i in parsed.items if i.anchor and i.anchor not in present]
            moved_back = {}
            for new in made or []:
                for anchor, prov in memory.repository.get_provenance(space_id, new.slug).items():
                    if anchor in returning:
                        moved_back[anchor] = prov.model_copy(update={"moved_from": None, "moved_by": None, "moved_at": None})
            archived = {}
            for row in sorted(memory.repository.list_archived_items(space_id), key=lambda a: a.archive_id):
                if row.slug == result.slug and row.anchor in returning and row.anchor not in moved_back:
                    archived[row.anchor] = row
            restored = {
                anchor: (archived[anchor].provenance or ItemProvenance(added_by=archived[anchor].archived_by))
                if anchor in archived else ItemProvenance()
                for anchor in returning
                if anchor not in moved_back
            }
            # The file now is exactly what the run wrote, so an anchor the
            # snapshot lacks is one the run minted: a split's pointer.
            minted = present - {i.anchor for i in parsed.items if i.anchor}
            fm = parsed.frontmatter or {}
            description, aliases = fm.get("description"), fm.get("aliases")
            saved = memory.save_entry(
                space_id, user.user_id, user.email, result.slug,
                render_items_for_save([EditedItem(text=i.text, anchor=i.anchor) for i in parsed.items]),
                description=description if isinstance(description, str) else None,
                aliases=list(aliases) if isinstance(aliases, list) else None,
                reason="restore",
                run_id=run.run_id,
                base_content_hash=current.content_hash,
                restorable=returning,
                context=SaveContext(restored=restored, moved=moved_back, not_archived=minted),
            )
        except MemorySpaceConcurrencyError:
            return "changed", None
        except MemorySpaceError:
            logger.warning("memory-maintenance: could not undo %s of run %s", result.slug, run.run_id, exc_info=True)
            return "failed", None
        for row in archived.values():
            try:
                memory.repository.delete_archived_item(space_id, row.archive_id)
            except Exception:
                logger.warning("Could not clear archive row %s in %s", row.archive_id, space_id, exc_info=True)
        return "restored", saved.ref.version

    def _undo_created(self, space_id: str, new: Any, *, restored: bool) -> Optional[str]:
        """Take away a file a split made, once its items are back where they came from.

        When the file they came from wasn't put back, this one stays too: ``changed``
        or ``missing`` if that is why, and no outcome if it is untouched.
        """
        from apis.shared.memory.service import MemorySpaceConcurrencyError, MemorySpaceNotFoundError

        if not restored:
            current = next((e for e in self.memory.repository.get_index(space_id).entries if e.slug == new.slug), None)
            if current is None:
                return "missing"
            return "changed" if current.content_hash != new.content_hash else None
        try:
            self.memory.discard_file(space_id, new.slug, delete_objects=True, expected_hash=new.content_hash)
        except MemorySpaceNotFoundError:
            return "missing"
        except MemorySpaceConcurrencyError:
            return "changed"
        except Exception:
            logger.warning("memory-maintenance: could not remove %s after an undo", new.slug, exc_info=True)
            return "failed"
        return "removed"

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
