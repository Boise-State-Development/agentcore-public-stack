"""A workflow run step's earlier results, as the step reads them (workflow-runs §6.1, workflow-designer §21.3).

A saved workflow's step may read other steps' results (PR-D.5a). Two places build that block, and
both must build the same bytes:

- app-api's ``POST …/workflow-runs/{runId}/steps/{stepId}/prepare``, for an Agent step. Its
  message is sent as the step's own first text.
- The Runtime's ``workflow_prompt`` invocation (PR-D.6b), for a prompt step. Its block goes into the
  request after the documents' cachePoint and before the instruction.

The Runtime may not import app-api, so the renderer lives here, in ``apis.shared``.

**What a read step contributes.** Each step it reads becomes a run result's part:

- **An Agent step:** its answer as it stood when it first finished, bounded at
  ``SEND_BACK_MAX_TOKENS``.
- **A prompt step:** its row's ``outputText``.
- **A checklist step** (workflow-designer §22.3): its row's ``outputText`` too, the values and
  states the Runtime wrote, one table per field set.
- **A step that stopped:** named by its status note.

Each part is checked by ``prepare_delivery`` against the receiving conversation, as a run result is,
and wrapped by ``render_delivery_parts``. **Only text crosses**: an Agent step's artifacts and files
are references into its own conversation, which the reader could not open, so they are never looked
up. Nothing here writes, and nothing here is on a turn's path.

The helpers collect needs for its own parts (``part_status``, ``prompt_part``, ``finished_by``) are
here too, so both read a step's row by one rule.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from apis.shared.handoff.context import (
    CHECKLIST_STEP_FALLBACK_NAME,
    DeliveryPartBlock,
    prompt_part_names,
    render_delivery_parts,
)
from apis.shared.handoff.delivery import DeliveryPartStatus
from apis.shared.handoff.models import Handoff
from apis.shared.handoff.runs import WorkflowRun, WorkflowRunStep
from apis.shared.handoff.send_back import (
    DeliveryPartRequest,
    DeliveryRefused,
    answer_message,
    prepare_delivery,
    render_return_excerpt,
)
from apis.shared.handoff.store import HandoffStore, get_handoff_store

#: A step row's status as its part reads it. ``open`` depends on the run (see ``part_status``).
PART_STATUS: Dict[str, DeliveryPartStatus] = {
    "done": "done",
    "stopped": "stopped",
    "needs_input": "needs_input",
    "running": "running",
    "dismissed": "stopped",
    "abandoned": "stopped",
}

#: A read step has settled when its part reads one of these: it will give nothing more (§6.1).
SETTLED_PARTS = frozenset({"done", "stopped"})

#: How long after a step's ``done`` write its own answer may still be stamped. Memory may stamp the
#: turn's events when it flushes them, just after the status write; the user's next turn in that
#: conversation is human-paced, far later than this.
SETTLE_SLACK = timedelta(seconds=60)


class StepReadsNotSettled(Exception):
    """A step the reader reads hasn't settled (it isn't ``done`` or ``stopped``), so there is no block yet.

    ``waiting`` names each such step in step order: its worker's display name (an Agent's name, or a
    prompt step's model's), else its step name. app-api answers 409 with ``str(exc)``; the Runtime
    stops the prompt step rather than run it without what it reads.
    """

    def __init__(self, waiting: Sequence[str]) -> None:
        self.waiting = list(waiting)
        super().__init__(f"Waiting for {', '.join(self.waiting)} to finish")


@dataclass(frozen=True)
class StepReadPart:
    """One step a reader reads, as its block names it, in step order."""

    step_id: str
    step_name: Optional[str]
    agent_name: str
    status: DeliveryPartStatus


def part_status(run: WorkflowRun, row: Optional[Handoff]) -> DeliveryPartStatus:
    """What a kept step's part says.

    - A step that never started is still to come in an approved run (the tab's queue, or the
      Runtime for a prompt step), and will never start in a stopped one.
    - A ``done`` Agent step with no conversation never ran, so it reads ``stopped``. A prompt or
      checklist step has no conversation by design (§21.3, §22.3), so its ``done`` stands.
    """
    if row is None or row.run_id != run.run_id:
        return "stopped"
    effective = row.effective_status()
    if effective == "open":
        return "running" if run.status == "approved" else "stopped"
    if effective == "done" and not row.target_session_id and not row.is_model_step:
        return "stopped"
    return PART_STATUS.get(effective, "stopped")


def finished_by(updated_at: str) -> str:
    """The cutoff for "the answer as it stood when the step first finished" (workflow-runs §8)."""
    try:
        at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return updated_at
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (at + SETTLE_SLACK).isoformat()


def prompt_part(step: WorkflowRunStep, row: Optional[Handoff], status: DeliveryPartStatus) -> DeliveryPartRequest:
    """A prompt or checklist step's part (§21.3, §22.3): text alone, named by its model and its step.

    When the step is done, the text is its row's ``outputText``: a prompt step's answer, or a
    checklist step's values and states. It has no conversation, so nothing is read beyond the row
    already in hand: no answer lookup, no artifacts, no files, and no ``WFCHK#`` row (the quotes are
    display, never model input). The Runtime capped the text when it wrote it, and
    ``prepare_delivery`` caps it again, as it does every part. A row of another kind than the step's
    reads ``stopped``, with no text.
    """
    fallback = {"fallback": CHECKLIST_STEP_FALLBACK_NAME} if step.is_checklist else {}
    agent_name, from_title = prompt_part_names(
        step.agent_name or (row.agent_name if row else ""), step.step_name, **fallback
    )
    is_own_row = row is not None and row.is_model_step and row.kind == (step.kind or "agent")
    text = (row.output_text or "") if status == "done" and is_own_row else ""
    return DeliveryPartRequest(
        step_id=step.step_id,
        agent_name=agent_name,
        status=status if row is None or is_own_row else "stopped",
        text=text,
        prompt=True,
        from_title=from_title,
        model_kind="checklist" if step.is_checklist else "prompt",
    )


async def read_part(
    user_id: str, run: WorkflowRun, step: WorkflowRunStep, row: Optional[Handoff], status: DeliveryPartStatus
) -> DeliveryPartRequest:
    """A step as a reader's part: text only. A step that isn't ``done`` gives its status and no text.

    An Agent step's text is its answer as it stood when it first finished. It names no artifact or
    file, so neither is looked up, and the answer is the only read.
    """
    if step.is_model_step:
        return prompt_part(step, row, status if status == "done" else "stopped")
    if status != "done" or row is None:
        return DeliveryPartRequest(step_id=step.step_id, agent_name=step.agent_name, status="stopped")
    target = row.target_session_id or ""
    found = await answer_message(target, user_id, at_or_before=finished_by(row.updated_at))
    return DeliveryPartRequest(
        step_id=step.step_id,
        agent_name=step.agent_name,
        status="done",
        from_session_id=target,
        text=render_return_excerpt(found[0]) if found is not None else "",
    )


def steps_read_by(run: WorkflowRun, step: WorkflowRunStep) -> List[WorkflowRunStep]:
    """The run's steps that ``step`` reads, by name, in step order (removed ones included)."""
    names = set(step.reads or [])
    return [c for c in run.steps if c.step_name in names and c.step_id != step.step_id]


async def render_step_reads(
    user_id: str,
    session_id: str,
    run: WorkflowRun,
    step: WorkflowRunStep,
    *,
    store: Optional[HandoffStore] = None,
) -> Tuple[str, List[StepReadPart]]:
    """``(block, parts)``: the results ``step`` reads, rendered as a run result's part blocks.

    ``session_id`` is the run's source conversation, which the parts are checked against as a run
    result's receiver. ``block`` is ``render_delivery_parts`` of one part per read step, in step
    order. ``parts`` is each read step's status: ``done`` or ``stopped``, since a read step has
    always settled by then. A step that reads none gives ``("", [])`` and reads nothing.

    The caller checks that ``run`` is the caller's and approved or stopped, and that ``step`` is one
    of its kept steps. This function reads each read step's ``HANDOFF#`` row (one ``GetItem`` each,
    issued together), and the answer of each finished Agent step it reads.

    Raises:
        StepReadsNotSettled: a read step is still ``running``, ``needs_input``, or not started in an
            approved run. Nothing is rendered.
        DeliveryRefused: the receiving conversation fails a result's rules (not the caller's, a
            Shared Project task), or a finished Agent step's conversation does. A refusal for
            ``empty`` is never raised: nothing finished with text renders each part's status note.
    """
    reads = steps_read_by(run, step)
    if not reads:
        return "", []
    store = store or get_handoff_store()
    rows = await asyncio.gather(*(asyncio.to_thread(store.get, user_id, read.handoff_id) for read in reads))
    statuses: List[DeliveryPartStatus] = [
        "stopped" if read.removed else part_status(run, row) for read, row in zip(reads, rows)
    ]
    waiting = [
        read.agent_name or read.step_name or "a step" for read, s in zip(reads, statuses) if s not in SETTLED_PARTS
    ]
    if waiting:
        raise StepReadsNotSettled(waiting)

    parts = list(
        await asyncio.gather(*(read_part(user_id, run, read, row, s) for read, row, s in zip(reads, rows, statuses)))
    )
    block = await _render(user_id, session_id, run.run_id, parts)
    return block, [
        StepReadPart(step_id=read.step_id, step_name=read.step_name, agent_name=read.agent_name, status=part.status)
        for read, part in zip(reads, parts)
    ]


async def _render(user_id: str, session_id: str, run_id: str, parts: List[DeliveryPartRequest]) -> str:
    """The parts as the model reads a run result's, through the checks a result's parts pass."""
    try:
        row = await prepare_delivery(user_id=user_id, to_session_id=session_id, parts=parts, run_id=run_id)
    except DeliveryRefused as exc:
        if exc.reason != "empty":
            raise
        # Nothing finished with text: each part is its status note, which needs no sender.
        blocks = [
            DeliveryPartBlock(
                text="", agent_name=p.agent_name, from_title=p.from_title if p.prompt else "", status=p.status
            )
            for p in parts
        ]
        return render_delivery_parts(blocks)
    return render_delivery_parts(
        [
            DeliveryPartBlock(text=p.text, agent_name=p.agent_name, from_title=p.from_title, status=p.status)
            for p in row.parts
        ]
    )


__all__ = [
    "PART_STATUS",
    "SETTLED_PARTS",
    "SETTLE_SLACK",
    "StepReadPart",
    "StepReadsNotSettled",
    "finished_by",
    "part_status",
    "prompt_part",
    "read_part",
    "render_step_reads",
    "steps_read_by",
]
