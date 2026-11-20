"""Workflow-run endpoints (``docs/specs/workflow-runs.md`` §9, §19 PR-W.1).

    POST /sessions/{session_id}/workflow-runs                    open a run of 2–8 Agents for review
    GET  /sessions/{session_id}/workflow-runs/{run_id}           read one, with its steps' rows
    POST /sessions/{session_id}/workflow-runs/{run_id}/approve   fix the envelope, once
    POST /sessions/{session_id}/workflow-runs/{run_id}/stop      no queued step starts after this
    POST /sessions/{session_id}/workflow-runs/{run_id}/dismiss   cancel the sheet

All 404 to a signed-in user while ``WORKFLOW_RUNS_ENABLED`` (or ``HANDOFFS_ENABLED``) is off, and
401 unauthenticated regardless: the flag is checked after authentication.

A step is an ordinary ``mention`` handoff row with a ``runId`` and ``stepId``, written by the same
helper the single mention create uses, so the target's first turn accepts it through the unchanged
``HandoffTurn``. Creating a run sends nothing and creates no session. In step 1 the SPA starts the
approved steps itself, up to ``limits.concurrency`` at once, with the call it already makes for one
handoff. Nothing here runs on a turn.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.feature_flags import workflow_runs_enabled
from apis.shared.files.linked_files import HANDOFF_MAX_FILES
from apis.shared.handoff import HandoffStore, HandoffStoreUnavailable, TurnsCovered, get_handoff_store
from apis.shared.handoff.context import build_handoff_message
from apis.shared.handoff.metrics import emit_workflow_run_metric
from apis.shared.handoff.models import HANDOFF_REQUEST_MAX_CHARS, Handoff, HandoffView
from apis.shared.handoff.relay import load_relayed_prompts
from apis.shared.handoff.runs import (
    WORKFLOW_RUN_MAX_STEPS,
    WORKFLOW_RUN_MIN_STEPS,
    WorkflowRun,
    WorkflowRunEdit,
    WorkflowRunStep,
    WorkflowRunStore,
    WorkflowRunView,
    get_workflow_run_store,
)
from apis.shared.handoff.send_back import DeliveryRefused
from apis.shared.models.retirement import resolve_effective_model
from apis.shared.timestamps import utc_now_iso

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


class CreateWorkflowRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    request: str = Field("", max_length=HANDOFF_REQUEST_MAX_CHARS, description="What the user typed, minus the mentions")
    steps: List[CreateRunStep] = Field(..., min_length=WORKFLOW_RUN_MIN_STEPS, max_length=WORKFLOW_RUN_MAX_STEPS)


class CreatedRunStep(BaseModel):
    """One step as the sheet shows it, with what it needs to price the step's first turn."""

    model_config = ConfigDict(populate_by_name=True)

    step_id: str = Field(..., alias="stepId")
    handoff_id: str = Field(..., alias="handoffId")
    agent_id: str = Field(..., alias="agentId")
    agent_name: str = Field(..., alias="agentName")
    request: str
    prompt: str = Field(..., description="The full first message this step's Agent receives: contextBlock, then request")
    model_id: Optional[str] = Field(
        None,
        alias="modelId",
        description="The Agent's own model, after retirement redirects. None when the Agent names none: the step runs on the model the send names, as a single handoff does",
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
    agent_ids = [step.agent_id for step in body.steps]
    if len(set(agent_ids)) != len(agent_ids):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Each Agent can be a step only once")

    source = await handoff_routes.get_session_metadata(session_id, user.user_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if handoff_routes._source_project_id(source):
        # Mentions are never offered in a Shared Project, and a result can't come back into one
        # (spec §19), so a run is refused there with the deliveries' own error.
        raise handoff_routes._refused(DeliveryRefused("project_task"))

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

    card, models = await asyncio.gather(
        handoff_routes._render_source_card(session_id, user),
        asyncio.gather(*(_step_model(agent) for agent in agents)),
    )
    shared_request = body.request.strip()
    run_id = str(uuid.uuid4())
    step_ids = [str(uuid.uuid4()) for _ in body.steps]
    rows: List[Handoff] = list(
        await asyncio.gather(
            *(
                handoff_routes._open_mention(
                    store,
                    user,
                    session_id,
                    source,
                    card,
                    agent_id=step.agent_id,
                    agent_name=agent.name or "",
                    request=_step_request(step.request, shared_request),
                    run_id=run_id,
                    step_id=step_id,
                )
                for step, agent, step_id in zip(body.steps, agents, step_ids)
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
                agent_id=row.agent_id,
                agent_name=row.agent_name,
                request=row.request,
            )
            for row in rows
        ],
        source_message_index=card.last_index,
        created_at=now,
        updated_at=now,
    )
    try:
        await asyncio.to_thread(runs.create, run)
    except HandoffStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")

    emit_workflow_run_metric("WorkflowRunCreated", session_id=session_id, run_id=run_id, step_count=len(rows))
    first = handoff_routes._card_response(rows[0], card)
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
                agent_id=row.agent_id,
                agent_name=row.agent_name,
                request=row.request,
                prompt=build_handoff_message(first.context_block, row.request),
                model_id=model_id,
                input_price_per_million_tokens=rate,
            )
            for row, (model_id, rate) in zip(rows, models)
        ],
    )


def _step_request(own: Optional[str], shared: str) -> str:
    """A step's request: its own when it has one, else the shared one."""
    own = (own or "").strip()
    return own or shared


async def _step_model(agent: Any) -> Tuple[Optional[str], Optional[float]]:
    """``(modelId, input rate)`` for a step's Agent, from its ``modelConfig`` and the catalog.

    The Agent's governed model wins over the request on the turn path, so it is what the step runs
    on; an Agent with none runs on the model the send names, which the SPA already knows. Never
    raises: an estimate that can't be priced shows less, and the run still opens.
    """
    settings = getattr(agent, "model_settings", None)
    model_id = getattr(settings, "model_id", None)
    if not model_id:
        return None, None
    try:
        effective = await resolve_effective_model(model_id)
    except Exception:  # noqa: BLE001 - the estimate is display only
        logger.warning("Workflow run: model lookup failed", exc_info=True)
        return model_id, None
    if effective is None:
        return model_id, None
    record = effective.record
    return effective.model_id, (float(record.input_price_per_million_tokens) if record is not None else None)


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


class ApproveWorkflowRunRequest(BaseModel):
    """What the user approved on the sheet. A step not listed is kept as it was created."""

    model_config = ConfigDict(populate_by_name=True)

    request: Optional[str] = Field(None, max_length=HANDOFF_REQUEST_MAX_CHARS, description="The edited shared request")
    steps: List[ApproveRunStep] = Field(default_factory=list, max_length=WORKFLOW_RUN_MAX_STEPS)
    file_ids: List[str] = Field(default_factory=list, alias="fileIds", max_length=HANDOFF_MAX_FILES)
    files_unticked: int = Field(
        0, alias="filesUnticked", ge=0, le=1000, description="Files the sheet listed and the user left unticked"
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
    if edited_requests or shared_request != run.request:
        edits.append("request")
    if len(kept) < len(approved):
        edits.append("step_removed")
    if body.files_unticked:
        edits.append("file_unticked")

    approved_at = utc_now_iso()
    updated = await asyncio.to_thread(
        runs.approve,
        user.user_id,
        run_id,
        request=shared_request,
        steps=approved,
        file_ids=list(dict.fromkeys(f for f in body.file_ids if f)),
        edits=edits,
        approved_at=approved_at,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This run was already started or cancelled")

    await asyncio.gather(
        *(_settle_step(store, user, run_id, step, approved_at, edited_requests.get(step.step_id)) for step in approved)
    )
    emit_workflow_run_metric("WorkflowRunApproved", session_id=session_id, run_id=run_id, step_count=len(kept))
    for edit in edits:
        emit_workflow_run_metric("WorkflowRunEdited", session_id=session_id, run_id=run_id, detail=edit)
    return await _run_view(store, user, updated, relay=False)


async def _settle_step(
    store: HandoffStore,
    user: User,
    run_id: str,
    step: WorkflowRunStep,
    approved_at: str,
    request: Optional[str],
) -> None:
    """Dismiss a removed step's row, or mark a kept one approved. A row that moved on is left alone."""
    if step.removed:
        moved = await asyncio.to_thread(store.transition, user.user_id, step.handoff_id, "dismissed")
    else:
        moved = await asyncio.to_thread(
            store.approve_run_step, user.user_id, step.handoff_id, run_id=run_id, approved_at=approved_at, request=request
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
