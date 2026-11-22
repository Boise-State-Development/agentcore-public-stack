"""Wire shapes for ``/workflows`` (``docs/specs/workflow-designer.md`` §9.2, PR-D.1).

The designer trades these; the file is written and read only by
``apis.shared.workflows.format`` (D1). Conversions live here so the routes stay
about access and storage.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.workflows.format import (
    FILES_ANY,
    INPUT_READS,
    KIND_AGENT,
    MAX_CHECKLIST_FIELD_SETS,
    MAX_REQUEST_CHARS,
    MAX_STEPS,
    RESULT_CARD,
    Workflow,
    WorkflowResult,
    WorkflowStep,
)

# The document every read and write answers with lives beside the sheet logic it carries, in
# ``apis.shared``, because run create (``app_api/handoffs``) builds the same one (PR-D.2).
from apis.shared.workflows.sheet import (  # noqa: F401 - re-exported for the routes and tests
    AgentView,
    ModelView,
    ResultView,
    SheetBlocker,
    SheetBlockerCode,
    SheetView,
    StepView,
    WorkflowDocument,
    WorkflowView,
)


class WorkflowStepModel(BaseModel):
    """One step as the designer edits it."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(
        KIND_AGENT,
        description=(
            "agent; prompt, one model call with no Agent (§21.3); or checklist, one model call per field set (§22.3)"
        ),
    )
    agent_id: str = Field(
        "", alias="agentId", max_length=256, description="An Agent step's Agent; empty on a prompt or checklist step"
    )
    agent_version: Optional[int] = Field(
        None,
        alias="agentVersion",
        description="The Agent's published version when the step was added or last reviewed (D6)",
    )
    model_id: Optional[str] = Field(
        None, alias="modelId", max_length=512, description="A prompt or checklist step's model; absent on an Agent step"
    )
    reads: List[str] = Field(default_factory=lambda: list(INPUT_READS), max_length=MAX_STEPS + 2)
    request: Optional[str] = Field(
        None, max_length=MAX_REQUEST_CHARS, description="This step's own request; a checklist step's guidance"
    )
    field_sets: List[str] = Field(
        default_factory=list,
        alias="fieldSets",
        # One past the limit, so the envelope's own sentence refuses a 17th rather than a schema error.
        max_length=MAX_CHECKLIST_FIELD_SETS + 1,
        description="A checklist step's field sets, in the order it runs them; empty on every other kind",
    )


class WorkflowResultModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: str = RESULT_CARD
    reads: Optional[List[str]] = Field(None, description="Step names; null for every step")
    instruction: Optional[str] = Field(None, max_length=MAX_REQUEST_CHARS)


class WorkflowDefinitionModel(BaseModel):
    """A workflow as the designer sends it. ``version`` is the server's, so a save ignores it."""

    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(..., min_length=1, max_length=200)
    files: str = FILES_ANY
    request: Optional[str] = Field(None, max_length=MAX_REQUEST_CHARS)
    steps: List[WorkflowStepModel] = Field(default_factory=list, max_length=MAX_STEPS)
    result: WorkflowResultModel = Field(default_factory=WorkflowResultModel)
    expected_version: Optional[int] = Field(
        None,
        alias="expectedVersion",
        ge=1,
        description=(
            "PUT only: the stored version the save was made against. A save whose stored file has moved "
            "on answers 409 with the current document, so one tab never clobbers another. Absent, a save "
            "replaces whatever is stored, as before"
        ),
    )

    def to_workflow(self, *, name: str, version: int) -> Workflow:
        return Workflow(
            name=name,
            title=self.title,
            version=version,
            files=self.files,
            request=self.request,
            steps=tuple(
                WorkflowStep(
                    name=step.name,
                    agent_id=step.agent_id,
                    agent_version=step.agent_version,
                    reads=tuple(step.reads),
                    request=step.request,
                    kind=step.kind,
                    model_id=step.model_id,
                    field_sets=tuple(step.field_sets),
                )
                for step in self.steps
            ),
            result=WorkflowResult(
                mode=self.result.mode,
                reads=tuple(self.result.reads) if self.result.reads is not None else None,
                instruction=self.result.instruction,
            ),
        )


class FromRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId", min_length=1, max_length=256)
    run_id: str = Field(..., alias="runId", min_length=1, max_length=256)


class CreateWorkflowRequest(BaseModel):
    """``POST /workflows``: a blank workflow, or one filled from a run's ``WFRUN#`` row."""

    model_config = ConfigDict(populate_by_name=True)

    title: Optional[str] = Field(None, max_length=200, description="'Untitled workflow' when absent")
    from_run: Optional[FromRunRequest] = Field(None, alias="fromRun")


class WorkflowSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    skill_id: str = Field(..., alias="skillId")
    skill_name: str = Field(..., alias="skillName")
    name: str
    title: Optional[str] = None
    version: Optional[int] = None
    step_count: Optional[int] = Field(None, alias="stepCount")
    is_owned: bool = Field(..., alias="isOwned")
    designer_made: bool = Field(..., alias="designerMade", description="The skill is a workflow's carrier")
    problem: Optional[str] = Field(None, description="Why the stored file does not parse; the rest is then absent")


class WorkflowListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    workflows: List[WorkflowSummary]
