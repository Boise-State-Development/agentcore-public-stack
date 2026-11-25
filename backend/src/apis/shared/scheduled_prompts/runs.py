"""Run history for personal schedules (docs/specs/quiet-watcher.md §3.4).

The scheduled-runs worker writes one row per run of every personal schedule,
standard or watch, while ``PROACTIVE_RUNS_ENABLED`` is on::

    PK: USER#{user_id} | SK: SCHEDRUN#{schedule_id}#{finished_at}#{run_id}

in the sessions-metadata table, expiring after 30 days through the table's
``ttl`` attribute. A project schedule's runs are on the project instead
(``ProjectSchedules.record_run``), so they get no row here.

Tokens are kept, not dollars: a run's ``C#`` cost rows land asynchronously from
the Runtime, and tokens are enough to tell a 2k-token quiet run from a
40k-token one.

Blocking, like ``watch_state``: callers use ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import service as _service
from .models import ScheduledPrompt

logger = logging.getLogger(__name__)

#: How long a run row is kept.
RUN_TTL_SECONDS = 30 * 24 * 60 * 60
#: The most rows ``GET /schedules/{id}/runs`` returns.
DEFAULT_RUNS_LIMIT = 50

RUN_SK_PREFIX = "SCHEDRUN#"

#: ``completed`` is a standard run; a watch run ends ``quiet`` (nothing new: its
#: conversation is soft-deleted) or ``notify`` (``itemCount`` inbox rows written).
ScheduleRunStatus = Literal["completed", "quiet", "notify", "error", "timeout", "paused"]


def _run_prefix(schedule_id: str) -> str:
    return f"{RUN_SK_PREFIX}{schedule_id}#"


class ScheduleRun(BaseModel):
    """One run of a personal schedule."""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., alias="runId")
    schedule_id: str = Field(..., alias="scheduleId")
    status: str
    item_count: int = Field(0, alias="itemCount")
    session_id: Optional[str] = Field(None, alias="sessionId")
    finished_at: str = Field(..., alias="finishedAt")
    input_tokens: int = Field(0, alias="inputTokens")
    output_tokens: int = Field(0, alias="outputTokens")
    cache_read_tokens: int = Field(0, alias="cacheReadTokens")

    @field_validator("item_count", "input_tokens", "output_tokens", "cache_read_tokens", mode="before")
    @classmethod
    def _int(cls, value: object) -> int:
        # DynamoDB returns numbers as Decimal.
        return int(value) if value is not None else 0


def _token_counts(usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Input, output and cache-read tokens from a ``RunResult.usage``.

    The harness keeps the stream's ``metadata_summary`` payload, so the counts
    sit under ``usage``; a flat dict of the same keys is read as well.
    """
    if not isinstance(usage, dict):
        usage = {}
    inner = usage.get("usage") if isinstance(usage.get("usage"), dict) else usage

    def count(*keys: str) -> int:
        for key in keys:
            value = inner.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
        return 0

    return {
        "inputTokens": count("inputTokens", "input_tokens"),
        "outputTokens": count("outputTokens", "output_tokens"),
        "cacheReadTokens": count("cacheReadInputTokens", "cache_read_input_tokens"),
    }


def record_schedule_run(
    schedule: ScheduledPrompt,
    *,
    run_id: str,
    status: ScheduleRunStatus,
    session_id: Optional[str],
    finished_at: str,
    item_count: int = 0,
    usage: Optional[Dict[str, Any]] = None,
) -> None:
    """Write one run row for a personal schedule. Never raises: a failure is logged.

    Writes nothing for a project schedule, whose runs are on the project.
    """
    if schedule.project_id:
        return
    try:
        item: Dict[str, Any] = {
            "PK": f"USER#{schedule.user_id}",
            "SK": f"{_run_prefix(schedule.schedule_id)}{finished_at}#{run_id}",
            "runId": run_id,
            "scheduleId": schedule.schedule_id,
            "status": status,
            "itemCount": int(item_count),
            "finishedAt": finished_at,
            **_token_counts(usage),
            "ttl": int(time.time()) + RUN_TTL_SECONDS,
        }
        if session_id:
            item["sessionId"] = session_id
        _service._get_table().put_item(Item=item)
    except Exception:
        logger.warning(
            "Could not record run %s of schedule %s", run_id, schedule.schedule_id, exc_info=True
        )


def list_schedule_runs(user_id: str, schedule_id: str, limit: int = DEFAULT_RUNS_LIMIT) -> List[ScheduleRun]:
    """The schedule's runs, newest first, at most ``limit``."""
    from boto3.dynamodb.conditions import Key

    response = _service._get_table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with(_run_prefix(schedule_id)),
        ScanIndexForward=False,
        Limit=limit,
    )
    runs: List[ScheduleRun] = []
    for item in response.get("Items", []):
        try:
            runs.append(ScheduleRun.model_validate(item))
        except Exception as e:
            logger.warning(f"Failed to parse schedule run item: {e}")
    return runs
