"""The budget's storage rules (docs/specs/proactive-trust.md §4.1–§4.2, PR 4.1).

- the budget's day is the schedule's own, and its midnight is DST-safe;
- the conditional count never loses a writer's cost when the day rolls over;
- ``check_budget`` counts consecutive days from the last hold's end;
- an edit that recomputes ``nextRunAt`` respects the hold; a raised or cleared
  budget releases it and the schedule runs again today;
- ``session_chat_cost`` sums a session's chat-call ``C#`` rows only;
- a resume adds its ``costUsd`` to the paused run's row;
- the default comes from SCHEDULE_DEFAULT_MAX_COST_PER_DAY, else $0.50.

moto DynamoDB (``sessions_metadata_table`` from tests/shared/conftest.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from boto3.dynamodb.conditions import Key

from apis.shared.scheduled_prompts import budget
from apis.shared.scheduled_prompts.models import ScheduledPrompt
from apis.shared.scheduled_prompts.runs import record_schedule_run, record_schedule_run_resume
from apis.shared.scheduled_prompts.service import (
    create_scheduled_prompt,
    get_scheduled_prompt,
    update_scheduled_prompt,
)

USER_ID = "user-1"
TZ = "America/Boise"
NOON = datetime(2026, 12, 2, 19, 0, tzinfo=timezone.utc)  # 12:00 in Boise


def _schedule(**overrides) -> ScheduledPrompt:
    base = dict(
        scheduleId="sched-abc",
        userId=USER_ID,
        label="W",
        promptText="p",
        cadence="interval",
        hourLocal=0,
        intervalValue=15,
        intervalUnit="minutes",
        timezone=TZ,
        state="active",
        createdAt="2026-12-01T00:00:00Z",
        updatedAt="2026-12-01T00:00:00Z",
    )
    base.update(overrides)
    return ScheduledPrompt.model_validate(base)


class TestDays:
    def test_the_day_is_the_schedules_own(self):
        late = datetime(2026, 12, 3, 5, 0, tzinfo=timezone.utc)  # 22:00 on the 2nd in Boise
        assert budget.local_date(TZ, late) == "2026-12-02"
        assert budget.local_date("UTC", late) == "2026-12-03"

    def test_next_midnight(self):
        assert budget.next_local_midnight(TZ, NOON) == "2026-12-03T07:00:00Z"
        assert budget.local_day_start(TZ, NOON) == "2026-12-02T07:00:00Z"

    def test_midnight_across_dst(self):
        # 2026-03-08: Boise springs forward. The day before ends at 07:00Z, that day at 06:00Z.
        before = datetime(2026, 3, 7, 19, 0, tzinfo=timezone.utc)
        assert budget.next_local_midnight(TZ, before) == "2026-03-08T07:00:00Z"
        assert budget.next_local_midnight(TZ, datetime(2026, 3, 8, 19, 0, tzinfo=timezone.utc)) == "2026-03-09T06:00:00Z"

    @pytest.mark.parametrize(
        "cadence, minutes, required",
        [("interval", 15, True), ("interval", 59, True), ("interval", 60, False), ("daily", None, False)],
    )
    def test_requires_budget(self, cadence, minutes, required):
        assert budget.requires_budget(cadence, minutes) is required

    def test_spend_for_today_is_zero_on_a_new_day(self):
        s = _schedule(spendToday=Decimal("0.4"), spendTodayDate="2026-12-01")
        assert budget.spend_for_today(s, NOON) == 0
        assert budget.spend_for_today(_schedule(spendToday=Decimal("0.4"), spendTodayDate="2026-12-02"), NOON) == Decimal("0.4")

    def test_guard_2b_reads_a_lowered_budget_and_the_hold(self):
        lowered = _schedule(maxCostPerDay=Decimal("0.1"), spendToday=Decimal("0.2"), spendTodayDate="2026-12-02")
        assert budget.budget_hold_end(lowered, NOON) == "2026-12-03T07:00:00Z"
        held = _schedule(budgetHoldUntil="2026-12-03T07:00:00Z")
        assert budget.budget_hold_end(held, NOON) == "2026-12-03T07:00:00Z"
        assert budget.budget_hold_end(_schedule(budgetHoldUntil="2026-12-02T07:00:00Z"), NOON) is None
        assert budget.budget_hold_end(_schedule(projectId="prj_1", budgetHoldUntil="2026-12-03T07:00:00Z"), NOON) is None


class TestCheckBudget:
    def test_under_budget_is_nothing(self):
        s = _schedule(maxCostPerDay=Decimal("0.5"), spendToday=Decimal("0.49"), spendTodayDate="2026-12-02")
        assert budget.check_budget(s, NOON) is None

    def test_first_day(self):
        s = _schedule(maxCostPerDay=Decimal("0.5"), spendToday=Decimal("0.5"), spendTodayDate="2026-12-02")
        trip = budget.check_budget(s, NOON)
        assert (trip.action, trip.days_at_limit, trip.until) == ("held", 1, "2026-12-03T07:00:00Z")

    def test_yesterdays_hold_ending_today_continues_the_streak(self):
        s = _schedule(
            maxCostPerDay=Decimal("0.5"), spendToday=Decimal("0.6"), spendTodayDate="2026-12-02",
            budgetHoldUntil="2026-12-02T07:00:00Z", budgetDaysAtLimit=2,
        )
        trip = budget.check_budget(s, NOON)
        assert (trip.action, trip.days_at_limit) == ("paused", 3)

    def test_an_older_hold_starts_over(self):
        s = _schedule(
            maxCostPerDay=Decimal("0.5"), spendToday=Decimal("0.6"), spendTodayDate="2026-12-02",
            budgetHoldUntil="2026-12-01T07:00:00Z", budgetDaysAtLimit=2,
        )
        assert budget.check_budget(s, NOON).days_at_limit == 1

    def test_already_held_today_or_paused_is_nothing(self):
        spent = dict(maxCostPerDay=Decimal("0.5"), spendToday=Decimal("0.6"), spendTodayDate="2026-12-02")
        assert budget.check_budget(_schedule(**spent, budgetHoldUntil="2026-12-03T07:00:00Z"), NOON) is None
        assert budget.check_budget(_schedule(**spent, state="paused"), NOON) is None


@pytest.mark.asyncio
class TestTheCount:
    async def _create(self, **kwargs):
        return await create_scheduled_prompt(
            user_id=USER_ID, label="W", prompt_text="p", cadence="interval", hour_local=0,
            interval_value=15, interval_unit="minutes", timezone_name=TZ, **kwargs,
        )

    async def test_adds_within_a_day_and_starts_a_new_one(self, sessions_metadata_table):
        s = await self._create(max_cost_per_day=Decimal("1"))
        assert budget.add_spend(USER_ID, s.schedule_id, Decimal("0.1"), "2026-12-02").spend_today == Decimal("0.1")
        assert budget.add_spend(USER_ID, s.schedule_id, Decimal("0.2"), "2026-12-02").spend_today == Decimal("0.3")
        rolled = budget.add_spend(USER_ID, s.schedule_id, Decimal("0.05"), "2026-12-03")
        assert (rolled.spend_today, rolled.spend_today_date) == (Decimal("0.05"), "2026-12-03")

    async def test_two_writers_at_midnight_both_count(self, sessions_metadata_table, monkeypatch):
        """Both writers find yesterday's date; the other's rollover SET lands first, so this one's
        SET loses its condition and its retry ADDs onto the new day. Nothing is lost."""
        s = await self._create(max_cost_per_day=Decimal("1"))
        budget.add_spend(USER_ID, s.schedule_id, Decimal("0.4"), "2026-12-02")
        table = budget._service._get_table()
        real_update = table.update_item
        raced = {"done": False}

        def racing_update(**kwargs):
            # Just before this writer's rollover SET, the other writer starts the new day.
            if not raced["done"] and str(kwargs.get("UpdateExpression", "")).startswith("SET spendToday"):
                raced["done"] = True
                real_update(
                    Key=kwargs["Key"],
                    UpdateExpression="SET spendToday = :c, spendTodayDate = :day",
                    ExpressionAttributeValues={":c": Decimal("0.07"), ":day": "2026-12-03"},
                )
            return real_update(**kwargs)

        monkeypatch.setattr(table, "update_item", racing_update)
        monkeypatch.setattr(budget._service, "_get_table", lambda: table)
        written = budget.add_spend(USER_ID, s.schedule_id, Decimal("0.02"), "2026-12-03")
        assert (written.spend_today, written.spend_today_date) == (Decimal("0.09"), "2026-12-03")

    async def test_a_deleted_schedule_is_never_recreated(self, sessions_metadata_table):
        assert budget.add_spend(USER_ID, "sched-gone", Decimal("0.1"), "2026-12-02") is None
        assert sessions_metadata_table.get_item(Key={"PK": f"USER#{USER_ID}", "SK": "SCHEDPROMPT#sched-gone"}).get("Item") is None


@pytest.mark.asyncio
class TestEditsAndTheHold:
    async def _held(self, sessions_metadata_table, monkeypatch, *, spend="0.5", budget_usd="0.5"):
        monkeypatch.setattr(budget, "utc_now", lambda: NOON)
        s = await create_scheduled_prompt(
            user_id=USER_ID, label="W", prompt_text="p", cadence="interval", hour_local=0,
            interval_value=15, interval_unit="minutes", timezone_name=TZ, max_cost_per_day=Decimal(budget_usd),
        )
        sessions_metadata_table.update_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{s.schedule_id}"},
            UpdateExpression="SET spendToday = :s, spendTodayDate = :d, budgetHoldUntil = :h",
            ExpressionAttributeValues={":s": Decimal(spend), ":d": "2026-12-02", ":h": "2999-01-01T07:00:00Z"},
        )
        return s

    async def test_a_cadence_edit_never_lands_inside_the_hold(self, sessions_metadata_table, monkeypatch):
        s = await self._held(sessions_metadata_table, monkeypatch)
        edited = await update_scheduled_prompt(USER_ID, s.schedule_id, interval_value=30)
        assert edited.next_run_at == "2999-01-01T07:00:00Z"
        assert edited.budget_hold_until == "2999-01-01T07:00:00Z"

    async def test_a_raised_budget_releases_the_hold(self, sessions_metadata_table, monkeypatch):
        s = await self._held(sessions_metadata_table, monkeypatch)
        edited = await update_scheduled_prompt(USER_ID, s.schedule_id, max_cost_per_day=Decimal("1"))
        assert edited.budget_hold_until is None
        assert edited.max_cost_per_day == Decimal("1")
        assert edited.next_run_at < "2100-01-01"  # its cadence from now: runs again today
        due = sessions_metadata_table.get_item(Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{s.schedule_id}"})["Item"]
        assert due["GSI3_SK"].startswith(edited.next_run_at)

    async def test_a_budget_still_under_todays_spend_keeps_the_hold(self, sessions_metadata_table, monkeypatch):
        s = await self._held(sessions_metadata_table, monkeypatch, spend="0.8")
        edited = await update_scheduled_prompt(USER_ID, s.schedule_id, max_cost_per_day=Decimal("0.6"))
        assert edited.budget_hold_until == "2999-01-01T07:00:00Z"

    async def test_clearing_the_budget_releases_the_hold(self, sessions_metadata_table, monkeypatch):
        s = await self._held(sessions_metadata_table, monkeypatch)
        edited = await update_scheduled_prompt(USER_ID, s.schedule_id, max_cost_per_day=None)
        assert edited.budget_hold_until is None and edited.max_cost_per_day is None

    async def test_an_edit_that_names_no_budget_leaves_it(self, sessions_metadata_table, monkeypatch):
        s = await self._held(sessions_metadata_table, monkeypatch)
        edited = await update_scheduled_prompt(USER_ID, s.schedule_id, label="Renamed")
        assert edited.max_cost_per_day == Decimal("0.5")
        assert edited.budget_hold_until == "2999-01-01T07:00:00Z"


class TestSessionChatCost:
    def test_sums_chat_calls_and_leaves_side_calls_out(self, sessions_metadata_table):
        rows = [
            (Decimal("0.001"), None, 0),
            ({"total": Decimal("0.002")}, None, 1),
            ({"total": Decimal("0.0005")}, "session_title", None),
            ({"total": Decimal("0.0007")}, "tool_summary", None),
            (Decimal("0.003"), None, "voice:1"),
        ]
        for i, (cost, kind, message_id) in enumerate(rows):
            item = {
                "PK": f"USER#{USER_ID}", "SK": f"C#2026-12-02T19:0{i}:00Z#{i}",
                "GSI_PK": "SESSION#headless-x", "GSI_SK": f"C#2026-12-02T19:0{i}:00Z",
                "sessionId": "headless-x", "cost": cost,
            }
            if kind:
                item["callKind"] = kind
            if message_id is not None:
                item["messageId"] = message_id
            sessions_metadata_table.put_item(Item=item)
        assert budget.session_chat_cost("headless-x") == Decimal("0.003")
        assert budget.session_chat_cost("headless-none") == Decimal(0)


class TestRunRows:
    def test_a_resume_adds_its_cost(self, sessions_metadata_table):
        s = _schedule()
        record_schedule_run(s, run_id="run-1", status="needs_user", session_id="headless-1",
                            finished_at="2026-12-02T19:00:00Z", cost_usd=0.01)
        assert record_schedule_run_resume(
            USER_ID, s.schedule_id, run_finished_at="2026-12-02T19:00:00Z", run_id="run-1",
            resolution="approved", resume_run_id="resume-1", resumed_at="2026-12-02T19:05:00Z",
            resume_outcome="finished", cost_usd=0.004,
        )
        [row] = sessions_metadata_table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{USER_ID}") & Key("SK").begins_with("SCHEDRUN#")
        )["Items"]
        assert row["costUsd"] == Decimal("0.014")

    def test_a_pending_row_has_no_cost(self, sessions_metadata_table):
        record_schedule_run(_schedule(), run_id="run-2", status="timeout", session_id="headless-2",
                            finished_at="2026-12-02T19:00:00Z", cost_usd=None, cost_pending=True)
        [row] = sessions_metadata_table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{USER_ID}") & Key("SK").begins_with("SCHEDRUN#")
        )["Items"]
        assert row["costPending"] is True and "costUsd" not in row


class TestDefault:
    @pytest.mark.parametrize("raw, expected", [
        (None, "0.50"), ("", "0.50"), ("0.25", "0.25"), ("abc", "0.50"), ("0", "0.50"), ("-1", "0.50"),
    ])
    def test_default(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv("SCHEDULE_DEFAULT_MAX_COST_PER_DAY", raising=False)
        else:
            monkeypatch.setenv("SCHEDULE_DEFAULT_MAX_COST_PER_DAY", raw)
        assert budget.default_max_cost_per_day() == Decimal(expected)


def test_later():
    assert budget.later("2026-12-02T07:00:00Z", "2026-12-03T07:00:00Z") == "2026-12-03T07:00:00Z"
    assert budget.later(None, "2026-12-03T07:00:00Z") == "2026-12-03T07:00:00Z"
    assert budget.later("2026-12-02T07:00:00.123456Z", None) == "2026-12-02T07:00:00.123456Z"
