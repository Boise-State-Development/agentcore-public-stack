"""Scheduled-runs dispatcher tests (moto DynamoDB, stubbed worker invoke).

Mirrors backend/tests/lambdas/test_kb_sync_dispatcher.py's structure: due
sweep, runaway guard (incl. UTC date rollover), conditional rearm
win/lose, and invoke fan-out — using the REAL scheduled_prompts service so
the dispatcher's assumptions about that data plane are cross-checked
against the actual storage schema.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from apis.shared.scheduled_prompts.service import (
    get_scheduled_prompt,
    create_scheduled_prompt,
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


async def _make_due_schedule(sessions_metadata_table, *, max_runs_per_day=24, label="Morning Briefing"):
    schedule = await create_scheduled_prompt(
        user_id=USER_ID,
        label=label,
        prompt_text="Summarize my day",
        cadence="daily",
        hour_local=9,
        timezone_name="America/Boise",
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
    async def test_next_run_at_uses_schedule_cadence(self, sessions_metadata_table, invoked_workers):
        schedule = await _make_due_schedule(sessions_metadata_table)

        await dispatcher.dispatch_once()

        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        next_dt = datetime.fromisoformat(updated.next_run_at.rstrip("Z")).replace(tzinfo=timezone.utc)
        # Daily at 9am America/Boise from "now" (past due) lands within the
        # next ~48h — a loose bound that just proves cadence math ran
        # (not the fallback delta, which would be ~1h out).
        assert timedelta(hours=1) < (next_dt - datetime.now(timezone.utc)) < timedelta(hours=48)
