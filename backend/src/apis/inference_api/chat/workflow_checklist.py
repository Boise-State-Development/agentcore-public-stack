"""A workflow run's checklist steps on the Runtime: one model call per field set, never a turn.

``docs/specs/workflow-designer.md`` §22.3. A checklist step rides the ``workflow_prompt``
invocation (``workflow_prompt.PromptStepRun``): the same checks, the same claim (``open → running``
on its ``HANDOFF#`` row, held to ``kind = checklist``), the same stop. What differs is the call:

    toolConfig: one constant ``record_checklist`` tool, strict, toolChoice auto (``extraction.checklist``)
    system:     one constant checklist frame
    messages:   [ user: documents…, cachePoint, earlier results (if read), instruction ]

The instruction is the set's field list, the step's own guidance and "call record_checklist once".
A step of N sets is N calls. Every call on one model shares one cached prefix (tool, frame,
documents), so the first call on each model is made alone and the rest read it; §22.1 measured 12
of 12 later sets reading it with this layout, against 0 of 12 with a per-set schema. A run's prompt
steps on the same model write a second entry, since their tool list (none) differs.

Each reply's values arrive as strings (an array of strings for a ``list`` field), so they are coerced
by type and validated with the set's own model (``checklist.read_answers``), then judged by
``source_provenance`` against the run's documents. A set that fails on its own (it is gone from the
skill, or the model twice recorded no answer) is that set's ``error`` row and a line in the answer; it never stops the other sets. Every
set's ``WFCHK#`` row is written **before** the step's ``done``, so a reader that sees ``done`` finds
them all. A step whose every set failed stops ``error``.

Each call writes a ``C#`` row on the source session with ``callKind: "workflow_checklist"`` and
``{runId, stepId, fieldSet}``; its prefix fingerprint is stateless, keyed on the model, the tool, the
frame and the documents (``workflow_prompt.prefix_key``), so a second set on one model reads as
``hit``, never as a missed read of a prompt step's entry.

Nothing here is on a turn's path, and nothing reaches a turn's ``toolConfig`` or system prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Sequence

from apis.shared.extraction import checklist as checklist_call
from apis.shared.extraction.batch import run_bounded
from apis.shared.extraction.field_sets import FieldSet
from apis.shared.extraction.rows import Row
from apis.shared.files.whole_document import WholeDocument
from apis.shared.handoff.runs import WorkflowRun

from apis.inference_api.chat.workflow_prompt import (
    MAX_OUTPUT_TOKENS,
    STEP_TIMEOUT_SECONDS,
    CallResult,
    ModelPlan,
    StepPlan,
    call_bedrock,
    document_tokens,
    prefix_key,
    step_content,
)

if TYPE_CHECKING:  # pragma: no cover
    import threading

    from apis.inference_api.chat.workflow_prompt import PromptStepRun

logger = logging.getLogger(__name__)

#: The ``callKind`` on each checklist call's cost row.
CALL_KIND = "workflow_checklist"

#: The tool list every checklist call sends, as Strands takes it.
TOOLS: List[Dict[str, Any]] = [checklist_call.TOOL_SPEC]
TOOL_CHOICE: Dict[str, Any] = {"auto": {}}

#: Characters a call sends besides the documents and its own tail, for a cancelled call's estimate.
_FIXED_CHARS = len(checklist_call.SYSTEM_PROMPT) + len(json.dumps(checklist_call.TOOL_SPEC))


async def call_bedrock_checklist(model: Any, messages: List[Dict[str, Any]], cancel: "threading.Event") -> CallResult:
    """One checklist call: the ``record_checklist`` tool, ``toolChoice`` auto, the checklist frame."""
    return await call_bedrock(
        model,
        messages,
        cancel,
        tool_specs=TOOLS,
        tool_choice=TOOL_CHOICE,
        system_prompt=checklist_call.SYSTEM_PROMPT,
    )


def build_bedrock_checklist_model(model_id: str) -> Any:
    """The dedicated strict model for one model's checklist calls, with the prompt step's limits."""
    return checklist_call.build_checklist_model(
        model_id, max_tokens=MAX_OUTPUT_TOKENS, read_timeout=int(STEP_TIMEOUT_SECONDS)
    )


async def read_run_field_sets(user: Any, run: WorkflowRun, names: Sequence[str]) -> Dict[str, Optional[FieldSet]]:
    """Each of ``names`` from the run's workflow skill, parsed; ``None`` for a set that is gone or unreadable.

    The skill is the run row's ``workflow.skillId``, the one create checked the sets against, and it
    must still be one the caller can use: their own, or an active skill ``resolve_accessible_skill_ids``
    reaches. The sets are read with D.7a's reader (``read_skill_field_sets``: the skill's manifest, one
    GET each, issued together). Never raises: a failure reads as every set missing.
    """
    found: Dict[str, Optional[FieldSet]] = {name: None for name in names}
    source = run.workflow
    if source is None or not source.skill_id or not names:
        return found
    try:
        from apis.shared.skills.access import resolve_accessible_skill_ids
        from apis.shared.skills.models import SkillStatus
        from apis.shared.skills.repository import get_skill_catalog_repository
        from apis.shared.skills.resource_store import get_skill_resource_store
        from apis.shared.workflows.field_set_refs import read_skill_field_sets

        skill = await get_skill_catalog_repository().get_skill(source.skill_id)
        if skill is None:
            return found
        if skill.owner_id != user.user_id:
            active = str(getattr(skill.status, "value", skill.status)) == SkillStatus.ACTIVE.value
            if not active or source.skill_id not in set(await resolve_accessible_skill_ids(user)):
                logger.info("workflow checklist: the run's skill is no longer the caller's to use")
                return found
        lookups = await read_skill_field_sets(skill.resources, list(names), read=get_skill_resource_store().get)
    except Exception:  # noqa: BLE001 - every set reads as missing; the step's answer says so
        logger.warning("workflow checklist: reading the run's field sets failed", exc_info=True)
        return found
    for name in names:
        lookup = lookups.get(name)
        found[name] = lookup.field_set if lookup is not None else None
    return found


async def read_provenance_units(user_id: str, session_id: str, upload_id: str) -> Any:
    """One run file as ``source_provenance`` reads it (``document_provenance_units``), or ``None``.

    ``None`` for a file with no text to search (a scanned PDF, a spreadsheet) or one that can't be
    read now: its quotes are then ``unverified``, which is the honest answer.
    """
    from apis.shared.files.document_read import document_provenance_units
    from apis.shared.files.source_provenance import document_units

    try:
        _meta, fmt, texts = await document_provenance_units(user_id, session_id, upload_id)
        if not any(texts):
            return None
        return await asyncio.to_thread(document_units, fmt, texts)
    except Exception:  # noqa: BLE001 - a document we can't search verifies nothing
        logger.info("workflow checklist: a run file has no text to verify quotes against", exc_info=True)
        return None


def _put_checklist_set(**kwargs: Any) -> bool:
    from apis.shared.handoff.checklist_rows import put_checklist_set

    return put_checklist_set(**kwargs)


@dataclass
class SetJob:
    """One field set of one claimed step."""

    plan: StepPlan
    index: int
    name: str
    field_set: Optional[FieldSet]

    @property
    def key(self) -> str:
        return f"{self.plan.step_id}:{self.name}"


@dataclass
class SetOutcome:
    """How one set ended: ``done`` with its rows, ``error`` with a code, or ``stopped`` with the run."""

    status: str
    error: Optional[str] = None
    rows: List[Row] = field(default_factory=list)
    output_tokens: int = 0


@dataclass
class StepTally:
    """A step's sets as they end; the step settles once the last one does."""

    plan: StepPlan
    jobs: List[SetJob]
    outcomes: Dict[int, SetOutcome] = field(default_factory=dict)


class ChecklistBranch:
    """The checklist steps of one ``workflow_prompt`` invocation, run on its ``PromptStepRun``."""

    def __init__(
        self,
        work: "PromptStepRun",
        run: WorkflowRun,
        *,
        call_checklist: Optional[Callable[..., Awaitable[CallResult]]] = None,
        build_checklist_model: Optional[Callable[[str], Any]] = None,
        load_field_sets: Optional[Callable[..., Awaitable[Dict[str, Optional[FieldSet]]]]] = None,
        provenance_units: Optional[Callable[..., Awaitable[Any]]] = None,
        put_checklist_set: Optional[Callable[..., bool]] = None,
    ) -> None:
        self.work = work
        self.run = run
        self._call = call_checklist or call_bedrock_checklist
        self._build_model = build_checklist_model or build_bedrock_checklist_model
        self._load = load_field_sets or read_run_field_sets
        self._units_for = provenance_units or read_provenance_units
        self._put = put_checklist_set or _put_checklist_set
        self.field_sets: Dict[str, Optional[FieldSet]] = {}
        self._units: Optional["asyncio.Future[List[Any]]"] = None

    async def load(self, plans: Sequence[StepPlan]) -> None:
        """Read every set the claimed checklist steps name, once, before any call."""
        names = list(dict.fromkeys(name for plan in plans for name in (plan.step.field_sets or [])))
        self.field_sets = await self._load(self.work.user, self.run, names)

    def _documents_units(self) -> "asyncio.Future[List[Any]]":
        """The run's documents as ``source_provenance`` reads them, read once for every set."""
        if self._units is None:
            user_id, session_id = self.work.user.user_id, self.work.session_id
            self._units = asyncio.ensure_future(
                asyncio.gather(*(self._units_for(user_id, session_id, file_id) for file_id in self.run.file_ids))
            )
        return self._units

    async def run_group(
        self,
        model_plan: ModelPlan,
        documents: Sequence[WholeDocument],
        prefix: List[Dict[str, Any]],
        plans: List[StepPlan],
        *,
        limit: int,
    ) -> None:
        """One model's checklist calls: the first alone writes the prefix, the rest read it."""
        work = self.work
        tallies: List[StepTally] = []
        for plan in plans:
            if not plan.reads_files:
                # The format refuses a checklist step that doesn't read files; there is nothing to fill in.
                logger.warning("workflow checklist: a checklist step reads no files")
                await work._stop(plan, "error")
                continue
            jobs = [
                SetJob(plan=plan, index=index, name=name, field_set=self.field_sets.get(name))
                for index, name in enumerate(plan.step.field_sets or [])
            ]
            tallies.append(StepTally(plan=plan, jobs=jobs))
        if not tallies:
            return

        # Sets that can't run end first, with no call: one that is gone from the skill, or no longer
        # parses. A step with none to run settles here.
        runnable: List[Any] = []
        for tally in tallies:
            for job in tally.jobs:
                if job.field_set is None:
                    await self._settle(tally, job, SetOutcome(status="error", error="field_set_missing"))
                else:
                    runnable.append((tally, job))
            if not tally.jobs:
                await self._finalize(tally)

        if not runnable:
            return
        model = self._build_model(model_plan.model_id)
        doc_tokens = document_tokens(documents, model_plan.model_id)
        warm: Dict[str, Any] = {
            "prefix": None,
            "key": prefix_key(
                model_plan.model_id, prefix, system=checklist_call.SYSTEM_PROMPT, tools=TOOLS
            ),
        }

        async def _one(tally: StepTally, job: SetJob) -> None:
            async with work._slots:
                outcome = await self._run_set(model, model_plan, prefix, doc_tokens, job, warm)
            await self._settle(tally, job, outcome)

        first_tally, first_job = runnable[0]
        await _one(first_tally, first_job)
        rest = runnable[1:]
        if not rest:
            return
        if work._stopped:
            for tally, job in rest:
                await self._settle(tally, job, SetOutcome(status="stopped"))
            return
        results = await run_bounded([(lambda t=t, j=j: _one(t, j)) for t, j in rest], limit=limit)
        for (tally, job), result in zip(rest, results):
            if isinstance(result, BaseException):
                logger.warning("workflow checklist: a set failed", exc_info=result)
                if job.index not in tally.outcomes:
                    await self._settle(tally, job, SetOutcome(status="error", error="model_error"))

    async def _run_set(
        self,
        model: Any,
        model_plan: ModelPlan,
        prefix: List[Dict[str, Any]],
        doc_tokens: int,
        job: SetJob,
        warm: Dict[str, Any],
    ) -> SetOutcome:
        work = self.work
        if work._stopped:
            return SetOutcome(status="stopped")
        plan, field_set = job.plan, job.field_set
        assert field_set is not None
        text = checklist_call.instruction(field_set, plan.step.request)
        content = step_content(
            prefix, plan.reads_block, text, cache_point=model_plan.cache_point, run_request=plan.run_request
        )
        messages = [{"role": "user", "content": content}]
        prompt_tokens = doc_tokens + math.ceil(
            (_FIXED_CHARS + len(plan.run_request) + len(plan.reads_block) + len(text)) / 4
        )
        output_tokens = 0
        tool_input: Optional[Dict[str, Any]] = None
        # A reply with no tool call gets one retry: the same request, which reads the cached prefix.
        for attempt in (1, 2):
            key = job.key if attempt == 1 else f"{job.key}:retry"
            call = await work._timed_call(model, messages, key, self._call)
            result = call.result
            await work._meter_result(
                model_plan,
                call,
                messages,
                prompt_tokens=prompt_tokens,
                warm=warm,
                key=warm["key"],
                attributes={"runId": work.run_id, "stepId": plan.step_id, "fieldSet": job.name},
                call_kind=CALL_KIND,
                tools=TOOLS,
                system=checklist_call.SYSTEM_PROMPT,
            )
            output_tokens += int((result.usage or {}).get("outputTokens") or 0)
            if result.stop_reason == "cancelled":
                if work._stopped and not call.timed_out:
                    return SetOutcome(status="stopped", output_tokens=output_tokens)
                return SetOutcome(status="error", error="timeout", output_tokens=output_tokens)
            if result.stop_reason in ("guardrail_intervened", "content_filtered", "error"):
                return SetOutcome(status="error", error="model_error", output_tokens=output_tokens)
            tool_input = checklist_call.tool_input_of(result.content)
            if tool_input is not None:
                break
            if result.stop_reason == "max_tokens":
                return SetOutcome(status="error", error="cut_off", output_tokens=output_tokens)
            if work._stopped:
                return SetOutcome(status="stopped", output_tokens=output_tokens)
        if tool_input is None:
            return SetOutcome(status="error", error="no_tool_call", output_tokens=output_tokens)

        answers = checklist_call.read_answers(field_set, tool_input)
        units = await self._documents_units()
        rows = checklist_call.verified_rows(field_set, answers, list(units))
        return SetOutcome(status="done", rows=rows, output_tokens=output_tokens)

    async def _settle(self, tally: StepTally, job: SetJob, outcome: SetOutcome) -> None:
        """Record one set's outcome, write its ``WFCHK#`` row, and settle the step after its last set."""
        if job.index in tally.outcomes:
            return
        if outcome.status in ("done", "error"):
            await self._write_row(tally.plan, job, outcome)
        tally.outcomes[job.index] = outcome
        if len(tally.outcomes) == len(tally.jobs):
            await self._finalize(tally)

    async def _write_row(self, plan: StepPlan, job: SetJob, outcome: SetOutcome) -> None:
        field_set = job.field_set
        try:
            written = await asyncio.to_thread(
                self._put,
                user_id=self.work.user.user_id,
                session_id=self.work.session_id,
                run_id=self.work.run_id,
                step_id=plan.step_id,
                field_set=job.name,
                set_index=job.index,
                field_set_version=field_set.version if field_set is not None else "",
                status=outcome.status,
                fields=checklist_call.row_fields(outcome.rows) if outcome.status == "done" else [],
                model_id=plan.step.model_id or "",
                field_set_title=field_set.title if field_set is not None else "",
                error=outcome.error,
            )
        except Exception:  # noqa: BLE001 - the display row is best-effort; the answer carries the values
            logger.warning("workflow checklist: writing a set's row failed", exc_info=True)
            written = False
        if not written:
            logger.warning("workflow checklist: a set's row was not written")

    async def _finalize(self, tally: StepTally) -> None:
        """Every set has ended: ``done`` with the answer, or stopped."""
        work, plan = self.work, tally.plan
        outcomes = [tally.outcomes[job.index] for job in tally.jobs]
        if work._stopped or any(o.status == "stopped" for o in outcomes):
            await work._stop(plan, "user_stopped")
            return
        if all(o.status == "error" for o in outcomes):
            # Every set failed on its own, so there is no answer to keep. A step whose calls all
            # ran out of time says so; anything else is an error.
            reason = "timeout" if outcomes and all(o.error == "timeout" for o in outcomes) else "error"
            await work._stop(plan, reason)
            return
        results = [
            checklist_call.SetResult(
                name=job.name,
                title=job.field_set.title if job.field_set is not None else job.name,
                status=outcome.status,
                error=outcome.error,
                field_set=job.field_set,
                rows=tuple(outcome.rows),
            )
            for job, outcome in zip(tally.jobs, outcomes)
        ]
        await work._finish(
            plan, checklist_call.render_answer(results), sum(o.output_tokens for o in outcomes)
        )


__all__ = [
    "CALL_KIND",
    "ChecklistBranch",
    "TOOLS",
    "build_bedrock_checklist_model",
    "call_bedrock_checklist",
    "read_provenance_units",
    "read_run_field_sets",
]
