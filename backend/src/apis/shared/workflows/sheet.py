"""A saved workflow as the caller sees it, and the sheet it would open (``workflow-designer.md`` §9.2, §11).

Shared by ``/workflows`` (PR-D.1), which answers every read and write with this document, and by
``POST /sessions/{id}/workflow-runs`` with a ``workflow`` (PR-D.2), which starts the file's steps
and refuses while the sheet has a blocker. Both judge it the same way, so a workflow the
Workflows page calls startable is one run create accepts.

- **Access is the skill's** (:func:`readable_workflow_skill`): its owner, or anyone
  ``resolve_accessible_skill_ids`` lets reach it. A skill nobody shared with the caller and one
  that does not exist read the same.
- **Each Agent step is judged as the caller** (:func:`agent_views`), as a mention is, so sharing a
  workflow shares none of its Agents.
- **The sheet's blockers** are step 1's engine limits (:func:`sheet_for`).

Nothing here runs on a turn. Each lookup raises a plain exception the routes map to HTTP.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Dict, Iterable, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.assistants.service import get_assistant_with_access_check
from apis.shared.auth import User
from apis.shared.blocking_io import run_blocking
from apis.shared.handoff.runs import WORKFLOW_RUN_MIN_STEPS
from apis.shared.models.retirement import resolve_effective_model
from apis.shared.skills.access import resolve_accessible_skill_ids
from apis.shared.skills.models import SkillDefinition, SkillResourceRef, SkillStatus
from apis.shared.skills.repository import get_skill_catalog_repository
from apis.shared.skills.user_service import get_user_skill_service
from apis.shared.workflows.format import (
    INPUT_READS,
    RESULT_MERGE,
    Workflow,
    WorkflowError,
    WorkflowStep,
    read_workflow_file,
    workflow_fingerprint,
    workflow_name_of_ref,
)

logger = logging.getLogger(__name__)


class WorkflowNotFound(LookupError):
    """No such workflow, or not one the caller can reach. Routes answer 404."""


class WorkflowUnreadable(ValueError):
    """The stored file can't be read or parsed. ``markdown`` is the raw text when it decoded."""

    def __init__(self, message: str, markdown: Optional[str] = None) -> None:
        super().__init__(message)
        self.markdown = markdown


# --- Wire shapes ----------------------------------------------------------------------------------


class AgentView(BaseModel):
    """A step's Agent as the caller sees it. A name only when the caller can use the Agent."""

    model_config = ConfigDict(populate_by_name=True)

    can_run: bool = Field(..., alias="canRun")
    name: Optional[str] = None
    emoji: Optional[str] = None
    image_url: Optional[str] = Field(None, alias="imageUrl")
    has_icon: bool = Field(False, alias="hasIcon")
    model_id: Optional[str] = Field(
        None, alias="modelId", description="The Agent's own model after retirement redirects; null runs on the send's"
    )
    input_price_per_million_tokens: Optional[float] = Field(None, alias="inputPricePerMillionTokens")
    published_version: Optional[int] = Field(None, alias="publishedVersion")
    changed: bool = Field(
        False, description="The Agent's published version moved since the step recorded it (§10)"
    )
    runs_draft: bool = Field(
        False, alias="runsDraft", description="The caller owns the Agent, so their test runs its draft (§10)"
    )


class StepView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    kind: str
    agent_id: str = Field(..., alias="agentId")
    agent_version: Optional[int] = Field(None, alias="agentVersion")
    reads: List[str]
    request: Optional[str] = None
    effective_request: str = Field(..., alias="effectiveRequest", description="Its own request, else the shared one")
    agent: AgentView


class ResultView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: str
    reads: Optional[List[str]] = None
    steps: List[str] = Field(..., description="The steps the result takes, in table order")
    instruction: Optional[str] = None


class WorkflowView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    skill_id: str = Field(..., alias="skillId")
    name: str
    title: str
    version: int
    files: str
    request: Optional[str] = None
    steps: List[StepView]
    result: ResultView
    fingerprint: str
    is_owned: bool = Field(..., alias="isOwned")


SheetBlockerCode = Literal[
    "too_few_steps",
    "step_unavailable",
    "same_agent_twice",
    "reads_step",
    "merge_result",
]


class SheetBlocker(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    code: SheetBlockerCode
    message: str
    step: Optional[str] = None


class SheetView(BaseModel):
    """What the approval sheet would say if this workflow were started now (§6.1, D3)."""

    model_config = ConfigDict(populate_by_name=True)

    startable: bool
    blockers: List[SheetBlocker]


class WorkflowDocument(BaseModel):
    """``GET``, ``POST`` and ``PUT`` on ``/workflows`` all answer with the workflow and its sheet."""

    model_config = ConfigDict(populate_by_name=True)

    workflow: WorkflowView
    sheet: SheetView
    written: Optional[bool] = Field(
        None, description="PUT only: false for ?validate=true and for a save that changed nothing"
    )


# --- Reading the file, as the caller --------------------------------------------------------------


def workflow_ref(skill: SkillDefinition, name: str) -> Optional[SkillResourceRef]:
    return next((r for r in skill.resources if workflow_name_of_ref(r) == name), None)


def is_active_skill(skill: SkillDefinition) -> bool:
    return str(getattr(skill.status, "value", skill.status)) == SkillStatus.ACTIVE.value


async def readable_workflow_skill(skill_id: str, user: User) -> SkillDefinition:
    """A skill the caller owns, or one they can use (``resolve_accessible_skill_ids``).

    Raises :class:`WorkflowNotFound` otherwise. The skill's own ownership, visibility and grants
    decide, so another user's private workflow and one that does not exist read the same.
    """
    skill = await get_skill_catalog_repository().get_skill(skill_id)
    if skill is not None and skill.owner_id == user.user_id:
        return skill
    if skill is not None and is_active_skill(skill) and skill_id in set(await resolve_accessible_skill_ids(user)):
        return skill
    raise WorkflowNotFound("Workflow not found")


async def read_stored_workflow_text(skill: SkillDefinition, name: str) -> Tuple[SkillResourceRef, str]:
    """The stored file's ref and text. :class:`WorkflowNotFound` when the skill has no such file."""
    ref = workflow_ref(skill, name)
    if ref is None:
        raise WorkflowNotFound("Workflow not found")
    store = get_user_skill_service().bundle_files.resource_store
    content = await run_blocking(store.get, ref.s3_key)
    try:
        return ref, content.decode("utf-8")
    except UnicodeDecodeError:
        raise WorkflowUnreadable(f"workflow '{name}' is not UTF-8 text")


async def load_workflow(skill_id: str, name: str, user: User) -> Tuple[SkillDefinition, Workflow]:
    """The caller's view of one workflow: the skill (read with its access rules) and the parsed file.

    :class:`WorkflowNotFound` when the caller can't reach it; :class:`WorkflowUnreadable` when the
    stored file doesn't parse (with its markdown).
    """
    skill = await readable_workflow_skill(skill_id, user)
    _ref, markdown = await read_stored_workflow_text(skill, name)
    try:
        workflow, _layout = read_workflow_file(markdown, name=name)
    except WorkflowError as e:
        raise WorkflowUnreadable(str(e), markdown=markdown)
    return skill, workflow


# --- Each step's Agent, as the caller -------------------------------------------------------------


async def step_model(agent: Any) -> Tuple[Optional[str], Optional[float]]:
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


async def agent_views(workflow: Workflow, user: User) -> Dict[str, AgentView]:
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


def step_changed(step: WorkflowStep, agent: AgentView) -> bool:
    """The Agent has a published version other than the one the step recorded (§10, D6)."""
    return agent.can_run and agent.published_version is not None and agent.published_version != step.agent_version


# --- The sheet ------------------------------------------------------------------------------------


def sheet_for(workflow: Workflow, agents: Dict[str, AgentView]) -> SheetView:
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


def without_steps(workflow: Workflow, removed: Iterable[str]) -> Tuple[Workflow, Tuple[str, ...]]:
    """The workflow with ``removed`` taken out, and every step that reads one of them (§20.1).

    Returns the trimmed workflow and every step name taken out, in table order. A reader of a
    removed step goes too, and so on down the graph, because it would wait on a result that will
    never come. The result's ``reads`` drops the removed names. Unknown names are ignored.
    """
    gone: Set[str] = {name for name in removed if workflow.step(name) is not None}
    if not gone:
        return workflow, ()
    changed = True
    while changed:
        changed = False
        for step in workflow.steps:
            if step.name not in gone and any(read in gone for read in step.reads):
                gone.add(step.name)
                changed = True
    kept = tuple(step for step in workflow.steps if step.name not in gone)
    result = workflow.result
    if result.reads is not None:
        result = replace(result, reads=tuple(name for name in result.reads if name not in gone))
    taken = tuple(step.name for step in workflow.steps if step.name in gone)
    return replace(workflow, steps=kept, result=result), taken


async def workflow_document(
    skill: SkillDefinition, workflow: Workflow, user: User, *, written: Optional[bool] = None
) -> WorkflowDocument:
    """The workflow with each step's Agent as the caller sees it, and the sheet it would open."""
    agents = await agent_views(workflow, user)
    return document_from(skill, workflow, user, agents, written=written)


def document_from(
    skill: SkillDefinition,
    workflow: Workflow,
    user: User,
    agents: Dict[str, AgentView],
    *,
    written: Optional[bool] = None,
) -> WorkflowDocument:
    """:func:`workflow_document` from Agent views already looked up."""
    steps = []
    for step in workflow.steps:
        agent = agents[step.agent_id].model_copy(update={"changed": step_changed(step, agents[step.agent_id])})
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
    return WorkflowDocument(workflow=view, sheet=sheet_for(workflow, agents), written=written)


__all__ = [
    "AgentView",
    "ResultView",
    "SheetBlocker",
    "SheetBlockerCode",
    "SheetView",
    "StepView",
    "WorkflowDocument",
    "WorkflowNotFound",
    "WorkflowUnreadable",
    "WorkflowView",
    "agent_views",
    "document_from",
    "is_active_skill",
    "load_workflow",
    "read_stored_workflow_text",
    "readable_workflow_skill",
    "sheet_for",
    "step_changed",
    "step_model",
    "without_steps",
    "workflow_document",
    "workflow_ref",
]
