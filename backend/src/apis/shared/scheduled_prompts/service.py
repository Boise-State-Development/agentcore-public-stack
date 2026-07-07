"""Scheduled-prompt repository — DynamoDB storage for scheduled agent runs.

Lives in apis.shared because it will have (eventually) three independent
consumers: app-api (this PR's CRUD routes), the B2 dispatcher Lambda (due
sweep), and the B2 worker Lambda (run bookkeeping). B1 only wires the first.

Storage (sessions-metadata table, adjacency list):
    PK: USER#{user_id} | SK: SCHEDPROMPT#{schedule_id}
    DueScheduleIndex (sparse): GSI3_PK = "SCHEDDUE", GSI3_SK = "{next_run_at}#{schedule_id}"
    DueScheduleIndex keys exist only while state == "active".

Cadence -> next_run_at is computed here, timezone-aware, so the (future)
dispatcher stays a dumb "who's due" query — no cron strings in the engine
(docs/specs/scheduled-agent-runs.md §5).
"""

import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Union
from zoneinfo import ZoneInfo

from .models import DUE_INDEX_PK, IntervalUnit, ScheduleCadence, ScheduledPrompt, ScheduledPromptState

logger = logging.getLogger(__name__)

#: Floor for the custom "every N" cadence. Below this a schedule fires faster
#: than the runaway guard (``max_runs_per_day``) tolerates and just self-pauses;
#: 15 minutes keeps the smallest interval useful without inviting a hot loop.
MIN_INTERVAL_MINUTES = 15


class _Unset:
    """Singleton sentinel meaning "argument not provided".

    Distinct from ``None``, which on the *clearable* update fields
    (``assistant_id`` / ``enabled_tools``) means "clear this field". A plain
    ``None`` default cannot tell "leave unchanged" from "clear".
    """

    _instance: Optional["_Unset"] = None

    def __new__(cls) -> "_Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "UNSET"


#: Sentinel for update_scheduled_prompt's clearable fields (see _Unset).
UNSET = _Unset()

DEFAULT_MAX_SCHEDULES_PER_USER = 20

_WEEKDAY_CADENCES = {"weekday"}  # Monday-Friday


class ScheduledPromptLimitExceeded(Exception):
    """Raised when a user already has the maximum number of schedules."""


def _iso(dt: datetime) -> str:
    """Serialize a UTC datetime as strict ISO 8601 with a ``Z`` suffix.

    ``datetime.isoformat()`` renders the offset as ``+00:00``; normalize to
    ``Z`` so the result is valid ISO 8601 that JavaScript's ``Date`` parses
    (matches the sync_policies house style — see that module's docstring for
    the Safari ``Invalid Date`` history).
    """
    return dt.isoformat().replace("+00:00", "Z")


def _get_current_timestamp() -> str:
    return _iso(datetime.now(timezone.utc))


def _generate_schedule_id() -> str:
    return f"sched-{uuid.uuid4().hex[:12]}"


def _table_name() -> str:
    table = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    if not table:
        raise RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME environment variable is required")
    return table


def _get_table():
    import boto3

    return boto3.resource("dynamodb").Table(_table_name())


def _due_sort_key(next_run_at: str, schedule_id: str) -> str:
    return f"{next_run_at}#{schedule_id}"


def max_schedules_per_user() -> int:
    return int(os.environ.get("SCHEDULED_RUNS_MAX_PER_USER", DEFAULT_MAX_SCHEDULES_PER_USER))


def interval_to_minutes(value: Optional[int], unit: Optional[IntervalUnit]) -> Optional[int]:
    """Canonicalize an interval value+unit to whole minutes.

    Returns ``None`` when either half is missing so callers can pass a
    schedule's interval fields through unconditionally (a non-interval
    schedule carries ``None`` for both).
    """
    if value is None or unit is None:
        return None
    return value * 60 if unit == "hours" else value


def compute_next_run_at(
    cadence: ScheduleCadence,
    hour_local: int,
    timezone_name: str,
    weekday: Optional[int] = None,
    from_time: Optional[datetime] = None,
    interval_minutes: Optional[int] = None,
) -> str:
    """Compute the next due timestamp (ISO 8601 UTC) for a cadence.

    Timezone-aware: the schedule's ``hour_local``/``weekday`` are interpreted
    in ``timezone_name`` (IANA), then converted to UTC. Always returns a
    time strictly after ``from_time`` (defaults to now) — a schedule created
    at 9:05am local for "daily at 9am" fires tomorrow, not immediately.

    ``weekday`` is 0=Monday..6=Sunday (``datetime.weekday()`` convention) and
    is required for cadence == "weekly"; ignored otherwise. ``interval_minutes``
    is required for cadence == "interval" (a plain delta from ``from_time`` —
    no wall-clock or timezone anchor); ignored otherwise.
    """
    if cadence == "interval":
        if interval_minutes is None or interval_minutes <= 0:
            raise ValueError("interval_minutes is required and must be positive when cadence == 'interval'")
        base_utc = (from_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return _iso(base_utc + timedelta(minutes=interval_minutes))

    tz = ZoneInfo(timezone_name)
    base = (from_time or datetime.now(timezone.utc)).astimezone(tz)

    candidate = base.replace(hour=hour_local, minute=0, second=0, microsecond=0)

    if cadence == "daily":
        if candidate <= base:
            candidate += timedelta(days=1)
    elif cadence == "weekday":
        if candidate <= base:
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:  # Sat=5, Sun=6
            candidate += timedelta(days=1)
    elif cadence == "weekly":
        if weekday is None:
            raise ValueError("weekday is required when cadence == 'weekly'")
        days_ahead = (weekday - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= base:
            candidate += timedelta(days=7)
    else:
        raise ValueError(f"Unknown cadence: {cadence}")

    return _iso(candidate.astimezone(timezone.utc))


async def create_scheduled_prompt(
    user_id: str,
    label: str,
    prompt_text: str,
    cadence: ScheduleCadence,
    hour_local: int,
    timezone_name: str,
    weekday: Optional[int] = None,
    interval_value: Optional[int] = None,
    interval_unit: Optional[IntervalUnit] = None,
    assistant_id: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    deliver_email: bool = False,
) -> ScheduledPrompt:
    """Create an active scheduled prompt, due at the next occurrence of its cadence.

    Enforces the per-user schedule cap (bounded by the cap, so the list scan
    to check it is cheap). ``enabled_tools`` is a snapshot — the caller
    passes the RBAC-resolved tool set at creation time; it is never
    re-resolved lazily at fire time (Phase A punch #7).
    """
    existing = await list_scheduled_prompts(user_id)
    if len(existing) >= max_schedules_per_user():
        raise ScheduledPromptLimitExceeded(
            f"User {user_id} already has {len(existing)} scheduled prompts (max {max_schedules_per_user()})"
        )

    now = _get_current_timestamp()
    next_run_at = compute_next_run_at(
        cadence,
        hour_local,
        timezone_name,
        weekday=weekday,
        interval_minutes=interval_to_minutes(interval_value, interval_unit),
    )
    schedule = ScheduledPrompt(
        schedule_id=_generate_schedule_id(),
        user_id=user_id,
        assistant_id=assistant_id,
        label=label,
        prompt_text=prompt_text,
        cadence=cadence,
        hour_local=hour_local,
        weekday=weekday,
        interval_value=interval_value,
        interval_unit=interval_unit,
        timezone=timezone_name,
        state="active",
        next_run_at=next_run_at,
        runs_today=0,
        runs_today_date=None,
        enabled_tools=enabled_tools,
        deliver_email=deliver_email,
        created_at=now,
        updated_at=now,
    )

    item = schedule.model_dump(by_alias=True, exclude_none=True)
    item["PK"] = f"USER#{user_id}"
    item["SK"] = f"SCHEDPROMPT#{schedule.schedule_id}"
    item["GSI3_PK"] = DUE_INDEX_PK
    item["GSI3_SK"] = _due_sort_key(next_run_at, schedule.schedule_id)

    _get_table().put_item(Item=item)
    logger.info(f"Created scheduled prompt {schedule.schedule_id} ({cadence}) for user {user_id}")
    return schedule


async def get_scheduled_prompt(user_id: str, schedule_id: str) -> Optional[ScheduledPrompt]:
    response = _get_table().get_item(Key={"PK": f"USER#{user_id}", "SK": f"SCHEDPROMPT#{schedule_id}"})
    item = response.get("Item")
    return ScheduledPrompt.model_validate(item) if item else None


async def list_scheduled_prompts(user_id: str) -> List[ScheduledPrompt]:
    from boto3.dynamodb.conditions import Key

    response = _get_table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("SCHEDPROMPT#")
    )
    schedules = []
    for item in response.get("Items", []):
        try:
            schedules.append(ScheduledPrompt.model_validate(item))
        except Exception as e:
            logger.warning(f"Failed to parse scheduled prompt item: {e}")
    return schedules


async def list_due_schedules(now: Optional[str] = None, limit: int = 20) -> List[ScheduledPrompt]:
    """Query the sparse DueScheduleIndex for active schedules whose next_run_at has passed.

    Returns schedules most-overdue first. Paused schedules have no GSI keys
    and are physically absent from this index. Not called by anything yet
    in B1 — this is the exact query shape B2's dispatcher will use.
    """
    from boto3.dynamodb.conditions import Key

    now = now or _get_current_timestamp()
    response = _get_table().query(
        IndexName="DueScheduleIndex",
        # '~' sorts after '#' and all timestamp characters, so this covers
        # every "{ts}#{schedule_id}" key with ts <= now.
        KeyConditionExpression=Key("GSI3_PK").eq(DUE_INDEX_PK) & Key("GSI3_SK").lte(f"{now}~"),
        Limit=limit,
        ScanIndexForward=True,
    )
    schedules = []
    for item in response.get("Items", []):
        try:
            schedules.append(ScheduledPrompt.model_validate(item))
        except Exception as e:
            logger.warning(f"Failed to parse due scheduled prompt item: {e}")
    return schedules


async def set_schedule_state(
    user_id: str,
    schedule_id: str,
    state: ScheduledPromptState,
    next_run_at: Optional[str] = None,
    state_reason: Optional[str] = None,
) -> bool:
    """Transition a schedule's lifecycle state.

    Transition to "active" requires next_run_at and (re)adds the GSI keys;
    any paused state REMOVEs them so the schedule drops out of
    DueScheduleIndex. Returns False if the schedule does not exist.
    """
    from botocore.exceptions import ClientError

    if state == "active" and not next_run_at:
        raise ValueError("next_run_at is required when activating a scheduled prompt")

    names = {"#state": "state"}
    values = {":state": state, ":updated_at": _get_current_timestamp()}
    set_parts = ["#state = :state", "updatedAt = :updated_at"]
    remove_parts = []

    if state == "active":
        set_parts += ["nextRunAt = :next", "GSI3_PK = :gsipk", "GSI3_SK = :gsisk"]
        values[":next"] = next_run_at
        values[":gsipk"] = DUE_INDEX_PK
        values[":gsisk"] = _due_sort_key(next_run_at, schedule_id)
        remove_parts.append("stateReason")
    else:
        remove_parts += ["GSI3_PK", "GSI3_SK"]
        if state_reason:
            set_parts.append("stateReason = :reason")
            values[":reason"] = state_reason

    expression = "SET " + ", ".join(set_parts)
    if remove_parts:
        expression += " REMOVE " + ", ".join(remove_parts)

    try:
        _get_table().update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"SCHEDPROMPT#{schedule_id}"},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(PK)",
        )
        logger.info(f"Scheduled prompt {schedule_id} -> {state}" + (f" ({state_reason})" if state_reason else ""))
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.warning(f"Cannot set state on missing scheduled prompt {schedule_id}")
            return False
        raise


async def rearm_schedule(
    user_id: str,
    schedule_id: str,
    expected_next_run_at: str,
    new_next_run_at: str,
) -> bool:
    """Advance next_run_at, conditional on the currently stored value.

    Mirrors ``sync_policies.rearm_policy``: the (future) B2 dispatcher
    re-arms BEFORE invoking the worker, so a double-fired tick is idempotent
    — the second dispatcher loses the conditional write and skips the
    schedule. Returns True if this caller won. Not called by anything yet
    in B1; exercised directly by tests so B2 needs no behavior change here.
    """
    from botocore.exceptions import ClientError

    try:
        _get_table().update_item(
            Key={"PK": f"USER#{user_id}", "SK": f"SCHEDPROMPT#{schedule_id}"},
            UpdateExpression="SET nextRunAt = :new, GSI3_SK = :gsisk, updatedAt = :updated_at",
            ExpressionAttributeValues={
                ":new": new_next_run_at,
                ":gsisk": _due_sort_key(new_next_run_at, schedule_id),
                ":updated_at": _get_current_timestamp(),
                ":expected": expected_next_run_at,
            },
            ConditionExpression="nextRunAt = :expected",
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info(f"Scheduled prompt {schedule_id} already re-armed by another dispatcher; skipping")
            return False
        raise


async def record_run_result(
    user_id: str,
    schedule_id: str,
    status: str,
    session_id: Optional[str] = None,
    error: Optional[str] = None,
) -> int:
    """Record a run's outcome; return the new consecutive-failure streak.

    Bumps the runaway-guard counter (``runs_today``/``runs_today_date``,
    reset on a UTC date rollover) and maintains ``consecutive_failures`` — a
    ``"completed"`` run resets it to 0, any other status increments it
    (mirrors ``sync_policies.update_sync_result``). The B2 worker uses the
    returned streak to trip the repeated-failure breaker at any threshold;
    the dispatcher reads ``runs_today`` to auto-pause a schedule that exceeds
    ``max_runs_per_day``.
    """
    today = date.today().isoformat()
    existing = await get_scheduled_prompt(user_id, schedule_id)
    runs_today = 1
    if existing is not None and existing.runs_today_date == today:
        runs_today = existing.runs_today + 1

    prior_failures = existing.consecutive_failures if existing is not None else 0
    consecutive_failures = 0 if status == "completed" else prior_failures + 1

    now = _get_current_timestamp()
    values = {
        ":now": now,
        ":status": status,
        ":runs_today": runs_today,
        ":today": today,
        ":cf": consecutive_failures,
    }
    set_parts = [
        "lastRunAt = :now",
        "lastRunStatus = :status",
        "updatedAt = :now",
        "runsToday = :runs_today",
        "runsTodayDate = :today",
        "consecutiveFailures = :cf",
    ]
    if session_id is not None:
        set_parts.append("lastRunSessionId = :session_id")
        values[":session_id"] = session_id
    if error is not None:
        set_parts.append("lastError = :error")
        values[":error"] = error

    _get_table().update_item(
        Key={"PK": f"USER#{user_id}", "SK": f"SCHEDPROMPT#{schedule_id}"},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeValues=values,
    )
    return consecutive_failures


async def update_scheduled_prompt(
    user_id: str,
    schedule_id: str,
    *,
    label: Optional[str] = None,
    prompt_text: Optional[str] = None,
    cadence: Optional[ScheduleCadence] = None,
    hour_local: Optional[int] = None,
    weekday: Optional[int] = None,
    interval_value: Optional[int] = None,
    interval_unit: Optional[IntervalUnit] = None,
    timezone_name: Optional[str] = None,
    assistant_id: Union[str, None, _Unset] = UNSET,
    enabled_tools: Union[List[str], None, _Unset] = UNSET,
    deliver_email: Optional[bool] = None,
) -> Optional[ScheduledPrompt]:
    """Edit a schedule's fields in place.

    Any of ``cadence``/``hour_local``/``weekday``/``timezone_name`` changes
    the due time: for an *active* schedule this recomputes and re-arms
    ``next_run_at`` (and its GSI key) in the same write; a paused schedule
    just remembers the new cadence for when it resumes (mirrors
    ``sync_policies.change_policy_interval``). Returns None if the schedule
    does not exist.

    ``assistant_id`` and ``enabled_tools`` are *clearable*: pass ``UNSET``
    (the default) to leave them untouched, a value to set them, or ``None``
    to clear them (the attribute is removed — the schedule falls back to the
    default agent / all-RBAC-allowed tools). All other fields keep the plain
    ``None`` == "leave unchanged" contract.
    """
    schedule = await get_scheduled_prompt(user_id, schedule_id)
    if schedule is None:
        return None

    new_cadence = cadence if cadence is not None else schedule.cadence
    new_hour_local = hour_local if hour_local is not None else schedule.hour_local
    new_weekday = weekday if weekday is not None else schedule.weekday
    new_interval_value = interval_value if interval_value is not None else schedule.interval_value
    new_interval_unit = interval_unit if interval_unit is not None else schedule.interval_unit
    new_timezone = timezone_name if timezone_name is not None else schedule.timezone

    cadence_changed = (
        new_cadence != schedule.cadence
        or new_hour_local != schedule.hour_local
        or new_weekday != schedule.weekday
        or new_interval_value != schedule.interval_value
        or new_interval_unit != schedule.interval_unit
        or new_timezone != schedule.timezone
    )

    names: dict = {}
    values = {":updated_at": _get_current_timestamp()}
    set_parts = ["updatedAt = :updated_at"]

    if cadence_changed:
        names["#tz"] = "timezone"
        set_parts += ["cadence = :cadence", "hourLocal = :hour_local", "#tz = :timezone"]
        values[":cadence"] = new_cadence
        values[":hour_local"] = new_hour_local
        values[":timezone"] = new_timezone
        if new_weekday is not None:
            set_parts.append("weekday = :weekday")
            values[":weekday"] = new_weekday
        if new_interval_value is not None:
            set_parts.append("intervalValue = :interval_value")
            values[":interval_value"] = new_interval_value
        if new_interval_unit is not None:
            set_parts.append("intervalUnit = :interval_unit")
            values[":interval_unit"] = new_interval_unit

        if schedule.state == "active":
            next_run_at = compute_next_run_at(
                new_cadence,
                new_hour_local,
                new_timezone,
                weekday=new_weekday,
                interval_minutes=interval_to_minutes(new_interval_value, new_interval_unit),
            )
            set_parts += ["nextRunAt = :next", "GSI3_PK = :gsipk", "GSI3_SK = :gsisk"]
            values[":next"] = next_run_at
            values[":gsipk"] = DUE_INDEX_PK
            values[":gsisk"] = _due_sort_key(next_run_at, schedule_id)

    # Non-clearable fields: None == "leave unchanged".
    for attr, value in (("label", label), ("promptText", prompt_text), ("deliverEmail", deliver_email)):
        if value is not None:
            set_parts.append(f"{attr} = :{attr}")
            values[f":{attr}"] = value

    # Clearable fields: UNSET == leave, None == clear (REMOVE the attribute),
    # any other value == set.
    remove_parts: List[str] = []
    for attr, value in (("assistantId", assistant_id), ("enabledTools", enabled_tools)):
        if isinstance(value, _Unset):
            continue
        if value is None:
            remove_parts.append(attr)
        else:
            set_parts.append(f"{attr} = :{attr}")
            values[f":{attr}"] = value

    update_expression = "SET " + ", ".join(set_parts)
    if remove_parts:
        update_expression += " REMOVE " + ", ".join(remove_parts)

    update_kwargs = {
        "Key": {"PK": f"USER#{user_id}", "SK": f"SCHEDPROMPT#{schedule_id}"},
        "UpdateExpression": update_expression,
        "ExpressionAttributeValues": values,
    }
    if names:
        update_kwargs["ExpressionAttributeNames"] = names
    _get_table().update_item(**update_kwargs)

    return await get_scheduled_prompt(user_id, schedule_id)


async def delete_scheduled_prompt(user_id: str, schedule_id: str) -> bool:
    """Delete = total revocation. No orphan timers: the row's absence is
    the only signal the (future) dispatcher needs — there is nothing else
    to clean up."""
    _get_table().delete_item(Key={"PK": f"USER#{user_id}", "SK": f"SCHEDPROMPT#{schedule_id}"})
    logger.info(f"Deleted scheduled prompt {schedule_id} for user {user_id}")
    return True
