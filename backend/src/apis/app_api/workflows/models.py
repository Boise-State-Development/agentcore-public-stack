"""Wire shapes for ``/workflows`` (``docs/specs/workflow-designer.md`` §9.2, PR-D.1).

The designer trades these; the file is written and read only by
``apis.shared.workflows.format`` (D1). Conversions live here so the routes stay
about access and storage.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.workflows.format import (
    FILES_ANY,
    INPUT_READS,
    KIND_AGENT,
    MAX_REQUEST_CHARS,
    MAX_STEPS,
    RESULT_CARD,
    Workflow,
    WorkflowResult,
    WorkflowStep,
)


class WorkflowStepModel(BaseModel):
    """One step as the designer edits it."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=64)
    kind: str = KIND_AGENT
    agent_id: str = Field(..., alias="agentId", min_length=1, max_length=256)
    agent_version: Optional[int] = Field(
        None,
        alias="agentVersion",
        description="The Agent's published version when the step was added or last reviewed (D6)",
    )
    reads: List[str] = Field(default_factory=lambda: list(INPUT_READS), max_length=MAX_STEPS + 2)
    request: Optional[str] = Field(None, max_length=MAX_REQUEST_CHARS, description="This step's own request")


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
    """``GET``, ``POST`` and ``PUT`` all answer with the workflow and its sheet."""

    model_config = ConfigDict(populate_by_name=True)

    workflow: WorkflowView
    sheet: SheetView
    written: Optional[bool] = Field(
        None, description="PUT only: false for ?validate=true and for a save that changed nothing"
    )


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
