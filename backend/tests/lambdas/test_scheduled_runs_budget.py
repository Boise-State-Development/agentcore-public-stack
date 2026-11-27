"""A schedule's daily budget, end to end (docs/specs/proactive-trust.md §4.1–§4.2, PR 4.1).

The 4.1 row's acceptance, in moto with the real scheduled_prompts service, worker and
dispatcher; only ``run_agent_headless`` (the harness's HTTP/SSE boundary) is stubbed, and
the budget's clock (``budget.utc_now``, ``dispatcher._now``) is moved across local midnight:

- a budget of a few runs' cost stops the schedule after that many runs, with one bell row;
- it runs again after its next local midnight, on a fresh day's count;
- a third consecutive day at the limit pauses it (``stateReason: "budget"``); a gap resets;
- each run's ``costUsd`` is the turn's cost, and a timed-out run's is filled in at the next
  run from its session's chat-call ``C#`` rows (side calls left out);
- an inbox resume's cost counts toward the day and can be what reaches it, with no second row;
- twenty over-budget schedules don't starve a twenty-first;
- while ``PROACTIVE_RUNS_ENABLED`` is off, nothing is counted and nothing is held.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import boto3
import pytest
from boto3.dynamodb.conditions import Key

from apis.shared.harness.models import RunResult
from apis.shared.scheduled_prompts import budget
from apis.shared.scheduled_prompts.service import create_scheduled_prompt, get_scheduled_prompt
from lambdas.scheduled_runs_dispatcher import dispatcher
from lambdas.scheduled_runs_worker import worker
from tests.shared.test_project_schedules import TABLE, _make_projects_table

pytestmark = pytest.mark.asyncio

USER_ID = "user-1"
EMAIL = "owner@example.edu"
TZ = "America/Boise"
PAST = "2000-01-01T00:00:00Z"
#: 10:00 in Boise on a day with no DST change (UTC-7).
DAY_1 = datetime(2026, 12, 2, 17, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    c = Clock(DAY_1)
    monkeypatch.setattr(budget, "utc_now", c)
    monkeypatch.setattr(dispatcher, "_now", c)
    return c


@pytest.fixture()
def env(sessions_metadata_table, bff_sessions_table, monkeypatch, clock):
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", "true")
    monkeypatch.setenv("PROJECTS_ENABLED", "true")
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("SCHEDULED_RUNS_WORKER_FUNCTION_NAME", "test-worker")
    monkeypatch.setattr(dispatcher, "_emit_metrics", lambda counts: None)
    _make_projects_table()
    return sessions_metadata_table


@pytest.fixture()
def invoked(monkeypatch):
    payloads = []
    monkeypatch.setattr(dispatcher, "_invoke_worker", payloads.append)
    return payloads


def _bell_rows(reason: str = "budget") -> list:
    table = boto3.resource("dynamodb").Table(TABLE)
    items = table.query(KeyConditionExpression=Key("PK").eq(f"INBOX#{EMAIL}"))["Items"]
    return [i for i in items if i.get("kind") == "schedule_paused" and i["payload"].get("reason") == reason]


def _run_rows(table) -> list:
    return table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{USER_ID}") & Key("SK").begins_with("SCHEDRUN#")
    )["Items"]


async def _schedule(budget_usd="0.03", interval=15, **overrides):
    kwargs = dict(
        user_id=USER_ID,
        label="Inbox watcher",
        prompt_text="Tell me what needs me",
        cadence="interval",
        hour_local=0,
        interval_value=interval,
        interval_unit="minutes",
        timezone_name=TZ,
        owner_email=EMAIL,
        max_cost_per_day=Decimal(budget_usd) if budget_usd is not None else None,
    )
    kwargs.update(overrides)
    return await create_scheduled_prompt(**kwargs)


def _make_due(table, schedule_id: str, at: str = PAST) -> None:
    table.update_item(
        Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule_id}"},
        UpdateExpression="SET nextRunAt = :t, GSI3_SK = :k",
        ExpressionAttributeValues={":t": at, ":k": f"{at}#{schedule_id}"},
    )


def _stub_runs(monkeypatch, *results):
    queue = list(results)
    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        return result

    monkeypatch.setattr(worker, "run_agent_headless", fake_run)
    return calls


_COUNTER = {"n": 0}


def _result(status="completed", cost=0.01, session_id=None, **overrides) -> RunResult:
    _COUNTER["n"] += 1
    n = _COUNTER["n"]
    base = dict(
        run_id=f"run-{n}",
        session_id=session_id or f"headless-{n}",
        user_id=USER_ID,
        status=status,
        started_at="2026-12-02T17:00:00Z",
        finished_at=f"2026-12-02T17:{n % 60:02d}:00Z",
        usage={"usage": {"inputTokens": 1000, "outputTokens": 20}},
        cost_usd=cost,
    )
    base.update(overrides)
    return RunResult(**base)


async def _run(schedule):
    return await worker.run_schedule({"scheduleId": schedule.schedule_id, "userId": USER_ID})


def _boise_midnight_after(now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo(TZ))
    nxt = (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).replace(tzinfo=None)
    return nxt.replace(tzinfo=ZoneInfo(TZ)).astimezone(timezone.utc)


class TestTheBudgetStopsTheDay:
    async def test_stops_after_that_many_runs_with_one_bell_row(self, env, monkeypatch, invoked):
        schedule = await _schedule(budget_usd="0.03")
        _stub_runs(monkeypatch, _result())

        for _ in range(2):
            await _run(schedule)
        assert _bell_rows() == []
        assert (await get_scheduled_prompt(USER_ID, schedule.schedule_id)).budget_hold_until is None

        await _run(schedule)  # the third $0.01 run reaches $0.03

        held = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        midnight = budget.next_local_midnight(TZ, DAY_1)
        assert held.state == "active"
        assert held.spend_today == Decimal("0.03")
        assert held.spend_today_date == "2026-12-02"
        assert held.budget_hold_until == midnight
        assert held.budget_days_at_limit == 1
        assert held.next_run_at >= midnight
        [row] = _bell_rows()
        assert row["payload"] == {
            "scheduleId": schedule.schedule_id,
            "label": "Inbox watcher",
            "reason": "budget",
            "urgency": "fyi",
            "spendToday": "0.03",
            "maxCostPerDay": "0.03",
            "daysAtLimit": 1,
            "resumesAt": midnight,
        }

        # A due tick (a stale re-arm, say) re-arms it past the hold and invokes nothing.
        _make_due(env, schedule.schedule_id)
        counts = await dispatcher.dispatch_once()
        assert counts["HeldBudget"] == 1 and counts["Dispatched"] == 0
        assert invoked == []
        assert (await get_scheduled_prompt(USER_ID, schedule.schedule_id)).next_run_at >= midnight
        # Still one row.
        assert len(_bell_rows()) == 1

    async def test_runs_again_after_local_midnight_on_a_fresh_count(self, env, monkeypatch, invoked, clock):
        schedule = await _schedule(budget_usd="0.02")
        _stub_runs(monkeypatch, _result())
        await _run(schedule)
        await _run(schedule)
        assert (await get_scheduled_prompt(USER_ID, schedule.schedule_id)).budget_hold_until

        clock.now = _boise_midnight_after(DAY_1) + timedelta(minutes=1)
        _make_due(env, schedule.schedule_id)
        counts = await dispatcher.dispatch_once()
        assert counts["Dispatched"] == 1 and counts["HeldBudget"] == 0

        await _run(schedule)
        fresh = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fresh.spend_today_date == "2026-12-03"
        assert fresh.spend_today == Decimal("0.01")
        assert len(_bell_rows()) == 1


class TestThreeDaysAtTheLimit:
    async def _spend_the_day(self, schedule, runs: int = 2) -> None:
        for _ in range(runs):
            await _run(schedule)

    async def test_the_third_consecutive_day_pauses(self, env, monkeypatch, clock):
        schedule = await _schedule(budget_usd="0.02")
        _stub_runs(monkeypatch, _result())

        await self._spend_the_day(schedule)
        clock.now = DAY_1 + timedelta(days=1)
        await self._spend_the_day(schedule)
        day_2 = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert day_2.state == "active" and day_2.budget_days_at_limit == 2

        clock.now = DAY_1 + timedelta(days=2)
        await self._spend_the_day(schedule)

        paused = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert paused.state == "paused_error"
        assert paused.state_reason == "budget"
        assert paused.budget_days_at_limit == 3
        rows = _bell_rows()
        assert len(rows) == 3
        last = max(rows, key=lambda r: r["payload"]["daysAtLimit"])
        assert last["payload"]["daysAtLimit"] == 3
        assert "resumesAt" not in last["payload"]  # paused until the user resumes it

    async def test_a_day_under_the_budget_resets_the_streak(self, env, monkeypatch, clock):
        schedule = await _schedule(budget_usd="0.02")
        _stub_runs(monkeypatch, _result())

        await self._spend_the_day(schedule)
        clock.now = DAY_1 + timedelta(days=1)
        await self._spend_the_day(schedule, runs=1)  # ends under
        clock.now = DAY_1 + timedelta(days=2)
        await self._spend_the_day(schedule)

        schedule = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert schedule.state == "active"
        assert schedule.budget_days_at_limit == 1


class TestRunCost:
    async def test_each_run_row_carries_its_cost(self, env, monkeypatch):
        schedule = await _schedule(budget_usd="5")
        _stub_runs(monkeypatch, _result(cost=0.0123456789))

        await _run(schedule)

        [row] = _run_rows(env)
        assert row["costUsd"] == Decimal("0.01234568")
        assert "costPending" not in row
        assert (await get_scheduled_prompt(USER_ID, schedule.schedule_id)).spend_today == Decimal("0.01234568")

    async def test_a_timed_out_run_is_filled_in_at_the_next_run(self, env, monkeypatch):
        schedule = await _schedule(budget_usd="5")
        timed_out = _result(status="timeout", cost=None, session_id="headless-timedout", error="limit")
        _stub_runs(monkeypatch, timed_out)
        await _run(schedule)

        [row] = _run_rows(env)
        assert row["costPending"] is True and "costUsd" not in row
        noted = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert noted.cost_pending_runs == 1
        assert noted.spend_today == Decimal(0)

        # The Runtime's C# rows for that session: two chat calls and a side call.
        for i, (cost, kind) in enumerate(((Decimal("0.004"), None), (Decimal("0.006"), None), (Decimal("0.001"), "session_title"))):
            item = {
                "PK": f"USER#{USER_ID}",
                "SK": f"C#2026-12-02T17:0{i}:00Z#{i}",
                "GSI_PK": "SESSION#headless-timedout",
                "GSI_SK": f"C#2026-12-02T17:0{i}:00Z",
                "sessionId": "headless-timedout",
                "messageId": i,
                "cost": {"total": cost},
            }
            if kind:
                item["callKind"] = kind
            env.put_item(Item=item)

        _stub_runs(monkeypatch, _result(cost=0.002))
        await _run(schedule)

        by_run = {r["runId"]: r for r in _run_rows(env)}
        filled = by_run[timed_out.run_id]
        assert filled["costUsd"] == Decimal("0.01")  # the side call is not in it
        assert "costPending" not in filled
        after = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert after.cost_pending_runs == 0
        assert after.spend_today == Decimal("0.012")  # the filled-in run, then this one

    async def test_a_clean_run_reads_no_c_rows(self, env, monkeypatch):
        schedule = await _schedule(budget_usd="5")
        _stub_runs(monkeypatch, _result())
        seen = []
        monkeypatch.setattr(budget, "session_chat_cost", lambda sid: seen.append(sid) or Decimal(0))
        await _run(schedule)
        await _run(schedule)
        assert seen == []

    async def test_a_reconciled_cost_that_spends_the_day_holds_the_run(self, env, monkeypatch):
        schedule = await _schedule(budget_usd="0.01")
        _stub_runs(monkeypatch, _result(status="timeout", cost=None, session_id="headless-big"))
        await _run(schedule)
        monkeypatch.setattr(budget, "session_chat_cost", lambda sid: Decimal("0.05"))
        calls = _stub_runs(monkeypatch, _result())

        outcome = await _run(schedule)

        assert outcome["result"] == "held_budget"
        assert calls == []  # the run that was dispatched does not start
        assert len(_bell_rows()) == 1


class TestInboxResume:
    async def test_a_resume_counts_and_can_reach_the_budget_once(self, env):
        schedule = await _schedule(budget_usd="0.02")
        await budget.count_run_cost(schedule, 0.01)
        assert _bell_rows() == []

        trip = await budget.count_run_cost(schedule, 0.015)  # the resume that reaches it

        assert trip is not None and trip.action == "held"
        assert len(_bell_rows()) == 1
        again = await budget.count_run_cost(await get_scheduled_prompt(USER_ID, schedule.schedule_id), 0.01)
        assert again is None  # already held today: no second row
        assert len(_bell_rows()) == 1


class TestDispatcherGuard2b:
    async def test_twenty_over_budget_schedules_dont_starve_a_twenty_first(self, env, invoked, monkeypatch):
        monkeypatch.setenv("SCHEDULED_RUNS_MAX_PER_USER", "30")
        today = budget.local_date(TZ, DAY_1)
        held = []
        for i in range(20):
            s = await _schedule(budget_usd="0.10", label=f"Over {i}")
            # Half held by a trip, half over a budget their owner lowered below today's spend.
            if i % 2:
                expr, vals = "SET budgetHoldUntil = :h", {":h": budget.next_local_midnight(TZ, DAY_1)}
            else:
                expr, vals = "SET spendToday = :s, spendTodayDate = :d", {":s": Decimal("0.25"), ":d": today}
            env.update_item(Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{s.schedule_id}"},
                            UpdateExpression=expr, ExpressionAttributeValues=vals)
            _make_due(env, s.schedule_id, "2000-01-01T00:00:00Z")
            held.append(s.schedule_id)
        ok = await _schedule(budget_usd="0.10", label="Under")
        _make_due(env, ok.schedule_id, "2000-01-02T00:00:00Z")  # less overdue than all twenty

        first = await dispatcher.dispatch_once()
        assert first["HeldBudget"] == 20 and first["Dispatched"] == 0

        second = await dispatcher.dispatch_once()
        assert second["Dispatched"] == 1
        assert [p["scheduleId"] for p in invoked] == [ok.schedule_id]
        for schedule_id in held:
            s = await get_scheduled_prompt(USER_ID, schedule_id)
            assert s.state == "active"
            assert s.next_run_at >= budget.next_local_midnight(TZ, DAY_1)

    async def test_a_schedule_with_no_budget_is_not_held(self, env, invoked):
        s = await _schedule(budget_usd=None, interval=90)
        _make_due(env, s.schedule_id)
        counts = await dispatcher.dispatch_once()
        assert counts["Dispatched"] == 1 and counts["HeldBudget"] == 0


class TestFlagOff:
    async def test_nothing_is_counted_or_held(self, env, monkeypatch, invoked):
        monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "false")
        schedule = await _schedule(budget_usd="0.01")
        _stub_runs(monkeypatch, _result(cost=0.5))
        await _run(schedule)
        after = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert after.spend_today == Decimal(0) and after.budget_hold_until is None
        assert _bell_rows() == []

        env.update_item(Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"},
                        UpdateExpression="SET budgetHoldUntil = :h",
                        ExpressionAttributeValues={":h": budget.next_local_midnight(TZ, DAY_1)})
        _make_due(env, schedule.schedule_id)
        counts = await dispatcher.dispatch_once()
        assert counts["Dispatched"] == 1
