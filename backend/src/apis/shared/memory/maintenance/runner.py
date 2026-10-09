"""One maintenance run over a project's memory (Shared Projects 2.6, §4.6).

app-api writes the run ``queued`` (who asked, which model, its prices) and
invokes the maintenance worker, which calls :meth:`MaintenanceRunner.run`:

1. **Claim.** Only a ``queued`` run proceeds, so Lambda's async retries and a
   duplicate invoke are no-ops.
2. **Snapshot** the manifest and the index's hash onto the run row *first*.
   Objects are content-addressed and every version row keeps its object, so
   the snapshot is enough to put the space back.
3. **Plan** each file with one model call, largest file first, a few at a
   time, stopping before the worker's deadline. A file at the soft size
   threshold is also offered splits (2.6c).
4. **Verify** each plan deterministically (``verify.py``); what fails is
   dropped and counted, never repaired.
5. **Propose or apply.** In a project's shared memory the surviving ops become
   one compaction proposal per file, in the name of the member who started
   the run: the project scope never changes without an editor's approval. In
   a member's own memory (``personal_in_project``, 2.6b) they are saved
   straight away, conditional on the file still being the one snapshotted,
   and the run row keeps what changed so the member can read it and undo it
   (``apis/shared/projects/memory_maintenance.py``). A split's new file is
   created with it and recorded as its own ``created`` result.
6. **Tell** the owner and editors once per run (shared memory only: in a
   member's own memory the requester is the only person affected, and the
   page that started the run shows the result), **meter** the model calls to
   the project's monthly rollup (the requester's share), and release the
   space's maintenance slot.

The worker holds no user session. The requester's access was checked when the
run was started; the worker re-checks that the project is still active and,
for a member's own memory, that the space is theirs and they are still a
member.
"""

from __future__ import annotations

import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from ..format import Item
from ..models import (
    ItemProvenance,
    MaintenanceFileResult,
    MaintenanceRun,
    MaintenanceSnapshot,
    PROJECT_SCOPES,
    MemoryEntryRef,
    MemorySpace,
)
from ..validation import build_name_table
from .ops import apply_ops
from .planner import FileSize, Plan, Planner, PlannerError
from .verify import verify_plan

logger = logging.getLogger(__name__)

# Planner calls in flight at once. Each is one Converse call on one file; four
# keeps a whole-space run well inside the worker's 15 minutes without leaning
# on the account's model quota.
_DEFAULT_CONCURRENCY = 4
_DEFAULT_MAX_FILES = 50
# A file is not started with less than this left on the worker's clock: a
# planner call takes up to two minutes before it times out.
_DEADLINE_MARGIN_SECONDS = 150
# Notification payloads name at most this many files.
_NOTIFY_SLUGS = 10
# An applied run keeps each file's changes for its summary, source texts
# included, until they total this many bytes; later files keep counts only.
# The run row is one DynamoDB item (400 KB), snapshot and all.
_SUMMARY_BUDGET_BYTES = 150_000


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


def max_files_per_run() -> int:
    return _env_int("MEMORY_MAINTENANCE_MAX_FILES", _DEFAULT_MAX_FILES)


def run_ttl(scope: str, *, now: Optional[datetime] = None) -> int:
    """A run row (and its snapshot) lives as long as an archived item from the same space."""
    from ..service import archive_retention_days

    moment = now or datetime.now(timezone.utc)
    return int((moment + timedelta(days=archive_retention_days(scope))).timestamp())


@dataclass(frozen=True)
class _FileWork:
    """One file as read for planning: the snapshot's ref, its items and their provenance."""

    ref: MemoryEntryRef
    items: Tuple[Item, ...]
    provenance: Dict[str, ItemProvenance]
    # Set for a file at the soft size threshold: it is offered splits.
    size: Optional[FileSize] = None


class MaintenanceError(RuntimeError):
    """The run can't proceed at all. Its message is shown to the member who started it."""


class MaintenanceRunner:
    def __init__(
        self,
        memory: Any = None,
        projects: Any = None,
        notifications: Any = None,
        planner_factory: Optional[Callable[[str], Planner]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        concurrency: Optional[int] = None,
    ) -> None:
        self._memory = memory
        self._projects = projects
        self._notifications = notifications
        self._planner_factory = planner_factory or (lambda model_id: Planner(model_id))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._concurrency = concurrency or _env_int("MEMORY_MAINTENANCE_CONCURRENCY", _DEFAULT_CONCURRENCY)

    @property
    def memory(self):
        if self._memory is None:
            from ..service import MemorySpaceService

            self._memory = MemorySpaceService()
        return self._memory

    @property
    def projects(self):
        if self._projects is None:
            from apis.shared.projects.repository import ProjectRepository

            self._projects = ProjectRepository()
        return self._projects

    @property
    def notifications(self):
        if self._notifications is None:
            from apis.shared.notifications.service import NotificationService

            self._notifications = NotificationService()
        return self._notifications

    def _now(self) -> str:
        return self._clock().isoformat()

    # ── the run ────────────────────────────────────────────────────────

    def run(
        self,
        space_id: str,
        run_id: str,
        *,
        remaining_seconds: Callable[[], float] = lambda: math.inf,
    ) -> Optional[MaintenanceRun]:
        repo = self.memory.repository
        run = repo.get_maintenance_run(space_id, run_id)
        if run is None:
            logger.warning("memory-maintenance: run %s not found in space %s", run_id, space_id)
            return None
        if run.state != "queued":
            logger.info("memory-maintenance: run %s is already %s; nothing to do", run_id, run.state)
            return run
        space = repo.get_space(space_id)
        ttl = run_ttl(space.scope if space is not None else "shared", now=self._clock())
        run.state = "running"
        run.started_at = self._now()
        repo.put_maintenance_run(run, ttl)
        started = time.monotonic()
        try:
            self._execute(run, space, ttl, remaining_seconds)
            run.state = "done"
        except MaintenanceError as exc:
            run.state, run.error = "failed", str(exc)
        except Exception:  # noqa: BLE001 - the run row must always end; the log has the trace
            logger.exception("memory-maintenance: run %s in space %s failed", run_id, space_id)
            run.state, run.error = "failed", "Maintenance stopped because of an unexpected error."
        finally:
            run.finished_at = self._now()
            self._meter(run)
            try:
                repo.put_maintenance_run(run, ttl)
            finally:
                repo.release_maintenance_lock(space_id, run_id)
        counts = {o: sum(1 for r in run.results if r.outcome == o) for o in {r.outcome for r in run.results}}
        logger.info(
            "memory-maintenance: run=%s space=%s state=%s files=%s inputTokens=%d outputTokens=%d elapsedMs=%d",
            run_id, space_id, run.state, counts, run.input_tokens, run.output_tokens,
            int((time.monotonic() - started) * 1000),
        )
        return run

    def _execute(
        self, run: MaintenanceRun, space: Optional[MemorySpace], ttl: int, remaining_seconds: Callable[[], float]
    ) -> None:
        repo = self.memory.repository
        if space is None or space.scope not in PROJECT_SCOPES or space.file_format != "canonical":
            raise MaintenanceError("Maintenance works on a project's memory only.")
        personal = space.scope == "personal_in_project"
        if personal and space.user_id != run.requested_by:
            raise MaintenanceError("Only the member whose memory this is can tidy it up.")
        project = self.projects.get_project(space.project_id) if space.project_id else None
        if project is None:
            raise MaintenanceError("The project is gone.")
        if project.status != "active":
            raise MaintenanceError("The project is archived, so its memory is read-only.")
        if personal and not self._still_member(project, run):
            raise MaintenanceError("You're no longer a member of this project.")

        index = repo.get_index(run.space_id)
        run.snapshot = MaintenanceSnapshot(
            manifest_version=index.version,
            index_content_hash=space.index_content_hash,
            entries=index.entries,
            taken_at=self._now(),
        )
        repo.put_maintenance_run(run, ttl)

        candidates = [e for e in index.entries if not e.archived]
        if run.slug:
            candidates = [e for e in candidates if e.slug == run.slug]
            if not candidates:
                where = "your memory" if personal else "project memory"
                raise MaintenanceError(f"'{run.slug}' isn't in {where} anymore.")
        candidates.sort(key=lambda e: (-(e.tokens or 0), e.slug))
        candidates = candidates[: max_files_per_run()]
        # A member's own memory has no review queue, so nothing can be waiting.
        waiting = set() if personal else {
            p.slug for p in repo.list_proposals(run.space_id) if p.state == "pending" and p.kind == "compaction"
        }
        planner = self._planner_factory(run.model_id)
        today = self._clock().date()
        from ..service import file_hard_cap_tokens, file_soft_threshold_tokens

        cap, soft = file_hard_cap_tokens(), file_soft_threshold_tokens()

        # Three phases, because boto3 resources (the repositories) aren't
        # thread-safe: read every file here, run only the model calls in the
        # pool, then verify and propose here again.
        results: Dict[str, MaintenanceFileResult] = {}
        work: List[_FileWork] = []
        for ref in candidates:
            if ref.slug in waiting:
                results[ref.slug] = MaintenanceFileResult(slug=ref.slug, outcome="pending_review")
                continue
            try:
                items = self.memory._current_file(ref, ref.slug).items
                if len(items) < 2:
                    results[ref.slug] = MaintenanceFileResult(slug=ref.slug, outcome="nothing_to_do")
                    continue
                size = FileSize(tokens=ref.tokens, cap=cap) if (ref.tokens or 0) >= soft else None
                work.append(_FileWork(ref, items, repo.get_provenance(run.space_id, ref.slug), size))
            except Exception:  # noqa: BLE001 - one unreadable file never stops the others
                logger.exception("memory-maintenance: run=%s could not read %s", run.run_id, ref.slug)
                results[ref.slug] = MaintenanceFileResult(slug=ref.slug, outcome="failed", error="This file couldn't be read.")

        def plan(w: _FileWork) -> Union[Plan, PlannerError, None]:
            if remaining_seconds() < _DEADLINE_MARGIN_SECONDS:
                return None
            try:
                return planner.plan(
                    w.ref.slug, w.ref.description, w.items, pinned=w.ref.pinned, provenance=w.provenance,
                    today=today, size=w.size,
                )
            except PlannerError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            plans = list(pool.map(plan, work))
        # Names a split may not take: every file's name and alias, plus each
        # new file an earlier file's split in this run claimed.
        claimed: Set[str] = set(build_name_table(index.entries))
        created: List[MaintenanceFileResult] = []
        for w, outcome in zip(work, plans):
            results[w.ref.slug] = self._settle(
                run, space, w, outcome, today, apply=personal, files=index.entries, claimed=claimed, created=created
            )
        run.results = [results[ref.slug] for ref in candidates] + created
        _bound_summaries(run.results)
        if not personal:
            self._announce(run, project)

    def _still_member(self, project: Any, run: MaintenanceRun) -> bool:
        from apis.shared.projects.access import resolve_project_role

        _, role = resolve_project_role(
            project.project_id, run.requested_by, run.requested_by_email, repository=self.projects
        )
        return role is not None

    def _settle(
        self,
        run: MaintenanceRun,
        space: MemorySpace,
        w: "_FileWork",
        outcome: Union[Plan, PlannerError, None],
        today,
        *,
        apply: bool,
        files: List[MemoryEntryRef],
        claimed: Set[str],
        created: List[MaintenanceFileResult],
    ) -> MaintenanceFileResult:
        """Verify one file's plan and, if anything survives, propose it or (``apply``) save it.

        A split whose name another file's split in this run took first is
        skipped; the new files a saved split made go on ``created``.
        """
        from ..service import MemoryProposalStateError, MemorySpaceConcurrencyError, MemoryValidationError

        slug = w.ref.slug
        if outcome is None:
            return MaintenanceFileResult(slug=slug, outcome="not_reached")
        self._add_usage(run, outcome.input_tokens, outcome.output_tokens)
        if isinstance(outcome, PlannerError):
            return MaintenanceFileResult(slug=slug, outcome="failed", error=str(outcome))
        try:
            ops, verification = verify_plan(
                outcome.changes, w.items, pinned=w.ref.pinned, provenance=w.provenance, today=today,
                files=files, allow_split=w.size is not None,
            )
            counts = {"planned": verification.planned, "kept": verification.kept, "dropped": len(verification.dropped)}
            if verification.dropped:
                logger.info(
                    "memory-maintenance: run=%s slug=%s dropped=%s",
                    run.run_id, slug, sorted(d.code for d in verification.dropped),
                )
            if not ops:
                return MaintenanceFileResult(slug=slug, outcome="nothing_to_do", **counts)
            result = apply_ops(w.items, ops, pinned=w.ref.pinned, taken=claimed)
            if not result.applied:
                return MaintenanceFileResult(slug=slug, outcome="nothing_to_do", **counts)
            if apply:
                from ..hydration import MINE_MEMORY_MAX_TOKENS

                saved = self.memory.apply_maintenance(
                    space.space_id,
                    user_id=run.requested_by,
                    user_email=run.requested_by_email,
                    slug=slug,
                    result=result,
                    run_id=run.run_id,
                    base_content_hash=w.ref.content_hash,
                    index_budget=MINE_MEMORY_MAX_TOKENS,
                )
                for ref in saved.created:
                    claimed.add(ref.slug.casefold())
                    created.append(MaintenanceFileResult(
                        slug=ref.slug, outcome="created", version=ref.version, content_hash=ref.content_hash,
                        split_from=slug,
                    ))
                return MaintenanceFileResult(
                    slug=slug, outcome="applied", version=saved.ref.version, content_hash=saved.ref.content_hash,
                    ops=[ops[i] for i in result.applied], **counts,
                )
            proposal = self.memory.create_compaction_proposal(
                space.space_id,
                requester_id=run.requested_by,
                requester_email=run.requested_by_email,
                slug=slug,
                ops=[ops[i] for i in result.applied],
                verification=verification,
                run_id=run.run_id,
                base=w.ref,
                items=result.items,
                new_files=result.new_files,
            )
            claimed.update(new.slug.casefold() for new in result.new_files)
            return MaintenanceFileResult(slug=slug, outcome="proposed", proposal_id=proposal.proposal_id, **counts)
        except MemoryProposalStateError as exc:
            return MaintenanceFileResult(slug=slug, outcome="pending_review", error=str(exc))
        except MemorySpaceConcurrencyError:
            return MaintenanceFileResult(
                slug=slug, outcome="changed", error="This file was saved while maintenance ran, so it was left as it is.",
            )
        except MemoryValidationError as exc:
            return MaintenanceFileResult(slug=slug, outcome="failed", error=f"The changes didn't pass the save checks: {exc}")
        except Exception:  # noqa: BLE001 - one file's failure never stops the others
            logger.exception("memory-maintenance: run=%s slug=%s failed", run.run_id, slug)
            return MaintenanceFileResult(slug=slug, outcome="failed", error="This file couldn't be maintained.")

    @staticmethod
    def _add_usage(run: MaintenanceRun, input_tokens: int, output_tokens: int) -> None:
        run.input_tokens += input_tokens
        run.output_tokens += output_tokens

    # ── after the run ──────────────────────────────────────────────────

    def _announce(self, run: MaintenanceRun, project: Any) -> None:
        """One notification per run to the owner and editors; the member who started it is skipped."""
        proposed = [r.slug for r in run.results if r.outcome == "proposed"]
        if not proposed:
            return
        from apis.shared.auth.models import User

        try:
            editors = [m.email for m in self.projects.list_members(project.project_id) if m.role == "editor"]
        except Exception:
            logger.warning("memory-maintenance: could not list editors of %s", project.project_id, exc_info=True)
            editors = []
        actor = User(
            email=run.requested_by_email, user_id=run.requested_by, name=run.requested_by_name or "", roles=[]
        )
        self.notifications.notify_many(
            [project.owner_email, *editors],
            kind="project_memory_maintenance",
            actor=actor,
            project_id=project.project_id,
            project_name=project.name,
            payload={"runId": run.run_id, "fileCount": len(proposed), "slugs": proposed[:_NOTIFY_SLUGS]},
        )

    def _meter(self, run: MaintenanceRun) -> None:
        """Add the run's model calls to the project's month, as the requester's share.

        The model and its prices were read from the catalog when the run was
        started, so a run is never unpriced. Best-effort, like every other
        cost rollup: a failed write costs the record, not the run.
        """
        if not (run.input_tokens or run.output_tokens) or not run.project_id:
            return
        if run.input_price_per_million_tokens is None or run.output_price_per_million_tokens is None:
            logger.warning("memory-maintenance: run %s has no prices; its model calls are unmetered", run.run_id)
            return
        cost = (
            Decimal(run.input_tokens) * Decimal(str(run.input_price_per_million_tokens))
            + Decimal(run.output_tokens) * Decimal(str(run.output_price_per_million_tokens))
        ) / Decimal(1_000_000)
        run.cost = float(cost)
        try:
            self.projects.add_call_cost(
                run.project_id,
                run.requested_by,
                self._clock().strftime("%Y-%m"),
                cost,
                run.input_tokens,
                run.output_tokens,
                self._now(),
            )
        except Exception:
            logger.warning("memory-maintenance: could not add run %s to the project's costs", run.run_id, exc_info=True)


def _bound_summaries(results: List[MaintenanceFileResult]) -> None:
    """Keep applied files' change lists until they reach the run row's budget; counts after that."""
    used = 0
    for result in results:
        if not result.ops:
            continue
        size = sum(len(op.model_dump_json(by_alias=True, exclude_none=True)) for op in result.ops)
        if used + size > _SUMMARY_BUDGET_BYTES:
            result.ops, result.ops_omitted = None, True
            continue
        used += size


def remaining_from_lambda(context: Any) -> Callable[[], float]:
    """Seconds left on a Lambda invocation, or forever outside one."""
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if getter is None:
        return lambda: math.inf
    return lambda: getter() / 1000.0

