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
- **Each prompt step's model is judged as the caller** (:func:`model_views`, workflow-designer
  §21.3): it needs a catalog row and the shared model-access rule, after retirement.
- **The sheet's blockers** are step 1's engine limits (:func:`sheet_for`), plus a prompt step
  while ``PROMPT_STEPS_ENABLED`` is off, or on a model the caller can't run.
- **A checklist step** (workflow-designer §22.3) is judged like a prompt step, on its model, and
  also on its field sets (:func:`field_set_views`): each must still be in the workflow's skill and
  hold no ``list`` field, since a set can change after the workflow is saved. It blocks while
  ``CHECKLIST_STEPS_ENABLED`` is off too.

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
from apis.shared.feature_flags import checklist_steps_enabled, prompt_steps_enabled
from apis.shared.handoff.runs import WORKFLOW_RUN_MIN_STEPS
from apis.shared.models.retirement import resolve_effective_model
from apis.shared.skills.access import resolve_accessible_skill_ids
from apis.shared.skills.models import SkillDefinition, SkillResourceRef, SkillStatus
from apis.shared.skills.repository import get_skill_catalog_repository
from apis.shared.skills.user_service import get_user_skill_service
from apis.shared.workflows.field_set_refs import FieldSetLookup, read_skill_field_sets
from apis.shared.workflows.format import (
    CHECKLIST_REFUSED_FIELD_TYPE,
    RESULT_MERGE,
    Workflow,
    WorkflowError,
    WorkflowStep,
    checklist_field_set_names,
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


ModelUnavailableReason = Literal["no_catalog_row", "no_access", "retired", "unsupported", "unavailable"]


class ModelView(BaseModel):
    """A prompt step's model as the caller sees it (workflow-designer §21.3).

    ``modelId`` is the id the step runs on, after retirement redirects; ``requestedModelId`` is the
    file's, when that differs. ``canRun`` is the shared model-access rule as the caller, which
    refuses an id with no catalog row; ``reason`` says why not.
    """

    model_config = ConfigDict(populate_by_name=True)

    can_run: bool = Field(..., alias="canRun")
    model_id: str = Field(..., alias="modelId")
    requested_model_id: Optional[str] = Field(None, alias="requestedModelId")
    name: Optional[str] = Field(None, description="The catalog's display name")
    input_price_per_million_tokens: Optional[float] = Field(None, alias="inputPricePerMillionTokens")
    reason: Optional[ModelUnavailableReason] = None
    reads_documents: bool = Field(
        True,
        exclude=True,
        description="Whether the model takes the run's documents; never serialized (see model_for_step)",
    )


def model_for_step(model: "ModelView", step: Any) -> "ModelView":
    """``model`` as one step can use it: a TEXT-only model can't run a step that reads ``files``.

    The view is one per model; whether the step sends documents is the step's, so it is applied
    here, by the rule the Runtime refuses with (``prompt_model_supported``).
    """
    from apis.shared.workflows.format import READ_FILES

    if model.can_run and not model.reads_documents and READ_FILES in (getattr(step, "reads", None) or ()):
        return model.model_copy(update={"can_run": False, "reason": "unsupported"})
    return model


FieldSetUnavailableReason = Literal["missing", "list_field", "unreadable"]


class FieldSetView(BaseModel):
    """One field set a checklist step runs, as the workflow's skill holds it now (§22.3).

    ``canRun`` is false when the skill no longer has the set (``missing``), it holds a ``list``
    field (``list_field``), or it couldn't be read (``unreadable``). ``fieldCount`` is how many
    fields the step's call for it asks for, which the sheet's estimate reads.
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str
    can_run: bool = Field(..., alias="canRun")
    title: Optional[str] = None
    version: Optional[str] = None
    field_count: Optional[int] = Field(None, alias="fieldCount")
    reason: Optional[FieldSetUnavailableReason] = None


class StepView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    kind: str
    agent_id: str = Field(..., alias="agentId", description="Empty on a prompt or checklist step")
    agent_version: Optional[int] = Field(None, alias="agentVersion")
    model_id: Optional[str] = Field(
        None, alias="modelId", description="A prompt or checklist step's model, as the file names it"
    )
    reads: List[str]
    request: Optional[str] = None
    effective_request: str = Field(
        ...,
        alias="effectiveRequest",
        description="Its own request, else the shared one; a checklist step's guidance alone, possibly empty",
    )
    agent: AgentView = Field(
        ...,
        description=(
            "The step's worker as the caller sees it. On a prompt step it is the model's view in this "
            "shape (name, modelId, rate, canRun), so a reader that names a step's worker still can"
        ),
    )
    model: Optional[ModelView] = Field(None, description="A prompt or checklist step's model as the caller sees it")
    field_sets: Optional[List[FieldSetView]] = Field(
        None,
        alias="fieldSets",
        description="A checklist step's field sets, in the order it runs them, each as the skill holds it now",
    )
    calls: Optional[int] = Field(
        None,
        description=(
            "How many model calls the step makes: one per field set on a checklist step (§22.3), one on a "
            "prompt step; absent on an Agent step, whose turn makes as many as it needs"
        ),
    )


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
    "prompt_steps_off",
    "model_unavailable",
    "checklist_steps_off",
    "field_set_unavailable",
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
    merge_instruction: Optional[str] = Field(
        None,
        alias="mergeInstruction",
        description=(
            "A merged result only (PR-D.5b): what the one turn that merges will ask the source "
            "conversation's model, the file's instruction or the default"
        ),
    )


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
    """Each Agent step's Agent, looked up **as the caller** (one read each, issued together).

    A prompt step has no Agent, so its model id is never looked up as one (:func:`model_views`).
    """
    agent_ids = sorted({step.agent_id for step in workflow.steps if not step.is_model_step})

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


async def prompt_model_view(model_id: str, user: User) -> ModelView:
    """A prompt step's model **as the caller**: retirement first, then the shared access rule.

    The id it runs on is the effective one (a retired model's successor). A retired model with no
    successor, an id with no catalog row, a model a prompt step can't run on
    (``prompt_model_supported``, the Runtime's own rule) and one the caller's roles don't grant all read as
    ``canRun: false`` with their reason; the shared rule (``AppRoleService.can_access_model``,
    which delegates to ``grants_model_access``) is the only access check. Never raises.
    """
    from apis.shared.handoff.prompt_models import prompt_model_supported
    from apis.shared.rbac.service import get_app_role_service

    try:
        effective = await resolve_effective_model(model_id)
        if effective is None:
            return ModelView(can_run=False, model_id=model_id, reason="unavailable")
        record = effective.record
        view = ModelView(
            can_run=False,
            model_id=effective.model_id,
            requested_model_id=model_id if effective.redirected else None,
            name=(record.model_name or None) if record is not None else None,
            input_price_per_million_tokens=(
                float(record.input_price_per_million_tokens) if record is not None else None
            ),
        )
        if effective.denied:
            return view.model_copy(update={"reason": "retired"})
        if effective.uncataloged:
            return view.model_copy(update={"reason": "no_catalog_row"})
        if record is not None and not prompt_model_supported(record, reads_files=False):
            # One rule with the Runtime, which would stop the step ``model_refused``.
            return view.model_copy(update={"reason": "unsupported"})
        view = view.model_copy(update={"reads_documents": prompt_model_supported(record, reads_files=True)})
        allowed = await get_app_role_service().can_access_model(
            user, effective.model_id, record=record, catalog_read=effective.catalog_read
        )
        return view.model_copy(update={"can_run": True} if allowed else {"reason": "no_access"})
    except Exception:  # noqa: BLE001 - an unreadable model is an unavailable step
        logger.warning("Workflow: model lookup failed", exc_info=True)
        return ModelView(can_run=False, model_id=model_id, reason="unavailable")


async def model_views(workflow: Workflow, user: User) -> Dict[str, ModelView]:
    """Each prompt or checklist step's model as the caller, keyed by the file's id (one check each, together)."""
    model_ids = sorted({step.model_id for step in workflow.steps if step.is_model_step and step.model_id})
    views = await asyncio.gather(*(prompt_model_view(model_id, user) for model_id in model_ids))
    return dict(zip(model_ids, views))


def model_as_worker(model: ModelView) -> AgentView:
    """A prompt step's model in the ``agent`` slot of its :class:`StepView` (see that field)."""
    return AgentView(
        can_run=model.can_run,
        name=(model.name or model.model_id) if model.can_run else None,
        model_id=model.model_id,
        input_price_per_million_tokens=model.input_price_per_million_tokens,
    )


async def field_set_views(skill: SkillDefinition, workflow: Workflow) -> Dict[str, FieldSetView]:
    """Each field set the workflow's checklist steps run, as its skill holds it now (§22.3).

    One GET per named set, issued together; a workflow with no checklist step reads nothing. The
    sets are read from the workflow's own skill, whoever is looking, since that is where a step
    resolves them. Never raises.
    """
    names = checklist_field_set_names(workflow)
    if not names:
        return {}
    # The store the workflow file itself is read from (``read_stored_workflow_text``).
    store = get_user_skill_service().bundle_files.resource_store
    lookups = await read_skill_field_sets(skill.resources, names, read=store.get)
    return {name: field_set_view(lookup) for name, lookup in lookups.items()}


def field_set_view(lookup: FieldSetLookup) -> FieldSetView:
    """A looked-up set as the sheet shows it, by the format's own rule (``field_set_ref_problem``)."""
    if lookup.unreadable:
        return FieldSetView(name=lookup.name, can_run=False, reason="unreadable")
    field_set = lookup.field_set
    if field_set is None:
        return FieldSetView(name=lookup.name, can_run=False, reason="missing")
    listed = any(field.type == CHECKLIST_REFUSED_FIELD_TYPE for field in field_set.fields)
    return FieldSetView(
        name=lookup.name,
        can_run=not listed,
        title=field_set.title,
        version=field_set.version,
        field_count=len(field_set.fields),
        reason="list_field" if listed else None,
    )


def step_calls(step: WorkflowStep) -> Optional[int]:
    """How many model calls a step makes, for the estimate: one per set, one, or ``None`` for an Agent."""
    if step.is_checklist:
        return len(step.field_sets)
    if step.is_prompt:
        return 1
    return None


def step_changed(step: WorkflowStep, agent: AgentView) -> bool:
    """The Agent has a published version other than the one the step recorded (§10, D6)."""
    if step.is_model_step:
        return False
    return agent.can_run and agent.published_version is not None and agent.published_version != step.agent_version


# --- The sheet ------------------------------------------------------------------------------------


_MODEL_BLOCKER_MESSAGES: Dict[str, str] = {
    "no_catalog_row": "This step's model isn't offered on this platform. Remove the step, or choose another model",
    "no_access": "You can't use this step's model. Remove the step, or choose a model you can use",
    "retired": "This step's model has been retired. Remove the step, or choose another model",
    "unsupported": "This step's model can't run a prompt step. Remove the step, or choose a text model that reads documents",
    "unavailable": "This step's model couldn't be checked. Remove the step, or try again",
}

_FIELD_SET_BLOCKER_MESSAGES: Dict[str, str] = {
    "missing": "This step's field set '{name}' is no longer in the workflow's skill. Remove the step, or add the set back",
    "list_field": "This step's field set '{name}' has a list field, which a checklist step can't fill yet. Remove the step, or change the field's type",
    "unreadable": "This step's field set '{name}' couldn't be read. Remove the step, or try again",
}


def sheet_for(
    workflow: Workflow,
    agents: Dict[str, AgentView],
    models: Optional[Dict[str, ModelView]] = None,
    field_sets: Optional[Dict[str, FieldSetView]] = None,
    *,
    prompt_steps: Optional[bool] = None,
    checklist_steps: Optional[bool] = None,
) -> SheetView:
    """What blocks Start today, each reason once, with the step it is about.

    Step 1's engine is the bound: at least two steps, each Agent once, and every step one the
    caller can run (§20.1: a step the starter can't run blocks Start until it is removed). A step
    that reads another step's result runs in a later stage (PR-D.5a), and a merged result starts
    by itself once the run settles (PR-D.5b), so neither blocks.

    A prompt step (§21.3) has no Agent, so "each Agent once" does not apply to it. It blocks while
    ``PROMPT_STEPS_ENABLED`` is off (``prompt_steps`` overrides the flag), and while its model
    (``models``, keyed by the file's id) is one the caller can't run: no catalog row, no access,
    or retired.

    A checklist step (§22.3) blocks while ``CHECKLIST_STEPS_ENABLED`` is off (``checklist_steps``
    overrides the flag), on its model by the prompt step's rule, and on each field set
    (``field_sets``, by name) the skill no longer has, or holds a ``list`` field in.
    """
    blockers: List[SheetBlocker] = []
    seen: Dict[str, str] = {}
    prompts_on = prompt_steps_enabled() if prompt_steps is None else prompt_steps
    checklists_on = checklist_steps_enabled() if checklist_steps is None else checklist_steps
    for step in workflow.steps:
        if step.is_checklist:
            if not checklists_on:
                blockers.append(
                    SheetBlocker(
                        code="checklist_steps_off",
                        step=step.name,
                        message="Checklist steps aren't available here yet. Remove the step to start",
                    )
                )
                continue
            model = (models or {}).get(step.model_id or "")
            if model is not None:
                model = model_for_step(model, step)
            if model is None or not model.can_run:
                reason = model.reason if model is not None and model.reason else "unavailable"
                blockers.append(
                    SheetBlocker(code="model_unavailable", step=step.name, message=_MODEL_BLOCKER_MESSAGES[reason])
                )
            for set_name in step.field_sets:
                view = (field_sets or {}).get(set_name)
                if view is not None and view.can_run:
                    continue
                reason = view.reason if view is not None and view.reason else "unreadable"
                blockers.append(
                    SheetBlocker(
                        code="field_set_unavailable",
                        step=step.name,
                        message=_FIELD_SET_BLOCKER_MESSAGES[reason].format(name=set_name),
                    )
                )
            continue
        if step.is_prompt:
            if not prompts_on:
                blockers.append(
                    SheetBlocker(
                        code="prompt_steps_off",
                        step=step.name,
                        message="Prompt steps aren't available here yet. Remove the step to start",
                    )
                )
                continue
            model = (models or {}).get(step.model_id or "")
            if model is not None:
                model = model_for_step(model, step)
            if model is None or not model.can_run:
                reason = model.reason if model is not None and model.reason else "unavailable"
                blockers.append(
                    SheetBlocker(code="model_unavailable", step=step.name, message=_MODEL_BLOCKER_MESSAGES[reason])
                )
            continue
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
    if len(workflow.steps) < WORKFLOW_RUN_MIN_STEPS:
        blockers.insert(
            0,
            SheetBlocker(
                code="too_few_steps",
                message=f"A workflow needs at least {WORKFLOW_RUN_MIN_STEPS} steps to start",
            ),
        )
    return SheetView(startable=not blockers, blockers=blockers, merge_instruction=merge_instruction(workflow))


#: What the one merge turn says when the file's ``## Result`` has no instruction of its own (PR-D.5b).
DEFAULT_MERGE_INSTRUCTION = "Combine these results into one answer."


def merge_instruction(workflow: Workflow) -> Optional[str]:
    """The text of the turn that merges a run's results, or None for a card result (PR-D.5b).

    The file's ``## Result`` instruction, else :data:`DEFAULT_MERGE_INSTRUCTION`, so the run row and
    the sheet always name what the source conversation's model will be asked.
    """
    if workflow.result.mode != RESULT_MERGE:
        return None
    return (workflow.result.instruction or "").strip() or DEFAULT_MERGE_INSTRUCTION


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
    """The workflow with each step's worker as the caller sees it, and the sheet it would open."""
    agents, models, sets = await asyncio.gather(
        agent_views(workflow, user), model_views(workflow, user), field_set_views(skill, workflow)
    )
    return document_from(skill, workflow, user, agents, models, sets, written=written)


def document_from(
    skill: SkillDefinition,
    workflow: Workflow,
    user: User,
    agents: Dict[str, AgentView],
    models: Optional[Dict[str, ModelView]] = None,
    field_sets: Optional[Dict[str, FieldSetView]] = None,
    *,
    written: Optional[bool] = None,
) -> WorkflowDocument:
    """:func:`workflow_document` from worker views already looked up."""
    steps = []
    for step in workflow.steps:
        model: Optional[ModelView] = None
        sets: Optional[List[FieldSetView]] = None
        if step.is_checklist:
            sets = [
                (field_sets or {}).get(name) or FieldSetView(name=name, can_run=False, reason="unreadable")
                for name in step.field_sets
            ]
        if step.is_model_step:
            model = (models or {}).get(step.model_id or "") or ModelView(
                can_run=False, model_id=step.model_id or "", reason="unavailable"
            )
            model = model_for_step(model, step)
            agent = model_as_worker(model)
        else:
            agent = agents[step.agent_id].model_copy(update={"changed": step_changed(step, agents[step.agent_id])})
        steps.append(
            StepView(
                name=step.name,
                kind=step.kind,
                agent_id=step.agent_id,
                agent_version=step.agent_version,
                model_id=step.model_id,
                reads=list(step.reads),
                request=step.request,
                # A checklist step's own text is guidance, never the shared request (§22.3).
                effective_request=(step.request or "") if step.is_checklist else (step.request or workflow.request or ""),
                agent=agent,
                model=model,
                field_sets=sets,
                calls=step_calls(step),
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
    return WorkflowDocument(workflow=view, sheet=sheet_for(workflow, agents, models, field_sets), written=written)


__all__ = [
    "DEFAULT_MERGE_INSTRUCTION",
    "AgentView",
    "FieldSetUnavailableReason",
    "FieldSetView",
    "ModelUnavailableReason",
    "ModelView",
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
    "field_set_view",
    "field_set_views",
    "is_active_skill",
    "load_workflow",
    "merge_instruction",
    "model_as_worker",
    "model_for_step",
    "model_views",
    "prompt_model_view",
    "read_stored_workflow_text",
    "readable_workflow_skill",
    "sheet_for",
    "step_calls",
    "step_changed",
    "step_model",
    "without_steps",
    "workflow_document",
    "workflow_ref",
]
