"""A schedule's daily budget (docs/specs/proactive-trust.md §4.1–§4.4, PR 4.1).

A personal schedule may carry ``maxCostPerDay`` (USD), and one whose interval is
under 60 minutes must (``requires_budget``). Every part of it runs only while
``PROACTIVE_RUNS_ENABLED`` is on, and none of it is on the interactive turn path:

- **The count.** After every scheduled run and every inbox resume, the worker adds
  the run's cost (``RunResult.cost_usd``, from the turn's final ``metadata`` frame)
  to ``spendToday`` with one conditional ``UpdateItem`` (``add_spend``). The day is
  the schedule's own, in its ``timezone`` (§12 Q3); ``runsToday`` stays UTC.
- **The trip.** When ``spendToday`` reaches ``maxCostPerDay`` the schedule is held
  until its next local midnight (``budgetHoldUntil``, ``nextRunAt`` re-armed past it)
  with one ``fyi`` ``schedule_paused`` bell row (``reason: "budget"``, with
  ``resumesAt``). The third consecutive day at the limit pauses it instead
  (``paused_error``, ``stateReason: "budget"``) until its owner resumes it (§12 Q2).
- **The streak.** ``budgetDaysAtLimit`` counts consecutive days. Yesterday was at the
  limit exactly when the last hold ended at today's local midnight, so a day that
  ends under budget breaks the streak with no write of its own. A raised budget
  releases the hold, which also ends the streak.
- **A run with no cost frame** (a timeout, a cut stream, a provider error) leaves its
  history row ``costPending`` and bumps ``costPendingRuns`` on the schedule. The next
  run fills it in from the run session's chat-call ``C#`` rows
  (``reconcile_pending_costs``), and adds it to the day it ran if that is still today.

The dispatcher's guard 2b (``budget_hold_end``) is the backstop before a run: it
honours the hold, and holds a schedule whose budget was lowered below today's spend.

Blocking DynamoDB calls, like ``runs``; async callers use ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from . import service as _service
from .models import ScheduledPrompt

logger = logging.getLogger(__name__)

#: An interval under this many minutes must have a budget (§4.4).
SHORT_INTERVAL_MINUTES = 60
#: The default budget for a schedule under 60 minutes, when the env var is unset (§12 Q1).
DEFAULT_MAX_COST_PER_DAY = Decimal("0.50")
#: Consecutive days at the limit after which the schedule pauses instead of holding.
PAUSE_AFTER_DAYS_AT_LIMIT = 3
#: The pause and bell reason for a spent budget.
BUDGET_REASON = "budget"
#: The most run rows the reconcile reads: the newest, where a pending one is.
RECONCILE_SCAN_LIMIT = 20
#: Dollars are stored to this precision, well past a cent.
_QUANTUM = Decimal("0.00000001")


def utc_now() -> datetime:
    """Now, in UTC. One place, so a test can move the budget's clock across local midnight."""
    return datetime.now(timezone.utc)


def default_max_cost_per_day() -> Decimal:
    """``SCHEDULE_DEFAULT_MAX_COST_PER_DAY`` (app-api), or $0.50. A bad value logs and falls back."""
    raw = (os.environ.get("SCHEDULE_DEFAULT_MAX_COST_PER_DAY") or "").strip()
    if not raw:
        return DEFAULT_MAX_COST_PER_DAY
    try:
        value = Decimal(raw)
    except InvalidOperation:
        logger.warning("SCHEDULE_DEFAULT_MAX_COST_PER_DAY=%r is not a number; using %s", raw, DEFAULT_MAX_COST_PER_DAY)
        return DEFAULT_MAX_COST_PER_DAY
    if not value.is_finite() or value <= 0:
        logger.warning("SCHEDULE_DEFAULT_MAX_COST_PER_DAY=%r is not positive; using %s", raw, DEFAULT_MAX_COST_PER_DAY)
        return DEFAULT_MAX_COST_PER_DAY
    return value


def requires_budget(cadence: Optional[str], interval_minutes: Optional[int]) -> bool:
    """Whether a schedule with this cadence must have a budget: an interval under 60 minutes."""
    return cadence == "interval" and interval_minutes is not None and 0 < interval_minutes < SHORT_INTERVAL_MINUTES


def schedule_requires_budget(schedule: ScheduledPrompt) -> bool:
    return requires_budget(
        schedule.cadence, _service.interval_to_minutes(schedule.interval_value, schedule.interval_unit)
    )


def to_usd(value: Any) -> Optional[Decimal]:
    """A dollar amount as a ``Decimal`` at storage precision; ``None`` for anything not a finite number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite():
        return None
    return amount.quantize(_QUANTUM)


def usd_text(value: Optional[Decimal]) -> str:
    """``"0.52"``: a bell payload's amount, to the cent, as quota rows send theirs."""
    return f"{(value or Decimal(0)).quantize(Decimal('0.01'))}"


# --------------------------------------------------------------------------- days


def _zone(schedule_tz: str):
    return _service._zone(schedule_tz)


def local_date(schedule_tz: str, now: datetime) -> str:
    """``YYYY-MM-DD`` of ``now`` in the schedule's timezone: the budget's day."""
    return now.astimezone(_zone(schedule_tz)).date().isoformat()


def local_day_start(schedule_tz: str, now: datetime) -> str:
    """The local midnight that began ``now``'s day, as ISO UTC (the shape ``budgetHoldUntil`` is stored in)."""
    local = now.astimezone(_zone(schedule_tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return _service._iso(start.astimezone(timezone.utc))


def next_local_midnight(schedule_tz: str, now: datetime) -> str:
    """The next local midnight after ``now``, as ISO UTC. DST-safe: computed on the local calendar."""
    tz = _zone(schedule_tz)
    local = now.astimezone(tz)
    tomorrow = (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).replace(tzinfo=None)
    return _service._iso(tomorrow.replace(tzinfo=tz).astimezone(timezone.utc))


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def later(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """The later of two ISO UTC times; either may be ``None``."""
    pa, pb = _parse(a), _parse(b)
    if pa is None:
        return b
    if pb is None:
        return a
    return a if pa >= pb else b


def spend_for_today(schedule: ScheduledPrompt, now: datetime) -> Decimal:
    """``spendToday`` if it counts today's local date, else 0 (the day rolled over unspent)."""
    if schedule.spend_today_date == local_date(schedule.timezone, now):
        return to_usd(schedule.spend_today) or Decimal(0)
    return Decimal(0)


def active_hold(schedule: ScheduledPrompt, now: datetime) -> Optional[str]:
    """``budgetHoldUntil`` while it is still ahead of ``now``, else ``None``."""
    until = _parse(schedule.budget_hold_until)
    if until is not None and until > now:
        return schedule.budget_hold_until
    return None


def budget_hold_end(schedule: ScheduledPrompt, now: datetime) -> Optional[str]:
    """When an over-budget schedule may next run, or ``None`` when it may run now (guard 2b).

    The hold a trip set, or, for a budget lowered below today's spend, the next local
    midnight. A personal schedule only.
    """
    if schedule.project_id:
        return None
    hold = active_hold(schedule, now)
    if hold:
        return hold
    budget = to_usd(schedule.max_cost_per_day)
    if budget is not None and spend_for_today(schedule, now) >= budget:
        return next_local_midnight(schedule.timezone, now)
    return None


def respect_hold(schedule: ScheduledPrompt, next_run_at: str, now: datetime) -> str:
    """An edit's or a resume's new ``nextRunAt``, never before the schedule's budget hold ends."""
    return later(next_run_at, active_hold(schedule, now)) or next_run_at


# --------------------------------------------------------------------------- the count


def _key(user_id: str, schedule_id: str) -> Dict[str, str]:
    return {"PK": f"USER#{user_id}", "SK": f"SCHEDPROMPT#{schedule_id}"}


def _conditional_failed(error: Exception) -> bool:
    return getattr(error, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def add_spend(user_id: str, schedule_id: str, amount: Decimal, day: str) -> Optional[ScheduledPrompt]:
    """Add ``amount`` to the schedule's spend for local ``day``; return the schedule as written.

    §4.2: ``ADD`` while ``spendTodayDate`` is ``day``; else start the day with ``SET``,
    conditional on the date being another (or absent); if that loses too, another
    writer just rolled the day over, so add again. Two writers at midnight never lose
    one's cost. ``None`` when the schedule is gone (nothing is recreated).
    """
    table = _service._get_table()
    key = _key(user_id, schedule_id)
    for _ in range(3):
        try:
            response = table.update_item(
                Key=key,
                UpdateExpression="ADD spendToday :c",
                ConditionExpression="attribute_exists(PK) AND spendTodayDate = :day",
                ExpressionAttributeValues={":c": amount, ":day": day},
                ReturnValues="ALL_NEW",
            )
            return ScheduledPrompt.model_validate(response["Attributes"])
        except Exception as e:
            if not _conditional_failed(e):
                raise
        try:
            response = table.update_item(
                Key=key,
                UpdateExpression="SET spendToday = :c, spendTodayDate = :day",
                ConditionExpression=(
                    "attribute_exists(PK) AND (attribute_not_exists(spendTodayDate) OR spendTodayDate <> :day)"
                ),
                ExpressionAttributeValues={":c": amount, ":day": day},
                ReturnValues="ALL_NEW",
            )
            return ScheduledPrompt.model_validate(response["Attributes"])
        except Exception as e:
            if not _conditional_failed(e):
                raise
        if _service.read_scheduled_prompt(user_id, schedule_id) is None:
            return None
    logger.warning("Schedule %s: could not count a run's cost after three tries", schedule_id)
    return None


def mark_cost_pending(user_id: str, schedule_id: str) -> None:
    """Note one more run row whose cost the next run must fill in. Never raises."""
    try:
        _service._get_table().update_item(
            Key=_key(user_id, schedule_id),
            UpdateExpression="ADD costPendingRuns :one",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":one": 1},
        )
    except Exception as e:
        if not _conditional_failed(e):
            logger.warning("Schedule %s: could not note a pending run cost", schedule_id, exc_info=True)


# --------------------------------------------------------------------------- the trip


@dataclass
class BudgetTrip:
    """What reaching the budget did: ``held`` until ``until``, or ``paused`` on the third day."""

    action: str  # "held" | "paused"
    until: str
    days_at_limit: int
    spend: Decimal
    budget: Decimal


def _set_hold(schedule: ScheduledPrompt, until: str, days: int) -> bool:
    """Set the day's hold, once: a second writer for the same day loses and writes no bell row."""
    try:
        _service._get_table().update_item(
            Key=_key(schedule.user_id, schedule.schedule_id),
            UpdateExpression="SET budgetHoldUntil = :until, budgetDaysAtLimit = :days, updatedAt = :now",
            ConditionExpression=(
                "attribute_exists(PK) AND #state = :active "
                "AND (attribute_not_exists(budgetHoldUntil) OR budgetHoldUntil <> :until)"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":until": until,
                ":days": days,
                ":now": _service._get_current_timestamp(),
                ":active": "active",
            },
        )
        return True
    except Exception as e:
        if _conditional_failed(e):
            return False
        raise


def check_budget(schedule: ScheduledPrompt, now: datetime) -> Optional[BudgetTrip]:
    """Whether ``schedule`` (as just written by the count) reached its budget, and what to do.

    Pure: decides, writes nothing. ``None`` when it has no budget, is under it, isn't
    active, or is already held for this day.
    """
    if schedule.project_id or schedule.state != "active":
        return None
    budget = to_usd(schedule.max_cost_per_day)
    if budget is None:
        return None
    spend = spend_for_today(schedule, now)
    if spend < budget:
        return None
    until = next_local_midnight(schedule.timezone, now)
    if schedule.budget_hold_until == until:
        return None  # already held for today
    yesterday_at_limit = schedule.budget_hold_until == local_day_start(schedule.timezone, now)
    days = (schedule.budget_days_at_limit + 1) if yesterday_at_limit else 1
    action = "paused" if days >= PAUSE_AFTER_DAYS_AT_LIMIT else "held"
    return BudgetTrip(action=action, until=until, days_at_limit=days, spend=spend, budget=budget)


def budget_payload(schedule: ScheduledPrompt, trip: BudgetTrip) -> Dict[str, Any]:
    """The bell row's payload beside ``scheduleId``/``label``/``reason``: ids, amounts and times only."""
    payload: Dict[str, Any] = {
        "urgency": "fyi",
        "spendToday": usd_text(trip.spend),
        "maxCostPerDay": usd_text(trip.budget),
        "daysAtLimit": trip.days_at_limit,
    }
    if trip.action == "held":
        payload["resumesAt"] = trip.until
    return payload


async def apply_budget(schedule: ScheduledPrompt, now: Optional[datetime] = None) -> Optional[BudgetTrip]:
    """Hold or pause ``schedule`` if its count reached the budget, with one bell row. Never raises.

    ``schedule`` is the record the count returned, so its spend, budget and
    ``nextRunAt`` are current. The hold re-arms ``nextRunAt`` to the later of the
    hold's end and the time it already had, with the conditional ``rearm_schedule``;
    a re-arm that loses is left to the dispatcher's guard 2b, which honours the hold.
    """
    import asyncio

    from .pausing import notify_schedule_paused, pause_schedule

    now = now or utc_now()
    try:
        trip = check_budget(schedule, now)
        if trip is None:
            return None
        if not await asyncio.to_thread(_set_hold, schedule, trip.until, trip.days_at_limit):
            return None
        details = budget_payload(schedule, trip)
        if trip.action == "paused":
            await pause_schedule(schedule, BUDGET_REASON, details=details)
            logger.warning(
                f"Schedule {schedule.schedule_id}: budget reached {trip.days_at_limit} days in a row; pausing"
            )
            return trip
        target = later(trip.until, schedule.next_run_at) or trip.until
        if schedule.next_run_at and target != schedule.next_run_at:
            await _service.rearm_schedule(schedule.user_id, schedule.schedule_id, schedule.next_run_at, target)
        await asyncio.to_thread(notify_schedule_paused, schedule, BUDGET_REASON, None, details)
        logger.info(
            f"Schedule {schedule.schedule_id}: budget reached ({usd_text(trip.spend)} of "
            f"{usd_text(trip.budget)}); held until {trip.until}"
        )
        return trip
    except Exception:
        logger.warning(f"Schedule {schedule.schedule_id}: could not apply its budget", exc_info=True)
        return None


async def count_run_cost(
    schedule: ScheduledPrompt, cost_usd: Optional[float], now: Optional[datetime] = None
) -> Optional[BudgetTrip]:
    """Add a run's cost to the schedule's day, then hold or pause it if that reached the budget.

    Never raises: the run already happened. A ``None`` cost adds nothing (the run's
    row is ``costPending`` and the next run fills it in).
    """
    import asyncio

    amount = to_usd(cost_usd)
    if amount is None or amount <= 0 or schedule.project_id:
        return None
    now = now or utc_now()
    try:
        written = await asyncio.to_thread(
            add_spend, schedule.user_id, schedule.schedule_id, amount, local_date(schedule.timezone, now)
        )
    except Exception:
        logger.warning(f"Schedule {schedule.schedule_id}: could not count the run's cost", exc_info=True)
        return None
    if written is None:
        return None
    return await apply_budget(written, now)


# --------------------------------------------------------------------------- the reconcile


def session_chat_cost(session_id: str) -> Decimal:
    """The sum of a session's chat-call ``C#`` rows: what a run with no cost frame spent.

    Side calls (``callKind`` rows: titles, summaries, compaction) are left out, as the
    turn's frame leaves them out (§4.1). One paginated Query on ``SessionLookupIndex``.
    """
    from boto3.dynamodb.conditions import Key

    from apis.shared.sessions.ledger_rollups import call_kind

    total = Decimal(0)
    kwargs: Dict[str, Any] = {
        "IndexName": "SessionLookupIndex",
        "KeyConditionExpression": Key("GSI_PK").eq(f"SESSION#{session_id}") & Key("GSI_SK").begins_with("C#"),
        "ProjectionExpression": "#c, callKind, sessionId, messageId",
        "ExpressionAttributeNames": {"#c": "cost"},
    }
    table = _service._get_table()
    while True:
        response = table.query(**kwargs)
        for row in response.get("Items", []):
            if call_kind(row) != "chat":
                continue
            cost = row.get("cost")
            if isinstance(cost, dict):
                cost = cost.get("total")
            total += to_usd(cost) or Decimal(0)
        last = response.get("LastEvaluatedKey")
        if not last:
            return total.quantize(_QUANTUM)
        kwargs["ExclusiveStartKey"] = last


def _pending_rows(schedule: ScheduledPrompt) -> List[Dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    from .runs import _run_prefix

    response = _service._get_table().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{schedule.user_id}")
        & Key("SK").begins_with(_run_prefix(schedule.schedule_id)),
        ScanIndexForward=False,
        Limit=RECONCILE_SCAN_LIMIT,
    )
    return [row for row in response.get("Items", []) if row.get("costPending")]


def _settle_row(row: Dict[str, Any], cost: Optional[Decimal]) -> bool:
    """Write the reconciled ``costUsd`` (or just drop ``costPending``) on a run row, once."""
    update = "REMOVE costPending"
    values: Dict[str, Any] = {}
    if cost is not None:
        update = "SET costUsd = :c REMOVE costPending"
        values[":c"] = cost
    kwargs: Dict[str, Any] = {
        "Key": {"PK": row["PK"], "SK": row["SK"]},
        "UpdateExpression": update,
        "ConditionExpression": "attribute_exists(costPending)",
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values
    try:
        _service._get_table().update_item(**kwargs)
        return True
    except Exception as e:
        if _conditional_failed(e):
            return False
        raise


def _clear_pending_marker(schedule: ScheduledPrompt) -> None:
    """Zero ``costPendingRuns`` unless another run added one since it was read."""
    try:
        _service._get_table().update_item(
            Key=_key(schedule.user_id, schedule.schedule_id),
            UpdateExpression="SET costPendingRuns = :zero",
            ConditionExpression="attribute_exists(PK) AND costPendingRuns = :seen",
            ExpressionAttributeValues={":zero": 0, ":seen": schedule.cost_pending_runs},
        )
    except Exception as e:
        if not _conditional_failed(e):
            raise


async def reconcile_pending_costs(
    schedule: ScheduledPrompt, now: Optional[datetime] = None
) -> Optional[BudgetTrip]:
    """Fill in the cost of earlier runs that had no cost frame (§4.1). Never raises.

    Runs only when the schedule notes a pending row, so a run after a clean one reads
    nothing. A row that an inbox resume also wrote onto is only un-marked: its
    session holds the resume's calls too, which the resume already counted. Returns
    the trip if a reconciled cost that belongs to today reached the budget.
    """
    import asyncio

    if schedule.project_id or schedule.cost_pending_runs <= 0:
        return None
    now = now or utc_now()
    today = local_date(schedule.timezone, now)
    latest: Optional[ScheduledPrompt] = None
    try:
        rows = await asyncio.to_thread(_pending_rows, schedule)
        for row in rows:
            session_id = row.get("sessionId")
            cost: Optional[Decimal] = None
            if session_id and not row.get("resumeRunId"):
                cost = await asyncio.to_thread(session_chat_cost, session_id)
            if not await asyncio.to_thread(_settle_row, row, cost):
                continue
            logger.info(
                f"Schedule {schedule.schedule_id}: run {row.get('runId')} cost filled in "
                + (f"as ${cost}" if cost is not None else "as unknown")
            )
            finished = _parse(row.get("finishedAt"))
            if cost and finished is not None and local_date(schedule.timezone, finished) == today:
                # The run's day is today, so the ordinary count is right: it adds to today's
                # counter, or starts today's when this is the day's first cost.
                written = await asyncio.to_thread(add_spend, schedule.user_id, schedule.schedule_id, cost, today)
                latest = written or latest
        await asyncio.to_thread(_clear_pending_marker, schedule)
    except Exception:
        logger.warning(f"Schedule {schedule.schedule_id}: could not fill in earlier run costs", exc_info=True)
        return None
    if latest is None:
        return None
    return await apply_budget(latest, now)
