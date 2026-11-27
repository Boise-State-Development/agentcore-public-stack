"""Schedule API request/response models."""

from decimal import Decimal
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apis.shared.scheduled_prompts.models import (
    IntervalUnit,
    ScheduleCadence,
    ScheduledPrompt,
    ScheduledPromptState,
    ScheduleMode,
)
from apis.shared.scheduled_prompts.runs import ScheduleRun
from apis.shared.scheduled_prompts.service import MIN_INTERVAL_MINUTES, interval_to_minutes
from apis.shared.scheduled_prompts.watch_state import WatchState

_MAX_PROMPT_CHARS = 20_000
#: The largest daily budget a schedule may set, USD: a typo guard, not a policy.
MAX_BUDGET_USD = Decimal("1000")


class CreateScheduleRequest(BaseModel):
    """Request body for creating a scheduled prompt."""

    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(..., min_length=1, max_length=200)
    prompt_text: str = Field(..., alias="promptText", min_length=1, max_length=_MAX_PROMPT_CHARS)
    cadence: ScheduleCadence
    hour_local: int = Field(..., alias="hourLocal", ge=0, le=23)
    weekday: Optional[int] = Field(None, ge=0, le=6)
    interval_value: Optional[int] = Field(None, alias="intervalValue", ge=1)
    interval_unit: Optional[IntervalUnit] = Field(None, alias="intervalUnit")
    timezone: str = Field(..., min_length=1, max_length=64)
    assistant_id: Optional[str] = Field(None, alias="assistantId")
    # None = snapshot "all RBAC-allowed at creation" (resolved by the route,
    # exactly as an attended chat turn resolves defaults); an explicit list
    # snapshots that subset instead.
    enabled_tools: Optional[List[str]] = Field(None, alias="enabledTools")
    deliver_email: bool = Field(False, alias="deliverEmail")

    @model_validator(mode="after")
    def _validate_cadence_fields(self) -> "CreateScheduleRequest":
        if self.cadence == "weekly" and self.weekday is None:
            raise ValueError("weekday is required when cadence is 'weekly'")
        if self.cadence == "interval":
            if self.interval_value is None or self.interval_unit is None:
                raise ValueError("intervalValue and intervalUnit are required when cadence is 'interval'")
            if interval_to_minutes(self.interval_value, self.interval_unit) < MIN_INTERVAL_MINUTES:
                raise ValueError(f"interval must be at least {MIN_INTERVAL_MINUTES} minutes")
        return self


class UpdateScheduleRequest(BaseModel):
    """Request body for editing a schedule or pausing/resuming it.

    ``state`` accepts only the user-owned transitions: "paused" and
    "active". A schedule in "paused_error" can also be resumed here — the
    resume is an explicit user decision to try again.
    """

    model_config = ConfigDict(populate_by_name=True)

    label: Optional[str] = Field(None, min_length=1, max_length=200)
    prompt_text: Optional[str] = Field(None, alias="promptText", min_length=1, max_length=_MAX_PROMPT_CHARS)
    cadence: Optional[ScheduleCadence] = None
    hour_local: Optional[int] = Field(None, alias="hourLocal", ge=0, le=23)
    weekday: Optional[int] = Field(None, ge=0, le=6)
    interval_value: Optional[int] = Field(None, alias="intervalValue", ge=1)
    interval_unit: Optional[IntervalUnit] = Field(None, alias="intervalUnit")
    timezone: Optional[str] = Field(None, min_length=1, max_length=64)
    assistant_id: Optional[str] = Field(None, alias="assistantId")
    enabled_tools: Optional[List[str]] = Field(None, alias="enabledTools")
    deliver_email: Optional[bool] = Field(None, alias="deliverEmail")
    state: Optional[Literal["active", "paused"]] = None
    # A bare null cannot express "clear" for assistant_id / enabled_tools (the
    # service reads null as "leave unchanged"), so clearing is an explicit
    # intent. clear_tools re-snapshots the caller's current RBAC-allowed tools
    # (mirrors creation), clear_assistant reverts to the default agent.
    clear_assistant: bool = Field(False, alias="clearAssistant")
    clear_tools: bool = Field(False, alias="clearTools")

    @model_validator(mode="after")
    def _clear_excludes_value(self) -> "UpdateScheduleRequest":
        if self.clear_assistant and self.assistant_id is not None:
            raise ValueError("clearAssistant cannot be combined with assistantId")
        if self.clear_tools and self.enabled_tools is not None:
            raise ValueError("clearTools cannot be combined with enabledTools")
        return self


class CreatePersonalScheduleRequest(CreateScheduleRequest):
    """``POST /schedules``: a personal schedule, which can also be a quiet watcher.

    The watcher fields (docs/specs/quiet-watcher.md §3.1) are on the personal
    requests only: a project schedule's requests extend the base ones and are
    unchanged. Refused while PROACTIVE_RUNS_ENABLED is off, unless left at
    their defaults.
    """

    mode: ScheduleMode = "standard"
    model_id: Optional[str] = Field(None, alias="modelId", min_length=1, max_length=256)
    #: USD a day (docs/specs/proactive-trust.md §4.2). Left out, an interval under 60
    #: minutes gets ``SCHEDULE_DEFAULT_MAX_COST_PER_DAY``; an explicit null there is a 422.
    max_cost_per_day: Optional[Decimal] = Field(None, alias="maxCostPerDay", gt=0, le=MAX_BUDGET_USD)


class UpdatePersonalScheduleRequest(UpdateScheduleRequest):
    """``PATCH /schedules/{id}``, with the watcher fields. ``clearModel`` reverts to the default model."""

    mode: Optional[ScheduleMode] = None
    model_id: Optional[str] = Field(None, alias="modelId", min_length=1, max_length=256)
    clear_model: bool = Field(False, alias="clearModel")
    #: A number sets the budget and an explicit null clears it, refused (422) on an
    #: interval under 60 minutes; left out, the budget is unchanged.
    max_cost_per_day: Optional[Decimal] = Field(None, alias="maxCostPerDay", gt=0, le=MAX_BUDGET_USD)

    @model_validator(mode="after")
    def _clear_model_excludes_model(self) -> "UpdatePersonalScheduleRequest":
        if self.clear_model and self.model_id is not None:
            raise ValueError("clearModel cannot be combined with modelId")
        return self


class ScheduledPromptResponse(BaseModel):
    """Public view of a scheduled prompt."""

    model_config = ConfigDict(populate_by_name=True)

    schedule_id: str = Field(..., alias="scheduleId")
    assistant_id: Optional[str] = Field(None, alias="assistantId")
    label: str
    prompt_text: str = Field(..., alias="promptText")
    cadence: ScheduleCadence
    hour_local: int = Field(..., alias="hourLocal")
    weekday: Optional[int] = None
    interval_value: Optional[int] = Field(None, alias="intervalValue")
    interval_unit: Optional[IntervalUnit] = Field(None, alias="intervalUnit")
    timezone: str
    state: ScheduledPromptState
    state_reason: Optional[str] = Field(None, alias="stateReason")
    next_run_at: Optional[str] = Field(None, alias="nextRunAt")
    last_run_at: Optional[str] = Field(None, alias="lastRunAt")
    last_run_status: Optional[str] = Field(None, alias="lastRunStatus")
    last_run_session_id: Optional[str] = Field(None, alias="lastRunSessionId")
    last_error: Optional[str] = Field(None, alias="lastError")
    runs_today: int = Field(0, alias="runsToday")
    max_runs_per_day: int = Field(24, alias="maxRunsPerDay")
    enabled_tools: Optional[List[str]] = Field(None, alias="enabledTools")
    deliver_email: bool = Field(False, alias="deliverEmail")
    project_id: Optional[str] = Field(
        None, alias="projectId", description="Runs in this project; change or resume it from the project"
    )
    deliver_to: str = Field("owner", alias="deliverTo", description='"project": each run is shared with the project')
    mode: ScheduleMode = Field("standard", description='"watch": a quiet watcher')
    model_id: Optional[str] = Field(None, alias="modelId", description="The model each run uses; null is the default")
    max_cost_per_day: Optional[float] = Field(None, alias="maxCostPerDay", description="USD a day; null is no budget")
    spend_today: float = Field(
        0.0, alias="spendToday", description="USD spent on spendTodayDate (the schedule's own day)"
    )
    spend_today_date: Optional[str] = Field(None, alias="spendTodayDate")
    budget_hold_until: Optional[str] = Field(
        None, alias="budgetHoldUntil", description="Held for its spent budget until this local midnight"
    )
    budget_days_at_limit: int = Field(0, alias="budgetDaysAtLimit")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")

    @classmethod
    def from_schedule(cls, schedule: ScheduledPrompt) -> "ScheduledPromptResponse":
        return cls(
            schedule_id=schedule.schedule_id,
            assistant_id=schedule.assistant_id,
            label=schedule.label,
            prompt_text=schedule.prompt_text,
            cadence=schedule.cadence,
            hour_local=schedule.hour_local,
            weekday=schedule.weekday,
            interval_value=schedule.interval_value,
            interval_unit=schedule.interval_unit,
            timezone=schedule.timezone,
            state=schedule.state,
            state_reason=schedule.state_reason,
            next_run_at=schedule.next_run_at,
            last_run_at=schedule.last_run_at,
            last_run_status=schedule.last_run_status,
            last_run_session_id=schedule.last_run_session_id,
            last_error=schedule.last_error,
            runs_today=schedule.runs_today,
            max_runs_per_day=schedule.max_runs_per_day,
            enabled_tools=schedule.enabled_tools,
            deliver_email=schedule.deliver_email,
            project_id=schedule.project_id,
            deliver_to=schedule.deliver_to,
            mode=schedule.mode,
            model_id=schedule.model_id,
            max_cost_per_day=float(schedule.max_cost_per_day) if schedule.max_cost_per_day is not None else None,
            spend_today=float(schedule.spend_today or 0),
            spend_today_date=schedule.spend_today_date,
            budget_hold_until=schedule.budget_hold_until,
            budget_days_at_limit=schedule.budget_days_at_limit,
            created_at=schedule.created_at,
            updated_at=schedule.updated_at,
        )


class ScheduledPromptsListResponse(BaseModel):
    """Response for listing the caller's scheduled prompts."""

    model_config = ConfigDict(populate_by_name=True)

    schedules: List[ScheduledPromptResponse] = Field(default_factory=list)


class ScheduleRunsResponse(BaseModel):
    """A personal schedule's runs, newest first."""

    model_config = ConfigDict(populate_by_name=True)

    runs: List[ScheduleRun] = Field(default_factory=list)


class WatchStateResponse(BaseModel):
    """A watcher's state; ``state`` is null when it has none."""

    model_config = ConfigDict(populate_by_name=True)

    state: Optional[WatchState] = None
