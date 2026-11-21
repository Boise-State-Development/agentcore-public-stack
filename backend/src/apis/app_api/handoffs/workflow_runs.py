"""Workflow-run endpoints (``docs/specs/workflow-runs.md`` §9, §19 PR-W.1).

    POST /sessions/{session_id}/workflow-runs                    open a run of 2–8 Agents for review
    GET  /sessions/{session_id}/workflow-runs/{run_id}           read one, with its steps' rows
    POST /sessions/{session_id}/workflow-runs/{run_id}/approve   fix the envelope, once
    POST /sessions/{session_id}/workflow-runs/{run_id}/stop      no queued step starts after this
    POST /sessions/{session_id}/workflow-runs/{run_id}/dismiss   cancel the sheet
    POST /sessions/{session_id}/workflow-runs/{run_id}/collect   the run's one result, once (PR-W.2)
    POST /sessions/{session_id}/workflow-runs/{run_id}/steps/{step_id}/prepare
                                                                 a later stage's first message (PR-D.5a)

All 404 to a signed-in user while ``WORKFLOW_RUNS_ENABLED`` (or ``HANDOFFS_ENABLED``) is off, and
401 unauthenticated regardless: the flag is checked after authentication.

A step is an ordinary ``mention`` handoff row with a ``runId`` and ``stepId``, written by the same
helper the single mention create uses, so the target's first turn accepts it through the unchanged
``HandoffTurn``. Creating a run sends nothing and creates no session. In step 1 the SPA starts the
approved steps itself, up to ``limits.concurrency`` at once, with the call it already makes for one
handoff. Nothing here runs on a turn. The run's result is one ``DELIVERY#`` row with a part per
kept step (``collect``), made by ``create_delivery``'s own checks and attached by the unchanged
``DeliveryTurn`` on the source's next send.

**Stages (PR-D.5a).** A saved workflow's step may read other steps' results. The run row keeps each
step's ``reads``; the tab's queue starts such a step only once everything it reads has settled
(done or stopped), and just before its send asks ``…/prepare`` for its first message: the read
steps' results as the same ``<handoff_context>`` part blocks a run result carries, then its
request. That message is sent as the step's own text, so the Runtime and the turn path are
unchanged, and nothing is rendered on a turn.

**Prompt steps (workflow-designer §21.3, PR-D.6a).** A saved workflow's step may be one model call
with no Agent and no conversation. Its row is a ``HANDOFF#`` row of kind ``prompt`` (never a
mention, so no turn can accept it), created here and approved like any step; the Runtime's
``workflow_prompt`` invocation claims and finishes it. Create refuses one while
``PROMPT_STEPS_ENABLED`` is off, and one on a model the starter can't run, as sheet blockers.
Its result is the row's ``outputText``: a text-only part, with no artifacts and no files, in
collect and in a later step's ``prepare`` alike. ``runSteps`` counts only the steps that have a
conversation, so the sidebar's count of a run's step conversations can still reach it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.feature_flags import workflow_runs_enabled, workflows_enabled
from apis.shared.files.linked_files import HANDOFF_MAX_FILES
from apis.shared.handoff import HandoffStore, HandoffStoreUnavailable, TurnsCovered, get_handoff_store
from apis.shared.handoff.context import (
    build_handoff_message,
    context_hash,
)
from apis.shared.files.linked_files import DELIVERY_MAX_FILES
from apis.shared.handoff.delivery_turn import join_delivery_blocks
from apis.shared.handoff.delivery import (
    RUN_RESULT_MAX_FILES,
    DeliveryPartStatus,
    DeliveryStore,
    DeliveryView,
    get_delivery_store,
)
from apis.shared.handoff.metrics import emit_workflow_run_metric
from apis.shared.handoff.models import HANDOFF_REQUEST_MAX_CHARS, KIND_PROMPT_STEP, Handoff, HandoffView
from apis.shared.handoff.relay import load_relayed_prompts
from apis.shared.handoff.run_reads import (
    StepReadsNotSettled,
    finished_by,
    part_status,
    prompt_part,
    render_step_reads,
)
from apis.shared.handoff.runs import (
    WORKFLOW_RUN_MAX_STEPS,
    WORKFLOW_RUN_MIN_STEPS,
    WorkflowRun,
    WorkflowRunCard,
    WorkflowRunEdit,
    WorkflowRunSource,
    WorkflowRunStep,
    WorkflowRunStore,
    WorkflowRunView,
    get_workflow_run_store,
)
from apis.shared.handoff.send_back import (
    DeliveryPartRequest,
    DeliveryRefused,
    answer_message,
    offered_artifacts,
    offered_files,
    prepare_delivery,
    render_return_excerpt,
    write_delivery,
)
from apis.shared.handoff.turn import cap_context_text
from apis.shared.timestamps import utc_now_iso
from apis.shared.workflows.format import (
    FILES_NONE,
    FILES_ONE,
    INPUT_READS,
    KIND_PROMPT,
    RESULT_MERGE,
    Workflow,
    WorkflowError,
    WorkflowStep,
    require_workflow_name,
    workflow_fingerprint,
)
from apis.shared.workflows.sheet import (
    AgentView,
    ModelView,
    WorkflowNotFound,
    WorkflowUnreadable,
    agent_views,
    load_workflow,
    merge_instruction,
    model_views,
    sheet_for,
    step_model,
    without_steps,
)

from apis.app_api.handoffs import routes as handoff_routes

logger = logging.getLogger(__name__)


async def require_workflow_runs_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """The signed-in user, or 404 while the flag is off. Authentication runs first."""
    if not workflow_runs_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


router = APIRouter(prefix="/sessions", tags=["workflow-runs"])


# --- Create ---------------------------------------------------------------------------------------


class CreateRunStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", min_length=1, max_length=256)
    request: Optional[str] = Field(
        None, max_length=HANDOFF_REQUEST_MAX_CHARS, description="This step's own request; the shared one when absent"
    )


class CreateRunWorkflow(BaseModel):
    """A saved workflow to start (``workflow-designer.md`` §9.2, PR-D.2)."""

    model_config = ConfigDict(populate_by_name=True)

    skill_id: str = Field(..., alias="skillId", min_length=1, max_length=256)
    name: str = Field(..., min_length=1, max_length=128)
    removed_steps: List[str] = Field(
        default_factory=list,
        alias="removedSteps",
        max_length=WORKFLOW_RUN_MAX_STEPS,
        description="Steps the starter took out at the sheet before the run opened; every step that reads one goes too",
    )


class CreateWorkflowRunRequest(BaseModel):
    """Either the Agents a message mentioned (``steps``), or a saved ``workflow`` whose file names them."""

    model_config = ConfigDict(populate_by_name=True)

    request: str = Field(
        "",
        max_length=HANDOFF_REQUEST_MAX_CHARS,
        description="What the user typed, minus the mentions. With a workflow, overrides the file's request when not blank",
    )
    steps: List[CreateRunStep] = Field(default_factory=list, max_length=WORKFLOW_RUN_MAX_STEPS)
    workflow: Optional[CreateRunWorkflow] = None

    @model_validator(mode="after")
    def _one_source(self) -> "CreateWorkflowRunRequest":
        if self.workflow is not None and self.steps:
            raise ValueError("Send the steps or a workflow, not both")
        if self.workflow is None and len(self.steps) < WORKFLOW_RUN_MIN_STEPS:
            raise ValueError(f"A run needs at least {WORKFLOW_RUN_MIN_STEPS} steps")
        return self


class CreatedRunStep(BaseModel):
    """One step as the sheet shows it, with what it needs to price the step's first turn."""

    model_config = ConfigDict(populate_by_name=True)

    step_id: str = Field(..., alias="stepId")
    handoff_id: str = Field(..., alias="handoffId")
    kind: Optional[str] = Field(None, description="prompt for a prompt step (§21.3); absent for an Agent step")
    agent_id: str = Field(..., alias="agentId", description="Empty on a prompt step")
    agent_name: str = Field(..., alias="agentName", description="The Agent's name; a prompt step's model's name")
    request: str
    prompt: str = Field(
        ...,
        description=(
            "The full first message this step's Agent receives: contextBlock, then request. A prompt "
            "step gets no card, so its prompt is its instruction alone; the run's files go before it"
        ),
    )
    model_id: Optional[str] = Field(
        None,
        alias="modelId",
        description="The Agent's own model, or a prompt step's, after retirement redirects. None when the Agent names none: the step runs on the model the send names, as a single handoff does",
    )
    input_price_per_million_tokens: Optional[float] = Field(
        None,
        alias="inputPricePerMillionTokens",
        description="modelId's catalog input rate (USD per million tokens); None without a modelId or a catalog row",
    )


class CreateWorkflowRunResponse(BaseModel):
    """The run sheet: the run, the context card once (every step gets the same one), and each step."""

    model_config = ConfigDict(populate_by_name=True)

    run: WorkflowRunView
    excerpt: str = Field(..., description="The context card: a <conversation_excerpt> block, or empty")
    excerpt_hash: str = Field(..., alias="excerptHash")
    excerpt_tokens: int = Field(..., alias="excerptTokens", description="Estimated at 4 characters a token")
    turns_covered: Optional[TurnsCovered] = Field(None, alias="turnsCovered")
    context_block: str = Field(..., alias="contextBlock", description="The excerpt wrapped as each Agent receives it")
    source_message_index: int = Field(..., alias="sourceMessageIndex")
    source_title: str = Field(..., alias="sourceTitle")
    steps: List[CreatedRunStep]


@router.post(
    "/{session_id}/workflow-runs",
    response_model=CreateWorkflowRunResponse,
    response_model_by_alias=True,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow_run(
    session_id: str,
    body: CreateWorkflowRunRequest,
    user: User = Depends(require_workflow_runs_user),
    store: HandoffStore = Depends(get_handoff_store),
    runs: WorkflowRunStore = Depends(get_workflow_run_store),
) -> CreateWorkflowRunResponse:
    """Open a run of several Agents from this conversation, for the user to review once.

    Renders the context card **once** and writes one ``open`` ``mention`` row per step through the
    helper the single mention create uses, then the ``WFRUN#`` row. 422 for fewer than 2 or more
    than 8 steps, or the same Agent twice; 409 for a Shared Project task (deliveries' rule, with
    their error); 404 when the conversation isn't the caller's or an Agent isn't theirs to use.
    """
    if body.workflow is not None:
        plan = await _plan_from_workflow(body.workflow, body.request, user)
    else:
        plan = await _plan_from_mentions(body, user)

    source = await handoff_routes.get_session_metadata(session_id, user.user_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if handoff_routes._source_project_id(source):
        # Mentions are never offered in a Shared Project, and a result can't come back into one
        # (spec §19), so a run is refused there with the deliveries' own error.
        raise handoff_routes._refused(DeliveryRefused("project_task"))

    card = await handoff_routes._render_source_card(session_id, user)
    shared_request = plan.request
    run_id = str(uuid.uuid4())
    step_ids = [str(uuid.uuid4()) for _ in plan.steps]
    rows: List[Handoff] = list(
        await asyncio.gather(
            *(
                _open_prompt_step(
                    store,
                    user,
                    session_id,
                    source,
                    card,
                    step,
                    request=_step_request(step.request, shared_request),
                    run_id=run_id,
                    step_id=step_id,
                )
                if step.kind == KIND_PROMPT
                else handoff_routes._open_mention(
                    store,
                    user,
                    session_id,
                    source,
                    card,
                    agent_id=step.agent_id,
                    agent_name=step.agent_name,
                    request=_step_request(step.request, shared_request),
                    run_id=run_id,
                    step_id=step_id,
                )
                for step, step_id in zip(plan.steps, step_ids)
            )
        )
    )

    now = utc_now_iso()
    run = WorkflowRun(
        run_id=run_id,
        user_id=user.user_id,
        session_id=session_id,
        status="open",
        request=shared_request,
        steps=[
            WorkflowRunStep(
                step_id=row.step_id or "",
                handoff_id=row.handoff_id,
                kind="prompt" if row.is_prompt_step else None,
                agent_id=row.agent_id,
                agent_name=row.agent_name,
                model_id=row.model_id if row.is_prompt_step else None,
                request=row.request,
                step_name=step.step_name,
                agent_version=step.agent_version,
                reads=list(step.reads) or None,
            )
            for row, step in zip(rows, plan.steps)
        ],
        source_message_index=card.last_index,
        created_at=now,
        updated_at=now,
        workflow=plan.workflow,
    )
    try:
        await asyncio.to_thread(runs.create, run)
    except HandoffStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")

    source_kw = {"run_source": "workflow"} if plan.workflow is not None else {}
    emit_workflow_run_metric(
        "WorkflowRunCreated", session_id=session_id, run_id=run_id, step_count=len(rows), **source_kw
    )
    # Every Agent step gets the same card; a prompt step gets none, so the card is read off an Agent
    # step's row, or rendered for a run of prompt steps alone exactly as it would be for one.
    card_row = next((row for row in rows if not row.is_prompt_step), None) or rows[0].model_copy(
        update={"excerpt_hash": context_hash(card.excerpt.text)}
    )
    first = handoff_routes._card_response(card_row, card)
    return CreateWorkflowRunResponse(
        run=WorkflowRunView.from_row(run, {row.handoff_id: HandoffView.from_row(row) for row in rows}),
        excerpt=first.excerpt,
        excerpt_hash=first.excerpt_hash,
        excerpt_tokens=first.excerpt_tokens,
        turns_covered=first.turns_covered,
        context_block=first.context_block,
        source_message_index=first.source_message_index,
        source_title=first.source_title,
        steps=[
            CreatedRunStep(
                step_id=row.step_id or "",
                handoff_id=row.handoff_id,
                kind="prompt" if row.is_prompt_step else None,
                agent_id=row.agent_id,
                agent_name=row.agent_name,
                request=row.request,
                prompt=row.request if row.is_prompt_step else build_handoff_message(first.context_block, row.request),
                model_id=step.model_id,
                input_price_per_million_tokens=step.rate,
            )
            for row, step in zip(rows, plan.steps)
        ],
    )


@dataclass(frozen=True)
class _PlannedStep:
    """One step to open: its worker as the caller sees it, its own request, and how to price it.

    A prompt step (``kind`` ``prompt``) has no Agent: ``agent_id`` is empty, ``agent_name`` is its
    model's display name, and ``model_id`` / ``rate`` are that model's, from its catalog row.
    """

    agent_id: str
    agent_name: str
    request: Optional[str]
    model_id: Optional[str]
    rate: Optional[float]
    step_name: Optional[str] = None
    agent_version: Optional[int] = None
    #: The other steps this one reads, by name, in step order (PR-D.5a). Empty for a mention run.
    reads: tuple = ()
    kind: str = "agent"


async def _open_prompt_step(
    store: HandoffStore,
    user: User,
    session_id: str,
    source: Any,
    card: Any,
    step: _PlannedStep,
    *,
    request: str,
    run_id: str,
    step_id: str,
) -> Handoff:
    """Write one ``open`` prompt step row (workflow-designer §21.3): no Agent, no card, no target.

    It sits where the run's row sits in the thread, on the model its catalog row resolved to. Never
    a ``mention``, so no turn's ``handoff_context`` can accept it (``HandoffTurn`` refuses the kind).
    503 when the store isn't configured.
    """
    now = utc_now_iso()
    row = Handoff(
        handoff_id=str(uuid.uuid4()),
        user_id=user.user_id,
        session_id=session_id,
        kind=KIND_PROMPT_STEP,
        status="open",
        agent_id="",
        agent_name=step.agent_name,
        request=request.strip(),
        source_title=getattr(source, "title", None) or "",
        source_message_index=card.last_index,
        created_at=now,
        updated_at=now,
        run_id=run_id,
        step_id=step_id,
        model_id=step.model_id,
    )
    try:
        await asyncio.to_thread(store.create, row)
    except HandoffStoreUnavailable:
        logger.error("Handoff store is not configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")
    return row


@dataclass(frozen=True)
class _RunPlan:
    request: str
    steps: List[_PlannedStep]
    workflow: Optional[WorkflowRunSource] = None


async def _plan_from_mentions(body: CreateWorkflowRunRequest, user: User) -> _RunPlan:
    """Step 1's run: the mentioned Agents, each checked as the caller. 422 for an Agent twice; 404 for one they can't use."""
    agent_ids = [step.agent_id for step in body.steps]
    if len(set(agent_ids)) != len(agent_ids):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Each Agent can be a step only once")
    lookups = await asyncio.gather(
        *(
            handoff_routes.get_assistant_with_access_check(
                assistant_id=agent_id, user_id=user.user_id, user_email=user.email
            )
            for agent_id in agent_ids
        )
    )
    agents = [agent for agent, _permission in lookups]
    if any(agent is None for agent in agents):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    models = await asyncio.gather(*(step_model(agent) for agent in agents))
    return _RunPlan(
        request=body.request.strip(),
        steps=[
            _PlannedStep(
                agent_id=step.agent_id,
                agent_name=agent.name or "",
                request=step.request,
                model_id=model_id,
                rate=rate,
            )
            for step, agent, (model_id, rate) in zip(body.steps, agents, models)
        ],
    )


async def _plan_from_workflow(ref: CreateRunWorkflow, request: str, user: User) -> _RunPlan:
    """A saved workflow's run: the file's steps, read with the skill's access rules, as the caller.

    404 while ``WORKFLOWS_ENABLED`` is off, for a workflow the caller can't reach (another user's
    private one reads as absent), and for a bad name; 422 for a file that no longer parses, and
    with the sheet's own blockers while any applies (a step the caller can't run, the same Agent
    twice, fewer than two steps). ``removedSteps`` are taken out first, with every step that reads
    them. Each step keeps the steps it reads (PR-D.5a), and the run keeps the steps the result
    takes and, for a merged result, its mode and instruction (PR-D.5b).
    """
    if not workflows_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    try:
        name = require_workflow_name(ref.name)
        skill, workflow = await load_workflow(ref.skill_id, name, user)
    except (WorkflowError, WorkflowNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    except WorkflowUnreadable as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    trimmed, removed = without_steps(workflow, ref.removed_steps)
    agents, models = await asyncio.gather(agent_views(trimmed, user), model_views(trimmed, user))
    sheet = sheet_for(trimmed, agents, models)
    if sheet.blockers:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "workflow_blocked",
                "message": " ".join(f"{b.message}." for b in sheet.blockers),
                "blockers": [b.model_dump(by_alias=True, exclude_none=True) for b in sheet.blockers],
            },
        )
    shared = request.strip() or (workflow.request or "").strip()
    return _RunPlan(
        request=shared,
        steps=[_planned_step(step, agents, models) for step in trimmed.steps],
        workflow=WorkflowRunSource(
            skill_id=skill.skill_id,
            name=workflow.name,
            title=workflow.title,
            fingerprint=workflow_fingerprint(workflow),
            version=workflow.version,
            files=workflow.files,
            removed_steps=list(removed) or None,
            result_reads=list(trimmed.result.reads) if trimmed.result.reads is not None else None,
            **_result_merge(trimmed),
        ),
    )


def _planned_step(step: WorkflowStep, agents: Dict[str, AgentView], models: Dict[str, ModelView]) -> _PlannedStep:
    """A saved workflow's step as the run opens it: its Agent, or a prompt step's model, as the caller."""
    reads = tuple(read for read in step.reads if read not in INPUT_READS)
    if step.is_prompt:
        model = models[step.model_id or ""]
        return _PlannedStep(
            agent_id="",
            agent_name=model.name or model.model_id,
            request=step.request,
            model_id=model.model_id,
            rate=model.input_price_per_million_tokens,
            step_name=step.name,
            reads=reads,
            kind=KIND_PROMPT,
        )
    agent = agents[step.agent_id]
    return _PlannedStep(
        agent_id=step.agent_id,
        agent_name=agent.name or "",
        request=step.request,
        model_id=agent.model_id,
        rate=agent.input_price_per_million_tokens,
        step_name=step.name,
        agent_version=agent.published_version,
        reads=reads,
    )


def _result_merge(workflow: Workflow) -> Dict[str, str]:
    """``resultMode`` / ``resultInstruction`` for a merged result (PR-D.5b); nothing for a card.

    Written only when the result merges, so a card run's row reads exactly as before.
    """
    instruction = merge_instruction(workflow)
    if instruction is None:
        return {}
    return {"result_mode": RESULT_MERGE, "result_instruction": instruction}


def _step_request(own: Optional[str], shared: str) -> str:
    """A step's request: its own when it has one, else the shared one."""
    own = (own or "").strip()
    return own or shared


# --- Read -----------------------------------------------------------------------------------------


@router.get(
    "/{session_id}/workflow-runs/{run_id}",
    response_model=WorkflowRunView,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def get_workflow_run(
    session_id: str,
    run_id: str,
    user: User = Depends(require_workflow_runs_user),
    store: HandoffStore = Depends(get_handoff_store),
    runs: WorkflowRunStore = Depends(get_workflow_run_store),
) -> WorkflowRunView:
    """One of the caller's runs from this conversation, each step with its row's current view.

    A ``needs_input`` step carries ``relayedPrompts``, read from its target as ``GET …/handoffs/{id}``
    reads them.
    """
    run = await _owned_run(runs, user, session_id, run_id)
    return await _run_view(store, user, run, relay=True)


# --- Approve --------------------------------------------------------------------------------------


class ApproveRunStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    step_id: str = Field(..., alias="stepId", min_length=1, max_length=64)
    request: Optional[str] = Field(None, max_length=HANDOFF_REQUEST_MAX_CHARS, description="The step's edited request")
    removed: bool = False


#: Bounds the reviewed card's field, as ``handoff_context.text`` is bounded, well above the cap the
#: row stores it at, so an oversized body is a 422 rather than a multi-megabyte string.
_RUN_CARD_FIELD_MAX_CHARS = 200_000


class ApproveRunCard(BaseModel):
    """The card as the user reviewed it: the text every step is sent with, or removed (PR-W.4b)."""

    model_config = ConfigDict(populate_by_name=True)

    text: Optional[str] = Field(None, max_length=_RUN_CARD_FIELD_MAX_CHARS)
    removed: bool = False

    def stored(self) -> WorkflowRunCard:
        """As the row keeps it: capped as the Runtime caps a card; blank text reads as removed,
        exactly as a blank ``handoff_context.text`` does."""
        text = "" if self.removed else cap_context_text(self.text or "")
        return WorkflowRunCard(text=text) if text else WorkflowRunCard(removed=True)


class ApproveWorkflowRunRequest(BaseModel):
    """What the user approved on the sheet. A step not listed is kept as it was created."""

    model_config = ConfigDict(populate_by_name=True)

    request: Optional[str] = Field(None, max_length=HANDOFF_REQUEST_MAX_CHARS, description="The edited shared request")
    steps: List[ApproveRunStep] = Field(default_factory=list, max_length=WORKFLOW_RUN_MAX_STEPS)
    file_ids: List[str] = Field(default_factory=list, alias="fileIds", max_length=HANDOFF_MAX_FILES)
    files_unticked: int = Field(
        0, alias="filesUnticked", ge=0, le=1000, description="Files the sheet listed and the user left unticked"
    )
    card: Optional[ApproveRunCard] = Field(
        None,
        description="The card as reviewed, kept on the run so its unstarted steps can start from any browser",
    )


@router.post(
    "/{session_id}/workflow-runs/{run_id}/approve",
    response_model=WorkflowRunView,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def approve_workflow_run(
    session_id: str,
    run_id: str,
    body: ApproveWorkflowRunRequest,
    user: User = Depends(require_workflow_runs_user),
    store: HandoffStore = Depends(get_handoff_store),
    runs: WorkflowRunStore = Depends(get_workflow_run_store),
) -> WorkflowRunView:
    """Fix the envelope: the steps, their requests and the files, exactly once (spec §4).

    Removed steps' rows are dismissed; every kept step's row is marked approved (and given its
    edited request), so it never reads as ``abandoned`` while it waits on the tab's queue. At least
    one step must remain (422). 409 once the run was approved or dismissed, so a double click
    approves once. The SPA then starts the kept steps.
    """
    run = await _owned_run(runs, user, session_id, run_id)
    if run.status != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This run was already started or cancelled")

    by_id = {step.step_id: step for step in run.steps}
    changes: Dict[str, ApproveRunStep] = {}
    for change in body.steps:
        if change.step_id not in by_id or change.step_id in changes:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown or repeated step")
        changes[change.step_id] = change

    shared_request = body.request.strip() if body.request is not None else run.request
    edits: List[WorkflowRunEdit] = []
    approved: List[WorkflowRunStep] = []
    edited_requests: Dict[str, str] = {}
    for step in run.steps:
        change = changes.get(step.step_id)
        removed = bool(change and change.removed)
        request = step.request
        if change is not None and change.request is not None:
            request = _step_request(change.request, shared_request)
        elif body.request is not None and step.request == run.request:
            # A step still on the shared request follows the edited one.
            request = shared_request
        if request != step.request and not removed:
            edited_requests[step.step_id] = request
        approved.append(step.model_copy(update={"request": request, "removed": removed}))
    kept = [step for step in approved if not step.removed]
    if not kept:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Keep at least one Agent")
    _check_readers_kept(approved)
    if edited_requests or shared_request != run.request:
        edits.append("request")
    if len(kept) < len(approved):
        edits.append("step_removed")
    if body.files_unticked:
        edits.append("file_unticked")

    file_ids = list(dict.fromkeys(f for f in body.file_ids if f))
    _check_files_rule(run, file_ids)

    approved_at = utc_now_iso()
    updated = await asyncio.to_thread(
        runs.approve,
        user.user_id,
        run_id,
        request=shared_request,
        steps=approved,
        file_ids=file_ids,
        edits=edits,
        approved_at=approved_at,
        card=body.card.stored() if body.card is not None else None,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This run was already started or cancelled")

    # The steps with a conversation, which is what the sidebar counts a run's step conversations
    # against: a prompt step never has one (workflow-designer §21.3).
    conversations = sum(1 for step in kept if not step.is_prompt)
    await asyncio.gather(
        *(
            _settle_step(store, user, run_id, step, approved_at, edited_requests.get(step.step_id), conversations)
            for step in approved
        )
    )
    emit_workflow_run_metric("WorkflowRunApproved", session_id=session_id, run_id=run_id, step_count=len(kept))
    for edit in edits:
        emit_workflow_run_metric("WorkflowRunEdited", session_id=session_id, run_id=run_id, detail=edit)
    return await _run_view(store, user, updated, relay=False)


def _check_readers_kept(steps: List[WorkflowRunStep]) -> None:
    """422 when a kept step reads a removed one (PR-D.5a): it would wait on a result that never comes.

    The sheet removes every reader with the step it reads (``workflow-designer.md`` §20.1), so
    this only refuses a client that didn't. A step reads others only in a saved workflow's run.
    """
    removed = {step.step_name for step in steps if step.removed and step.step_name}
    for step in steps:
        if step.removed:
            continue
        missing = [name for name in step.reads or [] if name in removed]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Remove '{step.step_name}' too: it reads '{missing[0]}', which was removed",
            )


def _check_files_rule(run: WorkflowRun, file_ids: List[str]) -> None:
    """A saved workflow's ``files`` rule (§3.1): none, exactly one, or any. 422 when the sheet broke it.

    Read from the run row, as the file said when the run was created, so an edit to the workflow
    since never changes what an open sheet may approve. A run of mentions has no rule.
    """
    rule = run.workflow.files if run.workflow is not None else None
    if rule == FILES_NONE and file_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This workflow takes no files")
    if rule == FILES_ONE and len(file_ids) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This workflow takes exactly one file"
        )


async def _settle_step(
    store: HandoffStore,
    user: User,
    run_id: str,
    step: WorkflowRunStep,
    approved_at: str,
    request: Optional[str],
    run_steps: int,
) -> None:
    """Dismiss a removed step's row, or mark a kept one approved. A row that moved on is left alone.

    A kept row also gets ``runSteps``, the run's kept count of steps with a conversation (Agent
    steps; a prompt step has none), which approval fixes for good. A prompt step's row is settled
    the same way: it waits ``open`` for the Runtime to claim it.
    """
    if step.removed:
        moved = await asyncio.to_thread(store.transition, user.user_id, step.handoff_id, "dismissed")
    else:
        moved = await asyncio.to_thread(
            store.approve_run_step,
            user.user_id,
            step.handoff_id,
            run_id=run_id,
            approved_at=approved_at,
            request=request,
            run_steps=run_steps,
        )
    if moved is None:
        logger.warning("Workflow run: a step row was no longer open at approval")


# --- Stop and dismiss -----------------------------------------------------------------------------


@router.post("/{session_id}/workflow-runs/{run_id}/stop", status_code=status.HTTP_204_NO_CONTENT)
async def stop_workflow_run(
    session_id: str,
    run_id: str,
    user: User = Depends(require_workflow_runs_user),
    runs: WorkflowRunStore = Depends(get_workflow_run_store),
) -> Response:
    """Stop run: mark an approved run ``stopped``, so no queued step starts after it.

    Running steps are stopped by the SPA's existing cooperative cancel, each as its own turn; this
    only records that the queue is closed. Idempotent for a stopped run; 409 for one never approved.
    """
    run = await _owned_run(runs, user, session_id, run_id)
    if run.status == "stopped":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    updated = await asyncio.to_thread(runs.transition, user.user_id, run_id, "stopped")
    if updated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This run was never started")
    emit_workflow_run_metric("WorkflowRunStopped", session_id=session_id, run_id=run_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{session_id}/workflow-runs/{run_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_workflow_run(
    session_id: str,
    run_id: str,
    user: User = Depends(require_workflow_runs_user),
    store: HandoffStore = Depends(get_handoff_store),
    runs: WorkflowRunStore = Depends(get_workflow_run_store),
) -> Response:
    """Cancel the run's sheet: ``open`` → ``dismissed``, dismissing every open step row with it.

    Idempotent for a dismissed run. 409 once it was approved: Stop run is the way out then.
    """
    run = await _owned_run(runs, user, session_id, run_id)
    if run.status == "dismissed":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    updated = await asyncio.to_thread(runs.transition, user.user_id, run_id, "dismissed")
    if updated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This run was already started")
    await asyncio.gather(
        *(asyncio.to_thread(store.transition, user.user_id, step.handoff_id, "dismissed") for step in run.steps)
    )
    emit_workflow_run_metric("WorkflowRunCancelled", session_id=session_id, run_id=run_id, step_count=len(run.steps))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Collect the result ---------------------------------------------------------------------------


class CollectWorkflowRunResponse(BaseModel):
    """The run's one result: its ``DELIVERY#`` card, now waiting on the source's composer.

    ``created`` is false when an earlier collect made it; ``status`` is the card's own (``pending``
    until a send attaches it), so a second tab can tell a result already sent from one waiting.
    """

    model_config = ConfigDict(populate_by_name=True)

    delivery_id: str = Field(..., alias="deliveryId")
    created: bool
    status: str
    delivery: Optional[DeliveryView] = Field(None, description="The card; absent only if its row is gone")


@router.post(
    "/{session_id}/workflow-runs/{run_id}/collect",
    response_model=CollectWorkflowRunResponse,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def collect_workflow_run(
    session_id: str,
    run_id: str,
    user: User = Depends(require_workflow_runs_user),
    store: HandoffStore = Depends(get_handoff_store),
    runs: WorkflowRunStore = Depends(get_workflow_run_store),
    deliveries: DeliveryStore = Depends(get_delivery_store),
) -> CollectWorkflowRunResponse:
    """Bring back the run's results as one card on this conversation's composer, exactly once.

    One part per kept step the result takes, in step order: a saved workflow's Result may read
    only some steps (PR-D.5a), and a run of mentions takes them all. A finished step's part is its answer as it stood when the
    step first finished, through the send-back renderer, with the artifacts that answer made and
    the step conversation's own files, as Bring back result picks them (at most
    ``DELIVERY_MAX_FILES`` a part and ``RUN_RESULT_MAX_FILES`` a run). A step that stopped, still
    waits on the user, or is still running is a part with that status and no text.

    The result is fixed when it is collected: "Bring back *k* results now" while steps still run
    makes the run's one result then, and a step that finishes later comes back the ordinary way,
    with its own Bring back result. Called again, it returns the card already made. 409 for a run
    never approved; 422 when no step has anything to bring back yet.
    """
    run = await _owned_run(runs, user, session_id, run_id)
    if run.status not in ("approved", "stopped"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This run was never started")
    if run.result_delivery_id:
        return await _collected(deliveries, user, run.result_delivery_id, created=False)

    kept = _result_steps(run)
    rows = await asyncio.gather(*(asyncio.to_thread(store.get, user.user_id, step.handoff_id) for step in kept))
    parts = list(await asyncio.gather(*(_part_for(user, run, step, row) for step, row in zip(kept, rows))))
    parts = _within_run_file_cap(parts)

    delivery_id = str(uuid.uuid4())
    try:
        delivery = await prepare_delivery(
            user_id=user.user_id, to_session_id=session_id, parts=parts, run_id=run_id, delivery_id=delivery_id
        )
    except DeliveryRefused as exc:
        if exc.reason == "empty":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No Agent has a result to bring back yet"
            )
        raise handoff_routes._refused(exc)

    claimed = await asyncio.to_thread(runs.claim_result, user.user_id, run_id, delivery_id)
    if claimed is None:
        # Another collect won the slot between our read and our claim: hand back its card.
        current = await asyncio.to_thread(runs.get, user.user_id, run_id)
        if current is not None and current.result_delivery_id:
            return await _collected(deliveries, user, current.result_delivery_id, created=False)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This run was never started")
    try:
        await write_delivery(delivery, store=deliveries)
    except Exception:
        await asyncio.to_thread(runs.release_result, user.user_id, run_id, delivery_id)
        logger.warning("Workflow run: writing the result failed; the claim was released", exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")

    finished = sum(1 for part in delivery.parts if part.status == "done")
    emit_workflow_run_metric(
        "WorkflowRunCollected",
        session_id=session_id,
        run_id=run_id,
        step_count=finished,
        detail="complete" if finished == len(delivery.parts) else "partial",
    )
    return CollectWorkflowRunResponse(
        delivery_id=delivery_id, created=True, status=delivery.status, delivery=DeliveryView.from_row(delivery)
    )


def _result_steps(run: WorkflowRun) -> List[WorkflowRunStep]:
    """The kept steps the run's result takes, in step order (PR-D.5a).

    Every kept step, unless the saved workflow's Result names some (``resultReads``). If every step
    it names was removed at the sheet, the result takes every kept step rather than bring back
    nothing.
    """
    kept = run.kept_steps()
    reads = run.workflow.result_reads if run.workflow is not None else None
    if reads is None:
        return kept
    named = [step for step in kept if step.step_name in set(reads)]
    return named or kept


async def _part_for(user: User, run: WorkflowRun, step: WorkflowRunStep, row: Optional[Handoff]) -> DeliveryPartRequest:
    """One kept step as collect's part, before ``create_delivery``'s checks. Reads the step's answer only when done.

    With the artifacts that answer made and the step conversation's own files, as Bring back result
    picks them. A prompt step's part is its text alone (``run_reads.prompt_part``). A later step's
    reads go through ``run_reads.render_step_reads`` instead, which carries text only.
    """
    status_now = part_status(run, row)
    if step.is_prompt:
        return prompt_part(step, row, status_now)
    target = (row.target_session_id or "") if row is not None and row.run_id == run.run_id else ""
    if status_now != "done":
        return DeliveryPartRequest(
            step_id=step.step_id, agent_name=step.agent_name, status=status_now, from_session_id=target
        )
    assert row is not None
    finished_at = finished_by(row.updated_at)
    found, files = await asyncio.gather(
        answer_message(target, user.user_id, at_or_before=finished_at),
        asyncio.to_thread(offered_files, user.user_id, target, created_before=finished_at),
    )
    text, artifact_ids = "", []
    if found is not None:
        message, index = found
        text = render_return_excerpt(message)
        artifacts = await asyncio.to_thread(offered_artifacts, user.user_id, target, index)
        artifact_ids = [a.artifact_id for a in artifacts]
    return DeliveryPartRequest(
        step_id=step.step_id,
        agent_name=step.agent_name,
        status="done",
        from_session_id=target,
        text=text,
        artifact_ids=artifact_ids,
        file_ids=[f.upload_id for f in files[:DELIVERY_MAX_FILES]],
    )


#: Kept under its old name for the tests that pin the cutoff (workflow-runs §8).
_finished_by = finished_by


def _within_run_file_cap(parts: List[DeliveryPartRequest]) -> List[DeliveryPartRequest]:
    """Keep ``RUN_RESULT_MAX_FILES`` across the run, in step order, each part's newest first."""
    room = RUN_RESULT_MAX_FILES
    kept: List[DeliveryPartRequest] = []
    for part in parts:
        files = list(part.file_ids)[: max(room, 0)]
        room -= len(files)
        kept.append(part if len(files) == len(part.file_ids) else replace(part, file_ids=files))
    return kept


async def _collected(
    deliveries: DeliveryStore, user: User, delivery_id: str, *, created: bool
) -> CollectWorkflowRunResponse:
    try:
        row = await asyncio.to_thread(deliveries.get, user.user_id, delivery_id)
    except HandoffStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")
    return CollectWorkflowRunResponse(
        delivery_id=delivery_id,
        created=created,
        status=row.status if row is not None else "dismissed",
        delivery=DeliveryView.from_row(row) if row is not None else None,
    )


# --- Prepare a later stage's step -----------------------------------------------------------------


class PreparedPart(BaseModel):
    """One step a prepared step reads, as its block names it."""

    model_config = ConfigDict(populate_by_name=True)

    step_id: str = Field(..., alias="stepId")
    step_name: Optional[str] = Field(None, alias="stepName")
    agent_name: str = Field(..., alias="agentName")
    status: DeliveryPartStatus = Field(..., description="done or stopped: a read step has always settled here")


class PrepareRunStepResponse(BaseModel):
    """A step's first message once what it reads has settled (PR-D.5a).

    ``message`` is what the step's send carries as its text: ``readsBlock``, a blank line, then the
    step's request. The Runtime puts the run's card before it, as for every step, so the Agent
    reads the card, then the earlier results, then the request.
    """

    model_config = ConfigDict(populate_by_name=True)

    step_id: str = Field(..., alias="stepId")
    message: str
    reads_block: str = Field(..., alias="readsBlock", description="The read steps' results, one block each; empty when it reads none")
    parts: List[PreparedPart] = Field(default_factory=list, description="The steps it reads, in step order")


@router.post(
    "/{session_id}/workflow-runs/{run_id}/steps/{step_id}/prepare",
    response_model=PrepareRunStepResponse,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def prepare_workflow_run_step(
    session_id: str,
    run_id: str,
    step_id: str,
    user: User = Depends(require_workflow_runs_user),
    store: HandoffStore = Depends(get_handoff_store),
    runs: WorkflowRunStore = Depends(get_workflow_run_store),
) -> PrepareRunStepResponse:
    """The first message of a step that reads other steps, rendered before its send (PR-D.5a).

    Rendered by ``apis.shared.handoff.run_reads.render_step_reads``, which the Runtime also uses for
    a prompt step's reads, so both build the same bytes. Each step it reads becomes a part as
    ``…/collect`` makes one (its answer as it stood when it first finished, bounded at
    ``SEND_BACK_MAX_TOKENS``; a prompt step's ``outputText``), checked by
    ``prepare_delivery`` against this conversation as a run result is, and wrapped by
    ``render_delivery_parts``, so the Agent reads the same ``<handoff_context>`` blocks a run result
    carries. A step that stopped is named by its block, as the result card names it.

    Only the text crosses: the blocks name no artifact or file, because those are references into
    the read step's conversation that this step's conversation could not open.

    Writes nothing, so it is idempotent: the same run gives the same message until a read step
    settles differently. 404 for a run or step that isn't the caller's from this conversation; 409
    for a run never approved, a removed step, or while a step it reads hasn't settled yet.
    """
    run = await _owned_run(runs, user, session_id, run_id)
    if run.status not in ("approved", "stopped"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This run was never started")
    step = next((candidate for candidate in run.steps if candidate.step_id == step_id), None)
    if step is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found")
    if step.removed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This step was removed")

    try:
        block, parts = await render_step_reads(user.user_id, session_id, run, step, store=store)
    except StepReadsNotSettled as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except DeliveryRefused as exc:
        raise handoff_routes._refused(exc)
    if not parts:
        return PrepareRunStepResponse(step_id=step.step_id, message=step.request, reads_block="", parts=[])
    return PrepareRunStepResponse(
        step_id=step.step_id,
        message=join_delivery_blocks([block], step.request.strip()),
        reads_block=block,
        parts=[
            PreparedPart(step_id=p.step_id, step_name=p.step_name, agent_name=p.agent_name, status=p.status)
            for p in parts
        ],
    )


# --- Helpers --------------------------------------------------------------------------------------


async def _owned_run(runs: WorkflowRunStore, user: User, session_id: str, run_id: str) -> WorkflowRun:
    try:
        run = await asyncio.to_thread(runs.get, user.user_id, run_id)
    except HandoffStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")
    if run is None or run.session_id != session_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


async def _run_view(store: HandoffStore, user: User, run: WorkflowRun, *, relay: bool) -> WorkflowRunView:
    """The run with each step's row read now (one ``GetItem`` each, issued together)."""
    rows = await asyncio.gather(
        *(asyncio.to_thread(store.get, user.user_id, step.handoff_id) for step in run.steps)
    )
    views = {
        row.handoff_id: HandoffView.from_row(row)
        for row in rows
        if row is not None and row.run_id == run.run_id and row.session_id == run.session_id
    }
    if relay and views:
        await load_relayed_prompts(list(views.values()), user.user_id)
    return WorkflowRunView.from_row(run, views)


__all__ = ["require_workflow_runs_user", "router"]
