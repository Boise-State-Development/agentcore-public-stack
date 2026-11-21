"""Saved workflows (``docs/specs/workflow-designer.md`` §9.2, PR-D.1).

    GET    /workflows                     every workflow in a skill the caller owns or can use
    POST   /workflows                     a new carrying skill and its file; ``fromRun`` fills it from a run
    GET    /workflows/{skillId}/{name}    one, with each step's Agent as the caller sees it, and its sheet
    PUT    /workflows/{skillId}/{name}    write it; ``?validate=true`` writes nothing; a stale
                                          ``expectedVersion`` is 409 with the current document
    DELETE /workflows/{skillId}/{name}    the file, and its carrying skill when that held nothing else

A workflow is ``references/workflow.<name>.md`` in a skill (D1), so access is the skill's: anyone
who can use the skill can read and start it, and only its owner can write it. Each Agent step is
checked **as the caller**, as a mention is, so sharing a workflow never shares its Agents (§11).

All 404 to a signed-in user while ``WORKFLOWS_ENABLED`` is off, and 401 unauthenticated
regardless: each handler declares ``get_current_user_from_session`` and checks the flag after it.

Nothing here runs on a turn, and nothing changes a system prompt or ``toolConfig``: the carrying
skill is opt-in like every user skill (Skills v2 D6), so it is active on a turn only when the user
turns it on or an Agent or project binds it (§20.3, *Built*).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse

from apis.shared.assistants.service import get_assistant_with_access_check
from apis.shared.auth import User, get_current_user_from_session
from apis.shared.blocking_io import run_blocking
from apis.shared.feature_flags import workflows_enabled
from apis.shared.handoff import HandoffStoreUnavailable
from apis.shared.handoff.runs import WORKFLOW_RUN_MIN_STEPS, WorkflowRun, get_workflow_run_store
from apis.shared.skills.access import resolve_accessible_skill_ids
from apis.shared.skills.models import SkillDefinition, SkillResourceRef, SkillStatus
from apis.shared.skills.repository import get_skill_catalog_repository
from apis.shared.skills.resource_store import compute_content_hash
from apis.shared.skills.user_service import (
    UserSkillError,
    UserSkillLimitError,
    UserSkillNotFoundError,
    get_user_skill_service,
)
from apis.shared.workflows.format import (
    INPUT_READS,
    RESULT_MERGE,
    Workflow,
    WorkflowError,
    WorkflowStep,
    carry_layout,
    read_workflow_file,
    require_workflow_name,
    same_definition,
    serialize_workflow,
    slug_for_title,
    step_name_for,
    workflow_filename,
    workflow_fingerprint,
    workflow_name_of_ref,
)

from apis.app_api.handoffs.workflow_runs import _step_model as step_model

from .models import (
    AgentView,
    CreateWorkflowRequest,
    ResultView,
    SheetBlocker,
    SheetView,
    StepView,
    WorkflowDefinitionModel,
    WorkflowDocument,
    WorkflowListResponse,
    WorkflowSummary,
    WorkflowView,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])

DEFAULT_TITLE = "Untitled workflow"
WORKFLOW_SKILL_KIND = "workflow"

#: The carrying skill's own text. Written once and never lists the steps, so editing a workflow
#: never moves the prefix of an Agent that has the skill bound (§12).
_CARRIER_INSTRUCTIONS = (
    "This skill holds a saved workflow: a set of Agent steps that a person starts from the "
    "Workflows page and approves before anything runs. It has no instructions for you, and you "
    "cannot start it from a conversation."
)


def _require_workflows() -> None:
    """404 while the flag is off. Called first in each handler, after authentication."""
    if not workflows_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _name(name: str) -> str:
    try:
        return require_workflow_name(name)
    except WorkflowError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


def _skill_error(e: UserSkillError) -> HTTPException:
    if isinstance(e, UserSkillNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    if isinstance(e, UserSkillLimitError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


def _workflow_ref(skill: SkillDefinition, name: str) -> Optional[SkillResourceRef]:
    return next((r for r in skill.resources if workflow_name_of_ref(r) == name), None)


def _is_active(skill: SkillDefinition) -> bool:
    return str(getattr(skill.status, "value", skill.status)) == SkillStatus.ACTIVE.value


async def _readable_skill(skill_id: str, user: User) -> SkillDefinition:
    """A skill the caller owns, or one they can use (``resolve_accessible_skill_ids``), else 404.

    The skill's own ownership, visibility and grants decide, so a skill nobody shared with the
    caller and a skill that does not exist read the same.
    """
    skill = await get_skill_catalog_repository().get_skill(skill_id)
    if skill is not None and skill.owner_id == user.user_id:
        return skill
    if skill is not None and _is_active(skill) and skill_id in set(await resolve_accessible_skill_ids(user)):
        return skill
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")


async def _read_stored(skill: SkillDefinition, name: str) -> Tuple[SkillResourceRef, str]:
    ref = _workflow_ref(skill, name)
    if ref is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    store = get_user_skill_service().bundle_files.resource_store
    content = await run_blocking(store.get, ref.s3_key)
    try:
        return ref, content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"workflow '{name}' is not UTF-8 text")


# --- The sheet ------------------------------------------------------------------------------------


async def _agent_views(workflow: Workflow, user: User) -> Dict[str, AgentView]:
    """Each step's Agent, looked up **as the caller** (one read each, issued together)."""
    agent_ids = sorted({step.agent_id for step in workflow.steps})

    async def one(agent_id: str) -> Tuple[str, Optional[Any]]:
        try:
            agent, _permission = await get_assistant_with_access_check(
                assistant_id=agent_id, user_id=user.user_id, user_email=user.email
            )
        except Exception:  # noqa: BLE001 - an unreadable Agent is an unavailable step
            logger.warning("Workflow: Agent lookup failed", exc_info=True)
            agent = None
        return agent_id, agent

    agents = dict(await asyncio.gather(*(one(agent_id) for agent_id in agent_ids)))
    usable = [agent_id for agent_id in agent_ids if agents.get(agent_id) is not None]
    models = dict(zip(usable, await asyncio.gather(*(step_model(agents[a]) for a in usable))))

    views: Dict[str, AgentView] = {}
    for agent_id in agent_ids:
        agent = agents.get(agent_id)
        if agent is None:
            views[agent_id] = AgentView(can_run=False)
            continue
        listing = getattr(agent, "listing", None)
        published = getattr(listing, "published_version", None) if listing is not None else None
        model_id, rate = models[agent_id]
        views[agent_id] = AgentView(
            can_run=True,
            name=agent.name or "",
            emoji=getattr(agent, "emoji", None),
            image_url=getattr(agent, "image_url", None),
            has_icon=bool(getattr(agent, "icon_key", None)),
            model_id=model_id,
            input_price_per_million_tokens=rate,
            published_version=published,
            runs_draft=getattr(agent, "owner_id", None) == user.user_id,
        )
    return views


def _changed(step: WorkflowStep, agent: AgentView) -> bool:
    """The Agent has a published version other than the one the step recorded (§10, D6)."""
    return agent.can_run and agent.published_version is not None and agent.published_version != step.agent_version


def _sheet(workflow: Workflow, agents: Dict[str, AgentView]) -> SheetView:
    """What blocks Start today, each reason once, with the step it is about.

    Step 1's engine is the bound: at least two steps, each Agent once, every step one the caller
    can run (§20.1: a step the starter can't run blocks Start until it is removed), no step that
    reads another's result until stages (D.5a), and a card result until the merge (D.5b).
    """
    blockers: List[SheetBlocker] = []
    seen: Dict[str, str] = {}
    for step in workflow.steps:
        agent = agents[step.agent_id]
        if not agent.can_run:
            blockers.append(
                SheetBlocker(
                    code="step_unavailable",
                    step=step.name,
                    message="You can't use this step's Agent. Remove the step, or ask its owner to share it",
                )
            )
        if step.agent_id in seen:
            blockers.append(
                SheetBlocker(
                    code="same_agent_twice",
                    step=step.name,
                    message=f"This Agent is already the '{seen[step.agent_id]}' step; each Agent can be a step only once",
                )
            )
        else:
            seen[step.agent_id] = step.name
        if any(read not in INPUT_READS for read in step.reads):
            blockers.append(
                SheetBlocker(
                    code="reads_step",
                    step=step.name,
                    message="A step that reads another step's result needs stages, which aren't available yet",
                )
            )
    if len(workflow.steps) < WORKFLOW_RUN_MIN_STEPS:
        blockers.insert(
            0,
            SheetBlocker(
                code="too_few_steps",
                message=f"A workflow needs at least {WORKFLOW_RUN_MIN_STEPS} steps to start",
            ),
        )
    if workflow.result.mode == RESULT_MERGE:
        blockers.append(
            SheetBlocker(
                code="merge_result",
                message="A merged result isn't available yet; the result comes back as one card",
            )
        )
    return SheetView(startable=not blockers, blockers=blockers)


async def _document(
    skill: SkillDefinition, workflow: Workflow, user: User, *, written: Optional[bool] = None
) -> WorkflowDocument:
    agents = await _agent_views(workflow, user)
    steps = []
    for step in workflow.steps:
        agent = agents[step.agent_id].model_copy(update={"changed": _changed(step, agents[step.agent_id])})
        steps.append(
            StepView(
                name=step.name,
                kind=step.kind,
                agent_id=step.agent_id,
                agent_version=step.agent_version,
                reads=list(step.reads),
                request=step.request,
                effective_request=step.request or workflow.request or "",
                agent=agent,
            )
        )
    view = WorkflowView(
        skill_id=skill.skill_id,
        name=workflow.name,
        title=workflow.title,
        version=workflow.version,
        files=workflow.files,
        request=workflow.request,
        steps=steps,
        result=ResultView(
            mode=workflow.result.mode,
            reads=list(workflow.result.reads) if workflow.result.reads is not None else None,
            steps=list(workflow.result_steps),
            instruction=workflow.result.instruction,
        ),
        fingerprint=workflow_fingerprint(workflow),
        is_owned=skill.owner_id == user.user_id,
    )
    return WorkflowDocument(workflow=view, sheet=_sheet(workflow, agents), written=written)


# --- List -----------------------------------------------------------------------------------------


@router.get("", response_model=WorkflowListResponse, response_model_by_alias=True, response_model_exclude_none=True)
async def list_workflows(user: User = Depends(get_current_user_from_session)) -> WorkflowListResponse:
    """Workflows in skills the caller owns or can use, from their manifests, each file read once."""
    _require_workflows()
    accessible = await resolve_accessible_skill_ids(user)
    if not accessible:
        return WorkflowListResponse(workflows=[])
    records = await get_skill_catalog_repository().batch_get_skills(accessible)
    found = [
        (skill, name)
        for skill in records
        if _is_active(skill)
        for name in sorted(filter(None, (workflow_name_of_ref(r) for r in skill.resources)))
    ]
    store = get_user_skill_service().bundle_files.resource_store

    async def summary(skill: SkillDefinition, name: str) -> WorkflowSummary:
        base = dict(
            skill_id=skill.skill_id,
            skill_name=skill.display_name,
            name=name,
            is_owned=skill.owner_id == user.user_id,
            designer_made=skill.kind == WORKFLOW_SKILL_KIND,
        )
        try:
            ref = _workflow_ref(skill, name)
            content = await run_blocking(store.get, ref.s3_key)  # type: ignore[union-attr]
            workflow, _layout = read_workflow_file(content.decode("utf-8"), name=name)
        except (WorkflowError, UnicodeDecodeError) as e:
            return WorkflowSummary(**base, problem=str(e))
        except Exception:  # noqa: BLE001 - one unreadable file never hides the rest
            logger.warning("Workflow list: a file could not be read", exc_info=True)
            return WorkflowSummary(**base, problem="This workflow's file could not be read")
        return WorkflowSummary(
            **base, title=workflow.title, version=workflow.version, step_count=len(workflow.steps)
        )

    summaries = await asyncio.gather(*(summary(skill, name) for skill, name in found))
    summaries = sorted(summaries, key=lambda s: ((s.title or s.name).lower(), s.skill_id, s.name))
    return WorkflowListResponse(workflows=list(summaries))


# --- Create ---------------------------------------------------------------------------------------


async def _steps_from_run(user: User, run: WorkflowRun) -> Tuple[Optional[str], Tuple[WorkflowStep, ...]]:
    """``(request, steps)`` from a run's kept steps, each Agent's current published version recorded."""
    kept = run.kept_steps()
    lookups = await asyncio.gather(
        *(
            get_assistant_with_access_check(assistant_id=s.agent_id, user_id=user.user_id, user_email=user.email)
            for s in kept
        )
    )
    shared = (run.request or "").strip() or None
    taken: List[str] = []
    steps: List[WorkflowStep] = []
    for run_step, (agent, _permission) in zip(kept, lookups):
        name = step_name_for(run_step.agent_name or (agent.name if agent else "") or "step", taken=taken)
        taken.append(name)
        listing = getattr(agent, "listing", None) if agent is not None else None
        own = (run_step.request or "").strip()
        steps.append(
            WorkflowStep(
                name=name,
                agent_id=run_step.agent_id,
                agent_version=getattr(listing, "published_version", None) if listing is not None else None,
                reads=INPUT_READS,
                request=own if own and own != shared else None,
            )
        )
    return shared, tuple(steps)


@router.post(
    "",
    response_model=WorkflowDocument,
    response_model_by_alias=True,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow(
    body: CreateWorkflowRequest,
    user: User = Depends(get_current_user_from_session),
) -> WorkflowDocument:
    """A new skill carrying one workflow, blank or filled from ``fromRun``.

    The skill is marked ``kind: "workflow"`` and is private and opt-in like every skill the user
    authors. ``fromRun`` takes a run that was started (approved or stopped) from the caller's own
    conversation: its kept steps, in order, each reading the request and the files, and its shared
    request. 404 for a run that isn't the caller's; 409 for one never started, or at the skill cap.
    """
    _require_workflows()
    title = (body.title or "").strip() or DEFAULT_TITLE
    request: Optional[str] = None
    steps: Tuple[WorkflowStep, ...] = ()
    if body.from_run is not None:
        try:
            run = await asyncio.to_thread(get_workflow_run_store().get, user.user_id, body.from_run.run_id)
        except HandoffStoreUnavailable:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Workflow runs are unavailable")
        if run is None or run.session_id != body.from_run.session_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        if run.status not in ("approved", "stopped"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a run that was started can be saved")
        request, steps = await _steps_from_run(user, run)

    name = slug_for_title(title)
    workflow = Workflow(name=name, title=title, request=request, steps=steps)
    try:
        content = serialize_workflow(workflow).encode("utf-8")
    except WorkflowError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    service = get_user_skill_service()
    try:
        skill = await service.create_my_skill(
            user,
            display_name=title,
            description=f"Carries the saved workflow “{title}”, started from the Workflows page.",
            instructions=_CARRIER_INSTRUCTIONS,
            kind=WORKFLOW_SKILL_KIND,
        )
    except UserSkillError as e:
        raise _skill_error(e)
    try:
        refs = await service.add_resource(
            skill.skill_id,
            filename=workflow_filename(name),
            content=content,
            content_type="text/markdown",
            user=user,
            kind="reference",
        )
    except (UserSkillError, ValueError) as e:
        # Never leave a carrier with no workflow in it.
        await service.delete_my_skill(skill.skill_id, user)
        if isinstance(e, UserSkillError):
            raise _skill_error(e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    logger.info("Created a saved workflow", extra={"event": "workflow_created", "from_run": body.from_run is not None})
    skill = skill.model_copy(update={"resources": refs})
    return await _document(skill, workflow, user)


# --- Read -----------------------------------------------------------------------------------------


@router.get(
    "/{skill_id}/{name}",
    response_model=WorkflowDocument,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def read_workflow(
    skill_id: str,
    name: str,
    user: User = Depends(get_current_user_from_session),
):
    """One workflow with each step's Agent as the caller sees it, and the sheet it would open.

    A stored file that does not parse answers 400 with the parser's message and the raw markdown,
    never a 500: a broken file is the one its author needs to open. Only a file written before
    every write path checked workflows can be in that state.
    """
    _require_workflows()
    name = _name(name)
    skill = await _readable_skill(skill_id, user)
    _ref, markdown = await _read_stored(skill, name)
    try:
        workflow, _layout = read_workflow_file(markdown, name=name)
    except WorkflowError as e:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(e), "markdown": markdown})
    return await _document(skill, workflow, user)


# --- Write ----------------------------------------------------------------------------------------


async def _version_conflict(
    skill: SkillDefinition,
    stored_ref: Optional[SkillResourceRef],
    stored: Optional[Workflow],
    expected: int,
    user: User,
) -> Optional[JSONResponse]:
    """409 when the stored file is no longer the version the save was made against, else ``None``.

    The body carries the current document, so the designer can say the workflow changed elsewhere
    and offer to reload it without a second request. A file deleted since is a conflict too, with no
    document. A stored file that no longer parses has no version to compare and is replaced whole,
    as a save without ``expectedVersion`` replaces it.
    """
    if stored_ref is None:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "This workflow was deleted somewhere else",
                "code": "workflow_deleted",
                "document": None,
            },
        )
    if stored is None or stored.version == expected:
        return None
    current = await _document(skill, stored, user)
    logger.info("Refused a stale workflow save", extra={"event": "workflow_version_conflict"})
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": (
                f"This workflow changed somewhere else: it is now version {stored.version}, "
                f"and this save was made against version {expected}"
            ),
            "code": "version_conflict",
            "document": current.model_dump(by_alias=True, exclude_none=True, mode="json"),
        },
    )


@router.put(
    "/{skill_id}/{name}",
    response_model=WorkflowDocument,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def write_workflow(
    skill_id: str,
    name: str,
    body: WorkflowDefinitionModel,
    validate: bool = False,
    user: User = Depends(get_current_user_from_session),
) -> WorkflowDocument | JSONResponse:
    """Create or replace one workflow on a skill the caller owns.

    ``?validate=true`` writes nothing and answers with the normalised definition and its sheet. A
    save whose definition is the stored one writes nothing and leaves ``version`` alone; any other
    save moves ``version`` by one. The body's own ``version`` is never read. With ``expectedVersion``,
    a save made against a version that is no longer the stored one answers 409 with the current
    document (``_version_conflict``); ``?validate=true`` never checks it. The stored file's
    prose, extra frontmatter and extra columns ride through untouched (``carry_layout``). This is
    its own route, not the generic resource upload, because that one always writes.
    """
    _require_workflows()
    name = _name(name)
    service = get_user_skill_service()
    try:
        skill = await service.require_owned(skill_id, user)
    except UserSkillError as e:
        raise _skill_error(e)

    filename = workflow_filename(name)
    stored_ref = next((r for r in skill.resources if r.path == filename), None)
    if stored_ref is not None and stored_ref.kind != "reference":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{filename}' is already one of this skill's {stored_ref.kind} files",
        )

    stored: Optional[Workflow] = None
    layout = None
    if stored_ref is not None:
        _ref, markdown = await _read_stored(skill, name)
        try:
            stored, layout = read_workflow_file(markdown, name=name)
        except WorkflowError:
            stored, layout = None, None  # a broken file is replaced whole

    if body.expected_version is not None and not validate:
        conflict = await _version_conflict(skill, stored_ref, stored, body.expected_version, user)
        if conflict is not None:
            return conflict

    try:
        proposed = body.to_workflow(name=name, version=1)
        unchanged = stored is not None and same_definition(stored, proposed)
        if unchanged:
            target = stored
        else:
            target = replace(proposed, version=stored.version + 1 if stored is not None else 1)
        carried = carry_layout(layout, stored, target) if (layout is not None and stored is not None) else None
        content = serialize_workflow(target, layout=carried).encode("utf-8")
        target, _ = read_workflow_file(content.decode("utf-8"), name=name)
    except WorkflowError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if validate:
        return await _document(skill, target, user, written=False)
    if unchanged or (stored_ref is not None and stored_ref.content_hash == compute_content_hash(content)):
        return await _document(skill, target, user, written=False)

    try:
        await service.add_resource(
            skill_id, filename=filename, content=content, content_type="text/markdown", user=user, kind="reference"
        )
    except UserSkillError as e:
        raise _skill_error(e)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    logger.info("Wrote a saved workflow", extra={"event": "workflow_written", "skill_id": skill_id})
    return await _document(skill, target, user, written=True)


# --- Delete ---------------------------------------------------------------------------------------


@router.delete("/{skill_id}/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    skill_id: str,
    name: str,
    user: User = Depends(get_current_user_from_session),
) -> Response:
    """Remove one workflow from a skill the caller owns.

    The carrying skill goes too, but only when the designer made it (``kind: "workflow"``) and it
    holds no other file: a skill someone built by hand, or one they have since added to, stays.
    """
    _require_workflows()
    name = _name(name)
    service = get_user_skill_service()
    try:
        skill = await service.require_owned(skill_id, user)
    except UserSkillError as e:
        raise _skill_error(e)
    ref = _workflow_ref(skill, name)
    if ref is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    try:
        remaining = await service.delete_resource(skill_id, ref.path, user)
        if skill.kind == WORKFLOW_SKILL_KIND and not remaining:
            await service.delete_my_skill(skill_id, user)
    except UserSkillError as e:
        raise _skill_error(e)
    logger.info("Deleted a saved workflow", extra={"event": "workflow_deleted", "skill_id": skill_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
