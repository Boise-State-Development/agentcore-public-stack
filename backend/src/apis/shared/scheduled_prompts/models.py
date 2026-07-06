"""Scheduled-prompt models — the schedule data model for scheduled agent runs.

A ScheduledPrompt is inert data: nothing fires unless the (future, B2)
dispatcher reads it from the sparse DueScheduleIndex, so deleting the record
is total revocation of the schedule (docs/specs/scheduled-agent-runs.md §5).
This module (B1) only creates/edits/pauses/deletes the record — the
dispatcher/worker that actually calls ``run_agent_headless`` lands in B2.

Stored in the sessions-metadata table under the owning user (adjacency list):
    PK: USER#{user_id}
    SK: SCHEDPROMPT#{schedule_id}

DueScheduleIndex (sparse) keys are present ONLY while state == "active" — a
paused schedule is physically invisible to the dispatcher, not filtered at
query time (mirrors SyncPolicy's DueSyncIndex discipline exactly).
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ScheduleCadence = Literal["daily", "weekday", "weekly"]
ScheduledPromptState = Literal["active", "paused", "paused_error"]

#: Sentinel partition key for the sparse due index. Single logical partition
#: — fine at our scale; shard to SCHEDDUE#{0..N} if writes ever demand it.
DUE_INDEX_PK = "SCHEDDUE"


class ScheduledPrompt(BaseModel):
    """Complete scheduled-prompt model (internal use)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    schedule_id: str = Field(..., alias="scheduleId", description="Schedule identifier (sched-{12-hex})")
    user_id: str = Field(..., alias="userId", description="Owning user; every record lives under USER#{user_id}")
    assistant_id: Optional[str] = Field(
        None, alias="assistantId", description="Target assistant's ast-id; None runs the default agent"
    )
    label: str = Field(..., min_length=1, max_length=200, description="Human-readable schedule name, e.g. 'Morning Briefing'")
    prompt_text: str = Field(..., alias="promptText", min_length=1, max_length=20_000, description="The prompt to run")
    cadence: ScheduleCadence = Field(..., description="Re-run cadence (bounded enum — no cron)")
    hour_local: int = Field(..., alias="hourLocal", ge=0, le=23, description="Local hour of day to run, 0-23")
    weekday: Optional[int] = Field(
        None, ge=0, le=6, description="0=Monday..6=Sunday; required when cadence == 'weekly'"
    )
    timezone: str = Field(..., description="IANA timezone, e.g. 'America/Boise'")
    state: ScheduledPromptState = Field("active", description="Lifecycle state; only 'active' schedules appear in DueScheduleIndex")
    state_reason: Optional[str] = Field(None, alias="stateReason", description="Human-readable reason for a paused state")
    next_run_at: Optional[str] = Field(None, alias="nextRunAt", description="ISO 8601 UTC next due time; drives DueScheduleIndex sort key")
    last_run_at: Optional[str] = Field(None, alias="lastRunAt", description="ISO 8601 timestamp of the last completed run")
    last_run_status: Optional[str] = Field(None, alias="lastRunStatus", description="Outcome of the last completed run")
    last_run_session_id: Optional[str] = Field(None, alias="lastRunSessionId", description="Session the last run materialized as")
    last_error: Optional[str] = Field(None, alias="lastError", description="Error detail from the last failed run")
    # Runaway guards (B1 fields only — B2 dispatcher/worker enforce these;
    # present now so B2 needs no migration).
    runs_today: int = Field(0, alias="runsToday", description="Runs fired today (runaway-guard counter)")
    runs_today_date: Optional[str] = Field(
        None, alias="runsTodayDate", description="UTC date (YYYY-MM-DD) runsToday is counting against"
    )
    max_runs_per_day: int = Field(
        24, alias="maxRunsPerDay", description="Runaway guard ceiling; the dispatcher auto-pauses a schedule that exceeds this in a day"
    )
    # Snapshot at creation (Phase A punch #7) — never resolved lazily at fire
    # time, so the catalog shifting under a sleeping schedule can't cause a
    # least-surprise violation.
    enabled_tools: Optional[List[str]] = Field(
        None, alias="enabledTools", description="Tool ids snapshot at creation time; None means 'all RBAC-allowed at creation'"
    )
    deliver_email: bool = Field(False, alias="deliverEmail", description="v1.5 connector-email opt-in; inert in B1")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp of creation")
    updated_at: str = Field(..., alias="updatedAt", description="ISO 8601 timestamp of last update")
