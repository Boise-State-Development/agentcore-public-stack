"""Scheduled prompts — schedule data model + CRUD for scheduled agent runs.

B1 (this PR) only creates/edits/pauses/deletes the record. The dispatcher
that reads DueScheduleIndex and actually fires a run is B2
(docs/specs/scheduled-agent-runs.md §7).
"""

from .models import (
    DUE_INDEX_PK,
    ScheduleCadence,
    ScheduledPrompt,
    ScheduledPromptState,
)
from .service import (
    ScheduledPromptLimitExceeded,
    compute_next_run_at,
    create_scheduled_prompt,
    delete_scheduled_prompt,
    get_scheduled_prompt,
    list_due_schedules,
    list_scheduled_prompts,
    max_schedules_per_user,
    rearm_schedule,
    record_run_result,
    set_schedule_state,
    update_scheduled_prompt,
)

__all__ = [
    "DUE_INDEX_PK",
    "ScheduleCadence",
    "ScheduledPrompt",
    "ScheduledPromptLimitExceeded",
    "ScheduledPromptState",
    "compute_next_run_at",
    "create_scheduled_prompt",
    "delete_scheduled_prompt",
    "get_scheduled_prompt",
    "list_due_schedules",
    "list_scheduled_prompts",
    "max_schedules_per_user",
    "rearm_schedule",
    "record_run_result",
    "set_schedule_state",
    "update_scheduled_prompt",
]
