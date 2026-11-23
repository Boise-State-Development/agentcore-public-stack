"""Admin routes for task models: the Task models tab on Manage Models.

docs/specs/admin-model-assignments.md §4.5. The tab lists each task the platform runs a model
for outside the chat turn (titles, tool summaries, compaction, extraction), with its default,
its assignment and the models that may fill it. The backend decides eligibility
(``apis.shared.models.task_models.eligibility_problem``); the SPA only renders the list it is
given.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.audit.models import TARGET_TASK_MODEL, AuditAction
from apis.shared.audit.service import get_audit_service
from apis.shared.auth import User, require_admin_scope
from apis.shared.models.managed_models import list_all_managed_models
from apis.shared.models.models import ManagedModel
from apis.shared.models.task_models import (
    TaskModelSlot,
    TaskSlotView,
    build_slot_views,
    effort_default,
    effort_levels,
    load_assignments,
    save_assignment,
    validate_assignment,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/task-models", tags=["admin-task-models"])

# Same scope as the rest of model management: an admin who curates the catalog decides
# which catalog models the platform's own tasks run on.
require_task_models_admin = require_admin_scope("admin.models")


class _Camel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class TaskModelOption(_Camel):
    model_id: str = Field(..., alias="modelId")
    model_name: str = Field(..., alias="modelName")
    provider_name: str = Field(..., alias="providerName")
    effort_levels: List[str] = Field(default_factory=list, alias="effortLevels")
    effort_default: Optional[str] = Field(None, alias="effortDefault")
    input_price_per_million_tokens: float = Field(..., alias="inputPricePerMillionTokens")
    output_price_per_million_tokens: float = Field(..., alias="outputPricePerMillionTokens")


class TaskModelRef(_Camel):
    model_id: str = Field(..., alias="modelId")
    model_name: Optional[str] = Field(None, alias="modelName")
    effort: Optional[str] = None


class TaskModelAssignment(TaskModelRef):
    updated_at: Optional[str] = Field(None, alias="updatedAt")
    updated_by: Optional[str] = Field(None, alias="updatedBy")


class TaskModelEffective(TaskModelRef):
    source: str


class TaskModelSlotResponse(_Camel):
    slot: str
    title: str
    description: str
    guidance: str
    metered: bool
    available: bool
    default_source: str = Field(..., alias="defaultSource")
    default_model: Optional[TaskModelRef] = Field(None, alias="defaultModel")
    default_notice: Optional[str] = Field(None, alias="defaultNotice")
    assignment: Optional[TaskModelAssignment] = None
    assignment_problem: Optional[str] = Field(None, alias="assignmentProblem")
    effective: TaskModelEffective
    eligible_models: List[TaskModelOption] = Field(default_factory=list, alias="eligibleModels")


class TaskModelsListResponse(_Camel):
    slots: List[TaskModelSlotResponse]


class TaskModelUpdateRequest(_Camel):
    model_id: Optional[str] = Field(
        None,
        alias="modelId",
        description="A catalog model id, or null to clear the assignment and use the default.",
    )
    effort: Optional[str] = Field(
        None, description="One of the model's effort levels, or null for the model's own default."
    )


def _name(record: Optional[ManagedModel]) -> Optional[str]:
    return record.model_name if record is not None else None


def _option(record: ManagedModel) -> TaskModelOption:
    return TaskModelOption(
        modelId=record.model_id,
        modelName=record.model_name,
        providerName=record.provider_name,
        effortLevels=effort_levels(record),
        effortDefault=effort_default(record),
        inputPricePerMillionTokens=record.input_price_per_million_tokens,
        outputPricePerMillionTokens=record.output_price_per_million_tokens,
    )


def _serialize(view: TaskSlotView) -> TaskModelSlotResponse:
    spec = view.spec
    assignment = None
    if view.assignment and view.assignment.get("modelId"):
        assignment = TaskModelAssignment(
            modelId=view.assignment["modelId"],
            modelName=_name(view.assignment_record),
            effort=view.assignment.get("effort"),
            updatedAt=view.assignment.get("updatedAt"),
            updatedBy=view.assignment.get("updatedBy"),
        )
    try:
        available = spec.available()
    except Exception:
        available = True
    return TaskModelSlotResponse(
        slot=spec.slot.value,
        title=spec.title,
        description=spec.description,
        guidance=spec.guidance,
        metered=spec.metered,
        available=available,
        defaultSource=spec.default_source,
        defaultModel=(
            TaskModelRef(modelId=view.default_model_id, modelName=_name(view.default_record))
            if view.default_model_id
            else None
        ),
        defaultNotice=view.default_notice,
        assignment=assignment,
        assignmentProblem=view.assignment_problem,
        effective=TaskModelEffective(
            modelId=view.effective.model_id,
            modelName=_name(view.effective.record),
            effort=view.effective.effort,
            source=view.effective.source,
        ),
        eligibleModels=[_option(m) for m in view.eligible],
    )


async def _views() -> List[TaskSlotView]:
    assignments = await load_assignments(fresh=True)
    catalog = await list_all_managed_models()
    return build_slot_views(assignments, catalog)


@router.get("", response_model=TaskModelsListResponse, response_model_by_alias=True)
async def list_task_models(
    admin_user: User = Depends(require_task_models_admin),
) -> TaskModelsListResponse:
    """Every task, its default, its assignment and the models that may fill it."""
    try:
        views = await _views()
    except Exception:
        logger.error("Error listing task models", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error listing task models")
    return TaskModelsListResponse(slots=[_serialize(v) for v in views])


@router.put("/{slot}", response_model=TaskModelSlotResponse, response_model_by_alias=True)
async def update_task_model(
    slot: str,
    body: TaskModelUpdateRequest,
    admin_user: User = Depends(require_task_models_admin),
) -> TaskModelSlotResponse:
    """Assign a model (and optionally an effort level) to one task, or clear it with ``modelId: null``."""
    try:
        task = TaskModelSlot(slot)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown task '{slot}'")

    model_id = (body.model_id or "").strip() or None
    effort = (body.effort or "").strip() or None
    if model_id is None and effort is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose a model to set an effort level; the default runs at its own effort.",
        )

    try:
        before = (await load_assignments(fresh=True)).get(task.value)
        catalog = await list_all_managed_models()
    except Exception:
        logger.error("Error reading task models before an update", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error updating task model")

    if model_id is not None:
        try:
            model_id, effort = validate_assignment(model_id, effort, catalog)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    try:
        await save_assignment(task, model_id, effort, updated_by=admin_user.email or admin_user.user_id)
    except Exception:
        logger.error("Error saving task model", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error updating task model")

    logger.info(
        "Admin updated a task model",
        extra={"event": "task_model_updated", "slot": task.value, "cleared": model_id is None},
    )
    get_audit_service().record(
        action=AuditAction.TASK_MODEL_UPDATED,
        actor=admin_user,
        target_id=task.value,
        target_type=TARGET_TASK_MODEL,
        changes=["modelId", "effort"],
        before={"modelId": (before or {}).get("modelId"), "effort": (before or {}).get("effort")},
        after={"modelId": model_id, "effort": effort},
    )

    views = await _views()
    view = next(v for v in views if v.spec.slot == task)
    return _serialize(view)
