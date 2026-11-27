"""Run history for personal schedules (docs/specs/quiet-watcher.md §3.4).

The scheduled-runs worker writes one row per run of every personal schedule,
standard or watch, while ``PROACTIVE_RUNS_ENABLED`` is on::

    PK: USER#{user_id} | SK: SCHEDRUN#{schedule_id}#{finished_at}#{run_id}

in the sessions-metadata table, expiring after 30 days through the table's
``ttl`` attribute. A project schedule's runs are on the project instead
(``ProjectSchedules.record_run``), so they get no row here.

Tokens are kept, and dollars too (docs/specs/proactive-trust.md §4.1): ``costUsd``
is the turn's cost from its final ``metadata`` frame, which arrives on the stream
the worker reads, so nothing waits on the ``C#`` rows the Runtime writes after
``done``. A run whose frame never came is ``costPending`` instead, and the
schedule's next run fills ``costUsd`` in from the session's chat-call ``C#`` rows
(``budget.reconcile_pending_costs``). An inbox resume adds its cost, as it adds
its tokens.

Blocking, like ``watch_state``: callers use ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
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
#: ``needs_user`` paused on the user (docs/specs/answer-from-inbox.md §3.8): it
#: carries the pause's ``expiresAt`` and ``promptCount``, and reads as expired
#: once ``expiresAt`` passes with no resolution.
ScheduleRunStatus = Literal["completed", "quiet", "notify", "error", "timeout", "paused", "needs_user"]


def _run_prefix(schedule_id: str) -> str:
    return f"{RUN_SK_PREFIX}{schedule_id}#"


def _run_key(user_id: str, schedule_id: str, finished_at: str, run_id: str) -> Dict[str, str]:
    return {"PK": f"USER#{user_id}", "SK": f"{_run_prefix(schedule_id)}{finished_at}#{run_id}"}


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
    #: What the run wrote to the prompt cache, at the write premium: on a short
    #: watcher run this is most of the cost, so a row without it reads cheap.
    cache_write_tokens: int = Field(0, alias="cacheWriteTokens")
    #: A ``needs_user`` run's pause: when it expires and how many prompts it holds.
    expires_at: Optional[str] = Field(None, alias="expiresAt")
    prompt_count: int = Field(0, alias="promptCount")
    #: How a ``needs_user`` run's pause was answered from the inbox (answer-from-inbox §3.8):
    #: ``approved``, ``declined``, ``answered`` or ``skipped``, or ``answered_elsewhere`` when
    #: the Runtime found it already answered. Unset while it waits, and after an answer given
    #: in the conversation, which nothing reads.
    resolution: Optional[str] = None
    #: What the resume did: ``finished``, ``waiting_again`` or ``failed``.
    resume_outcome: Optional[str] = Field(None, alias="resumeOutcome")
    resume_run_id: Optional[str] = Field(None, alias="resumeRunId")
    resumed_at: Optional[str] = Field(None, alias="resumedAt")
    #: A watcher's shadow triage (quiet-watcher triage §6.5): ``{verdict, reason, tier?, ms?}``,
    #: ``verdict`` one of ``quiet``, ``escalate`` or ``skipped``. Absent on every other run.
    triage: Optional[Dict[str, Any]] = None
    #: What the run cost in USD (the main agent's calls; a resume's added). ``None`` while
    #: ``costPending``, and on rows written before budgets.
    cost_usd: Optional[float] = Field(None, alias="costUsd")
    #: The run's cost never arrived; the schedule's next run fills it in.
    cost_pending: bool = Field(False, alias="costPending")

    @field_validator("cost_usd", mode="before")
    @classmethod
    def _usd(cls, value: object) -> Optional[float]:
        return float(value) if value is not None else None

    @field_validator(
        "item_count", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "prompt_count",
        mode="before",
    )
    @classmethod
    def _int(cls, value: object) -> int:
        # DynamoDB returns numbers as Decimal.
        return int(value) if value is not None else 0

    @field_validator("triage", mode="before")
    @classmethod
    def _triage(cls, value: object) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict):
            return None
        # Enums stay strings; ``tier`` and ``ms`` come back from DynamoDB as Decimal.
        return {k: (int(v) if k in ("tier", "ms") and v is not None else v) for k, v in value.items()}


def _token_counts(usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Input, output, cache-read and cache-write tokens from a ``RunResult.usage``.

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
        "cacheWriteTokens": count("cacheWriteInputTokens", "cache_write_input_tokens"),
    }


def _usd_decimal(value: Optional[float]) -> Optional[Decimal]:
    """A cost as the ``Decimal`` DynamoDB stores, at the budget's precision."""
    from .budget import to_usd

    return to_usd(value)


def record_schedule_run(
    schedule: ScheduledPrompt,
    *,
    run_id: str,
    status: ScheduleRunStatus,
    session_id: Optional[str],
    finished_at: str,
    item_count: int = 0,
    usage: Optional[Dict[str, Any]] = None,
    expires_at: Optional[str] = None,
    prompt_count: int = 0,
    triage: Optional[Dict[str, Any]] = None,
    cost_usd: Optional[float] = None,
    cost_pending: bool = False,
) -> None:
    """Write one run row for a personal schedule. Never raises: a failure is logged.

    Writes nothing for a project schedule, whose runs are on the project. ``triage`` is a
    watcher's shadow verdict (quiet-watcher triage §6.5), enums and a duration only.
    ``cost_usd`` is the run's cost; ``cost_pending`` marks a run whose cost never arrived.
    """
    if schedule.project_id:
        return
    try:
        item: Dict[str, Any] = {
            **_run_key(schedule.user_id, schedule.schedule_id, finished_at, run_id),
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
        if expires_at:
            item["expiresAt"] = expires_at
        if prompt_count:
            item["promptCount"] = int(prompt_count)
        if triage:
            item["triage"] = dict(triage)
        cost = _usd_decimal(cost_usd)
        if cost is not None:
            item["costUsd"] = cost
        elif cost_pending:
            item["costPending"] = True
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


def record_schedule_run_resume(
    user_id: str,
    schedule_id: str,
    *,
    run_finished_at: str,
    run_id: str,
    resolution: str,
    resume_run_id: str,
    resumed_at: str,
    resume_outcome: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
    expires_at: Optional[str] = None,
    prompt_count: Optional[int] = None,
    cost_usd: Optional[float] = None,
) -> bool:
    """Record an inbox answer on the paused run's own row (answer-from-inbox §3.8).

    The resume's tokens and ``costUsd`` are **added**, so a run's cost stays one row. A resume that waits on the
    user again sets the new pause's ``expiresAt`` and ``promptCount``; one that did not clears
    ``expiresAt``, so the row never reads as expired after it was answered. Writes only over a
    row that exists (one past its 30-day TTL is left gone). Never raises; False when nothing
    was written.
    """
    counts = _token_counts(usage)
    names: Dict[str, str] = {"#res": "resolution"}
    values: Dict[str, Any] = {":res": resolution, ":rid": resume_run_id, ":at": resumed_at}
    sets = ["#res = :res", "resumeRunId = :rid", "resumedAt = :at"]
    removes: List[str] = []
    if resume_outcome:
        sets.append("resumeOutcome = :out")
        values[":out"] = resume_outcome
    if expires_at:
        sets.append("expiresAt = :exp")
        values[":exp"] = expires_at
    else:
        removes.append("expiresAt")
    if prompt_count is not None:
        sets.append("promptCount = :pc")
        values[":pc"] = int(prompt_count)
    adds = []
    for name, count in counts.items():
        if count:
            adds.append(f"{name} :{name}")
            values[f":{name}"] = count
    cost = _usd_decimal(cost_usd)
    if cost:
        adds.append("costUsd :costUsd")
        values[":costUsd"] = cost
    update = "SET " + ", ".join(sets)
    if adds:
        update += " ADD " + ", ".join(adds)
    if removes:
        update += " REMOVE " + ", ".join(removes)
    try:
        _service._get_table().update_item(
            Key=_run_key(user_id, schedule_id, run_finished_at, run_id),
            UpdateExpression=update,
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True
    except Exception:
        logger.warning("Could not record the answer on run %s of schedule %s", run_id, schedule_id, exc_info=True)
        return False


def record_schedule_run_answered_elsewhere(
    user_id: str, schedule_id: str, *, run_finished_at: str, run_id: str
) -> bool:
    """Mark a paused run answered in its conversation (answer-from-inbox §3.6).

    That resume goes through ``/chat/stream`` and nothing reads its outcome, so the history row
    learns of it only when the inbox list finds the pause gone. Sets ``resolution`` and clears
    ``expiresAt``, so the row stops reading as waiting and never reads as expired. Writes only
    over a row that exists and that no inbox answer has resolved. Blocking; never raises. False
    when nothing was written.
    """
    try:
        _service._get_table().update_item(
            Key=_run_key(user_id, schedule_id, run_finished_at, run_id),
            UpdateExpression="SET #res = :res REMOVE expiresAt",
            ConditionExpression="attribute_exists(PK) AND attribute_not_exists(#res)",
            ExpressionAttributeNames={"#res": "resolution"},
            ExpressionAttributeValues={":res": "answered_elsewhere"},
        )
        return True
    except Exception as e:  # noqa: BLE001 - a lost condition is the usual case, not an error
        code = getattr(e, "response", {}).get("Error", {}).get("Code")
        if code != "ConditionalCheckFailedException":
            logger.warning(
                "Could not mark run %s of schedule %s answered in its conversation", run_id, schedule_id,
                exc_info=True,
            )
        return False
