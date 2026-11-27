"""Scheduled-runs dispatcher tests (moto DynamoDB, stubbed worker invoke).

Mirrors backend/tests/lambdas/test_kb_sync_dispatcher.py's structure: due
sweep, runaway guard (incl. UTC date rollover), conditional rearm
win/lose, and invoke fan-out — using the REAL scheduled_prompts service so
the dispatcher's assumptions about that data plane are cross-checked
against the actual storage schema.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from apis.shared.scheduled_prompts.models import ScheduledPrompt
from apis.shared.scheduled_prompts.service import (
    RUNAWAY_SLACK_RUNS,
    get_scheduled_prompt,
    create_scheduled_prompt,
    runaway_ceiling,
    set_schedule_state,
)
from lambdas.scheduled_runs_dispatcher import dispatcher

pytestmark = pytest.mark.asyncio

USER_ID = "user-1"
PAST = "2000-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def scheduled_runs_env(monkeypatch):
    monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULED_RUNS_WORKER_FUNCTION_NAME", "test-scheduled-runs-worker")


@pytest.fixture()
def invoked_workers(monkeypatch):
    """Capture worker invocations instead of calling Lambda."""
    payloads = []
    monkeypatch.setattr(dispatcher, "_invoke_worker", payloads.append)
    return payloads


@pytest.fixture(autouse=True)
def no_metrics(monkeypatch):
    monkeypatch.setattr(dispatcher, "_emit_metrics", lambda counts: None)


async def _make_due_schedule(
    sessions_metadata_table, *, max_runs_per_day=24, label="Morning Briefing", interval_minutes=None
):
    cadence_kwargs = (
        {"cadence": "interval", "interval_value": interval_minutes, "interval_unit": "minutes"}
        if interval_minutes
        else {"cadence": "daily"}
    )
    schedule = await create_scheduled_prompt(
        user_id=USER_ID,
        label=label,
        prompt_text="Summarize my day",
        hour_local=9,
        timezone_name="America/Boise",
        **cadence_kwargs,
    )
    # Force it due immediately (create_scheduled_prompt always computes a
    # future next_run_at) and set the runaway-guard ceiling for this test.
    sessions_metadata_table.update_item(
        Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"},
        UpdateExpression="SET nextRunAt = :past, GSI3_SK = :gsisk, maxRunsPerDay = :max",
        ExpressionAttributeValues={
            ":past": PAST,
            ":gsisk": f"{PAST}#{schedule.schedule_id}",
            ":max": max_runs_per_day,
        },
    )
    return await get_scheduled_prompt(USER_ID, schedule.schedule_id)


class TestKillSwitch:
    async def test_disabled_tick_is_noop(self, sessions_metadata_table, invoked_workers, monkeypatch):
        monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", "false")
        await _make_due_schedule(sessions_metadata_table)

        counts = await dispatcher.dispatch_once()

        assert counts["SchedulesDue"] == 0
        assert invoked_workers == []


class TestRunawayGuard:
    async def test_exceeding_ceiling_pauses_instead_of_dispatching(self, sessions_metadata_table, invoked_workers):
        schedule = await _make_due_schedule(sessions_metadata_table, max_runs_per_day=3)
        sessions_metadata_table.update_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"},
            UpdateExpression="SET runsToday = :n, runsTodayDate = :today",
            ExpressionAttributeValues={":n": 3, ":today": date.today().isoformat()},
        )

        counts = await dispatcher.dispatch_once()

        assert counts["PausedRunaway"] == 1
        assert invoked_workers == []
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.state == "paused_error"
        assert updated.state_reason == "max_runs_per_day_exceeded"

    async def test_under_ceiling_still_dispatches(self, sessions_metadata_table, invoked_workers):
        schedule = await _make_due_schedule(sessions_metadata_table, max_runs_per_day=3)
        sessions_metadata_table.update_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"},
            UpdateExpression="SET runsToday = :n, runsTodayDate = :today",
            ExpressionAttributeValues={":n": 2, ":today": date.today().isoformat()},
        )

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1
        assert counts["PausedRunaway"] == 0

    async def test_stale_date_rollover_resets_counter(self, sessions_metadata_table, invoked_workers):
        """runsToday from a previous UTC day must not count against today's
        ceiling — the rollover-aware counter treats it as zero."""
        schedule = await _make_due_schedule(sessions_metadata_table, max_runs_per_day=1)
        sessions_metadata_table.update_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"},
            UpdateExpression="SET runsToday = :n, runsTodayDate = :yesterday",
            ExpressionAttributeValues={":n": 5, ":yesterday": "2000-01-01"},
        )

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1
        assert counts["PausedRunaway"] == 0


def _set_runs_today(sessions_metadata_table, schedule, runs):
    sessions_metadata_table.update_item(
        Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"},
        UpdateExpression="SET runsToday = :n, runsTodayDate = :today",
        ExpressionAttributeValues={":n": runs, ":today": date.today().isoformat()},
    )


class TestRunawayCeilingFollowsCadence:
    """An interval schedule is allowed the runs its cadence implies (#2494).

    Before, every schedule's ceiling was the default 24, so a 15-minute one
    paused itself after 24 of its 96 daily runs.
    """

    async def test_fifteen_minute_schedule_runs_past_24(self, sessions_metadata_table, invoked_workers):
        schedule = await _make_due_schedule(sessions_metadata_table, interval_minutes=15)
        _set_runs_today(sessions_metadata_table, schedule, 24)

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1
        assert counts["PausedRunaway"] == 0
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.state == "active"

    async def test_fifteen_minute_schedule_last_allowed_run(self, sessions_metadata_table, invoked_workers):
        schedule = await _make_due_schedule(sessions_metadata_table, interval_minutes=15)
        _set_runs_today(sessions_metadata_table, schedule, 96 + RUNAWAY_SLACK_RUNS - 1)

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1

    async def test_schedule_firing_past_its_cadence_still_pauses(self, sessions_metadata_table, invoked_workers):
        """A stuck re-arm fires every tick, far past the cadence: the guard still catches it."""
        schedule = await _make_due_schedule(sessions_metadata_table, interval_minutes=15)
        _set_runs_today(sessions_metadata_table, schedule, 96 + RUNAWAY_SLACK_RUNS)

        counts = await dispatcher.dispatch_once()

        assert counts["PausedRunaway"] == 1
        assert invoked_workers == []
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.state == "paused_error"
        assert updated.state_reason == "max_runs_per_day_exceeded"

    async def test_hourly_or_slower_interval_keeps_the_stored_ceiling(self, sessions_metadata_table, invoked_workers):
        schedule = await _make_due_schedule(sessions_metadata_table, interval_minutes=360)
        _set_runs_today(sessions_metadata_table, schedule, 24)

        counts = await dispatcher.dispatch_once()

        assert counts["PausedRunaway"] == 1


class TestRunawayCeiling:
    """``runaway_ceiling`` per cadence."""

    @staticmethod
    def _schedule(**fields):
        base = dict(
            schedule_id="sched-1",
            user_id=USER_ID,
            label="x",
            prompt_text="x",
            cadence="daily",
            hour_local=9,
            timezone="America/Boise",
            created_at=PAST,
            updated_at=PAST,
        )
        base.update(fields)
        return ScheduledPrompt.model_validate(base)

    @pytest.mark.parametrize("cadence,extra", [("daily", {}), ("weekday", {}), ("weekly", {"weekday": 0})])
    async def test_clock_cadences_keep_max_runs_per_day(self, cadence, extra):
        assert runaway_ceiling(self._schedule(cadence=cadence, **extra)) == 24
        assert runaway_ceiling(self._schedule(cadence=cadence, max_runs_per_day=3, **extra)) == 3

    @pytest.mark.parametrize(
        "value,unit,expected",
        [
            (15, "minutes", 96 + RUNAWAY_SLACK_RUNS),
            (20, "minutes", 72 + RUNAWAY_SLACK_RUNS),
            (45, "minutes", 32 + RUNAWAY_SLACK_RUNS),  # ceil(1440 / 45) = 32
            (60, "minutes", 24 + RUNAWAY_SLACK_RUNS),
            (1, "hours", 24 + RUNAWAY_SLACK_RUNS),
            (6, "hours", 24),  # 4 + slack is under the stored 24
        ],
    )
    async def test_interval_allows_its_cadence(self, value, unit, expected):
        schedule = self._schedule(cadence="interval", interval_value=value, interval_unit=unit)
        assert runaway_ceiling(schedule) == expected

    async def test_a_raised_stored_ceiling_is_kept(self):
        schedule = self._schedule(cadence="interval", interval_value=15, interval_unit="minutes", max_runs_per_day=500)
        assert runaway_ceiling(schedule) == 500


class TestConditionalRearm:
    async def test_happy_path_dispatches_and_rearms(self, sessions_metadata_table, invoked_workers):
        schedule = await _make_due_schedule(sessions_metadata_table)

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1
        assert invoked_workers == [{"scheduleId": schedule.schedule_id, "userId": USER_ID}]
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.next_run_at > datetime.now(timezone.utc).isoformat()

    async def test_dispatched_schedule_not_redispatched_same_tick(self, sessions_metadata_table, invoked_workers):
        await _make_due_schedule(sessions_metadata_table)

        await dispatcher.dispatch_once()
        counts = await dispatcher.dispatch_once()

        # Re-armed a day out (daily cadence) — second tick sees nothing due.
        assert counts["SchedulesDue"] == 0
        assert len(invoked_workers) == 1

    async def test_lost_conditional_write_skips_without_double_invoke(
        self, sessions_metadata_table, invoked_workers, monkeypatch
    ):
        """Simulate a double-fired tick: another dispatcher already won the
        conditional rearm before this call runs `_dispatch_schedule`."""
        schedule = await _make_due_schedule(sessions_metadata_table)

        real_rearm = dispatcher.rearm_schedule

        async def rearm_and_lose(*args, **kwargs):
            # A concurrent dispatcher wins first — real rearm succeeds once,
            # then this call's own attempt (with the stale expected value)
            # would lose. Simplest simulation: force False directly.
            return False

        monkeypatch.setattr(dispatcher, "rearm_schedule", rearm_and_lose)

        counts = await dispatcher.dispatch_once()

        assert counts["RearmLost"] == 1
        assert invoked_workers == []
        # next_run_at untouched — schedule remains due for the next tick.
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.next_run_at == PAST
        assert real_rearm is not None  # sanity: we didn't lose the reference


class TestBrokenScheduleIsolation:
    async def test_broken_schedule_does_not_starve_sweep(self, sessions_metadata_table, invoked_workers, monkeypatch):
        schedule_a = await _make_due_schedule(sessions_metadata_table, label="A")
        schedule_b = await _make_due_schedule(sessions_metadata_table, label="B")

        real_rearm = dispatcher.rearm_schedule
        calls = {"n": 0}

        async def flaky_rearm(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return await real_rearm(*args, **kwargs)

        monkeypatch.setattr(dispatcher, "rearm_schedule", flaky_rearm)

        counts = await dispatcher.dispatch_once()

        assert counts["Dispatched"] == 1
        assert counts["SchedulesDue"] == 2
        dispatched_ids = {p["scheduleId"] for p in invoked_workers}
        assert dispatched_ids <= {schedule_a.schedule_id, schedule_b.schedule_id}


class TestCadenceRearm:
    async def test_next_run_at_uses_schedule_cadence(self, sessions_metadata_table, invoked_workers, monkeypatch):
        from apis.shared.scheduled_prompts.service import compute_next_run_at

        schedule = await _make_due_schedule(sessions_metadata_table)

        # Freeze the dispatcher's clock so the daily-9am re-arm is a fixed
        # instant, not wall-clock dependent. Without this the delta assertion
        # is flaky in the hour before 9am Boise (14:00–15:00 UTC in MDT), when
        # the next daily run is legitimately <1h away.
        frozen_now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(dispatcher, "_now", lambda: frozen_now)

        await dispatcher.dispatch_once()

        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        # Recompute with the same frozen instant and the schedule's own
        # cadence: an exact match proves the schedule's cadence math ran (not
        # the ~1h fallback delta).
        expected = compute_next_run_at(
            schedule.cadence,
            schedule.hour_local,
            schedule.timezone,
            weekday=schedule.weekday,
            from_time=frozen_now,
        )
        assert updated.next_run_at == expected
        # Sanity: 9am America/Boise (MDT, UTC-6) on 2026-07-09 is 15:00 UTC,
        # same day (frozen "now" is 12:00 UTC = 6am Boise, before 9am).
        assert expected == "2026-07-09T15:00:00Z"
