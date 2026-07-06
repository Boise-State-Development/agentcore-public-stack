"""Schedule API request/response models."""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apis.shared.scheduled_prompts.models import ScheduleCadence, ScheduledPrompt, ScheduledPromptState

_MAX_PROMPT_CHARS = 20_000


class CreateScheduleRequest(BaseModel):
    """Request body for creating a scheduled prompt."""

    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(..., min_length=1, max_length=200)
    prompt_text: str = Field(..., alias="promptText", min_length=1, max_length=_MAX_PROMPT_CHARS)
    cadence: ScheduleCadence
    hour_local: int = Field(..., alias="hourLocal", ge=0, le=23)
    weekday: Optional[int] = Field(None, ge=0, le=6)
    timezone: str = Field(..., min_length=1, max_length=64)
    assistant_id: Optional[str] = Field(None, alias="assistantId")
    # None = snapshot "all RBAC-allowed at creation" (resolved by the route,
    # exactly as an attended chat turn resolves defaults); an explicit list
    # snapshots that subset instead.
    enabled_tools: Optional[List[str]] = Field(None, alias="enabledTools")
    deliver_email: bool = Field(False, alias="deliverEmail")

    @model_validator(mode="after")
    def _weekly_requires_weekday(self) -> "CreateScheduleRequest":
        if self.cadence == "weekly" and self.weekday is None:
            raise ValueError("weekday is required when cadence is 'weekly'")
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
            created_at=schedule.created_at,
            updated_at=schedule.updated_at,
        )


class ScheduledPromptsListResponse(BaseModel):
    """Response for listing the caller's scheduled prompts."""

    model_config = ConfigDict(populate_by_name=True)

    schedules: List[ScheduledPromptResponse] = Field(default_factory=list)
