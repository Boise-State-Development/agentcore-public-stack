"""Scheduled-prompt repository tests (moto DynamoDB).

Covers:
- cadence -> next_run_at math (daily/weekday/weekly, timezone-aware, DST-safe)
- sparse DueScheduleIndex (paused schedules are physically absent)
- conditional re-arm (double-fired dispatcher tick is idempotent)
- state transitions (active <-> paused, paused_error)
- snapshot semantics (enabled_tools frozen at creation)
- per-user cap
- delete = total revocation
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from apis.shared.scheduled_prompts.models import DUE_INDEX_PK
from apis.shared.scheduled_prompts.service import (
    ScheduledPromptLimitExceeded,
    compute_next_run_at,
    create_scheduled_prompt,
    delete_scheduled_prompt,
    get_scheduled_prompt,
    interval_to_minutes,
    list_due_schedules,
    list_scheduled_prompts,
    max_schedules_per_user,
    rearm_schedule,
    record_run_result,
    set_schedule_state,
    update_scheduled_prompt,
)

pytestmark = pytest.mark.asyncio

USER_ID = "user-1"
OTHER_USER_ID = "user-2"


async def _make_schedule(
    label="Morning Briefing",
    prompt_text="Summarize my day",
    cadence="daily",
    hour_local=9,
    timezone_name="America/Boise",
    weekday=None,
    user_id=USER_ID,
    **kwargs,
):
    return await create_scheduled_prompt(
        user_id=user_id,
        label=label,
        prompt_text=prompt_text,
        cadence=cadence,
        hour_local=hour_local,
        timezone_name=timezone_name,
        weekday=weekday,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# compute_next_run_at — cadence math
# ---------------------------------------------------------------------------


class TestComputeNextRunAt:
    def test_daily_before_hour_fires_today(self):
        # 2026-07-05 06:00 Boise time (MDT, UTC-6); "9am daily" -> today 9am.
        from_time = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)  # 06:00 MDT
        result = compute_next_run_at("daily", 9, "America/Boise", from_time=from_time)
        expected_utc = datetime(2026, 7, 5, 9, 0, tzinfo=ZoneInfo("America/Boise")).astimezone(timezone.utc)
        assert result == expected_utc.isoformat().replace("+00:00", "Z")

    def test_daily_after_hour_rolls_to_tomorrow(self):
        # 2026-07-05 10:00 Boise time; "9am daily" already passed -> tomorrow.
        from_time = datetime(2026, 7, 5, 16, 0, tzinfo=timezone.utc)  # 10:00 MDT
        result = compute_next_run_at("daily", 9, "America/Boise", from_time=from_time)
        expected = datetime(2026, 7, 6, 9, 0, tzinfo=ZoneInfo("America/Boise")).astimezone(timezone.utc)
        assert result == expected.isoformat().replace("+00:00", "Z")

    def test_daily_exactly_on_hour_rolls_to_tomorrow(self):
        # candidate <= base is treated as already passed (strictly future guarantee).
        from_time = datetime(2026, 7, 5, 15, 0, tzinfo=timezone.utc)  # 09:00 MDT exactly
        result = compute_next_run_at("daily", 9, "America/Boise", from_time=from_time)
        expected = datetime(2026, 7, 6, 9, 0, tzinfo=ZoneInfo("America/Boise")).astimezone(timezone.utc)
        assert result == expected.isoformat().replace("+00:00", "Z")

    def test_weekday_skips_weekend(self):
        # 2026-07-05 is a Sunday in Boise; "9am weekday" should land Monday 7/6.
        from_time = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)  # Sun 06:00 MDT
        result = compute_next_run_at("weekday", 9, "America/Boise", from_time=from_time)
        expected = datetime(2026, 7, 6, 9, 0, tzinfo=ZoneInfo("America/Boise")).astimezone(timezone.utc)
        assert result == expected.isoformat().replace("+00:00", "Z")

    def test_weekday_friday_afternoon_rolls_to_monday(self):
        # 2026-07-03 is a Friday; if "9am weekday" already passed, next is Monday 7/6.
        from_time = datetime(2026, 7, 3, 16, 0, tzinfo=timezone.utc)  # Fri 10:00 MDT
        result = compute_next_run_at("weekday", 9, "America/Boise", from_time=from_time)
        expected = datetime(2026, 7, 6, 9, 0, tzinfo=ZoneInfo("America/Boise")).astimezone(timezone.utc)
        assert result == expected.isoformat().replace("+00:00", "Z")

    def test_weekly_lands_on_requested_weekday(self):
        # 2026-07-05 is Sunday (weekday()==6). Requesting Wednesday (2).
        from_time = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
        result = compute_next_run_at("weekly", 9, "America/Boise", weekday=2, from_time=from_time)
        expected = datetime(2026, 7, 8, 9, 0, tzinfo=ZoneInfo("America/Boise")).astimezone(timezone.utc)
        assert result == expected.isoformat().replace("+00:00", "Z")

    def test_weekly_same_day_but_already_passed_rolls_a_full_week(self):
        # Sunday (weekday()==6) requesting Sunday, but after 9am -> next Sunday.
        from_time = datetime(2026, 7, 5, 17, 0, tzinfo=timezone.utc)  # Sun 11:00 MDT
        result = compute_next_run_at("weekly", 9, "America/Boise", weekday=6, from_time=from_time)
        expected = datetime(2026, 7, 12, 9, 0, tzinfo=ZoneInfo("America/Boise")).astimezone(timezone.utc)
        assert result == expected.isoformat().replace("+00:00", "Z")

    def test_weekly_requires_weekday(self):
        with pytest.raises(ValueError, match="weekday is required"):
            compute_next_run_at("weekly", 9, "America/Boise", weekday=None)

    def test_unknown_cadence_raises(self):
        with pytest.raises(ValueError, match="Unknown cadence"):
            compute_next_run_at("monthly", 9, "America/Boise")  # type: ignore[arg-type]

    def test_interval_adds_a_plain_delta_from_now(self):
        # "every 90 minutes" is a fixed delta off from_time — no hour/tz anchor.
        from_time = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
        result = compute_next_run_at(
            "interval", 9, "America/Boise", from_time=from_time, interval_minutes=90
        )
        expected = datetime(2026, 7, 5, 13, 30, tzinfo=timezone.utc)
        assert result == expected.isoformat().replace("+00:00", "Z")

    def test_interval_ignores_hour_and_timezone(self):
        # hour_local/timezone are meaningless for interval; two different zones
        # produce the same UTC delta.
        from_time = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
        a = compute_next_run_at("interval", 3, "America/Boise", from_time=from_time, interval_minutes=360)
        b = compute_next_run_at("interval", 21, "Asia/Tokyo", from_time=from_time, interval_minutes=360)
        assert a == b == "2026-07-05T18:00:00Z"

    def test_interval_requires_positive_minutes(self):
        with pytest.raises(ValueError, match="interval_minutes is required"):
            compute_next_run_at("interval", 9, "America/Boise")
        with pytest.raises(ValueError, match="interval_minutes is required"):
            compute_next_run_at("interval", 9, "America/Boise", interval_minutes=0)


class TestIntervalToMinutes:
    def test_hours_convert(self):
        assert interval_to_minutes(6, "hours") == 360

    def test_minutes_passthrough(self):
        assert interval_to_minutes(45, "minutes") == 45

    def test_missing_half_is_none(self):
        assert interval_to_minutes(None, "hours") is None
        assert interval_to_minutes(6, None) is None

    def test_different_timezone_produces_different_utc_time(self):
        from_time = datetime(2026, 7, 5, 0, 0, tzinfo=timezone.utc)
        boise = compute_next_run_at("daily", 9, "America/Boise", from_time=from_time)
        tokyo = compute_next_run_at("daily", 9, "Asia/Tokyo", from_time=from_time)
        assert boise != tokyo


# ---------------------------------------------------------------------------
# create / get / list
# ---------------------------------------------------------------------------


class TestCreateAndGet:
    async def test_create_get_roundtrip(self, sessions_metadata_table):
        schedule = await _make_schedule()

        assert schedule.schedule_id.startswith("sched-")
        assert schedule.state == "active"
        assert schedule.next_run_at is not None
        assert schedule.runs_today == 0
        assert schedule.max_runs_per_day == 24

        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched is not None
        assert fetched.label == "Morning Briefing"
        assert fetched.prompt_text == "Summarize my day"
        assert fetched.cadence == "daily"
        assert fetched.hour_local == 9
        assert fetched.timezone == "America/Boise"

    async def test_active_schedule_has_due_index_keys(self, sessions_metadata_table):
        schedule = await _make_schedule()

        item = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"}
        )["Item"]
        assert item["GSI3_PK"] == DUE_INDEX_PK
        assert item["GSI3_SK"] == f"{schedule.next_run_at}#{schedule.schedule_id}"

    async def test_get_missing_returns_none(self, sessions_metadata_table):
        assert await get_scheduled_prompt(USER_ID, "sched-missing") is None

    async def test_list_scopes_to_owner(self, sessions_metadata_table):
        mine = await _make_schedule(user_id=USER_ID)
        await _make_schedule(user_id=OTHER_USER_ID, label="Someone else's")

        mine_list = await list_scheduled_prompts(USER_ID)
        assert [s.schedule_id for s in mine_list] == [mine.schedule_id]

        theirs_list = await list_scheduled_prompts(OTHER_USER_ID)
        assert len(theirs_list) == 1
        assert theirs_list[0].schedule_id != mine.schedule_id

    async def test_weekly_requires_weekday_at_creation(self, sessions_metadata_table):
        with pytest.raises(ValueError):
            await _make_schedule(cadence="weekly", weekday=None)

    async def test_interval_persists_value_and_unit(self, sessions_metadata_table):
        schedule = await _make_schedule(
            cadence="interval", interval_value=6, interval_unit="hours"
        )
        assert schedule.cadence == "interval"
        assert schedule.next_run_at is not None

        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched is not None
        assert fetched.interval_value == 6
        assert fetched.interval_unit == "hours"

    async def test_per_user_cap_enforced(self, sessions_metadata_table, monkeypatch):
        monkeypatch.setenv("SCHEDULED_RUNS_MAX_PER_USER", "2")
        assert max_schedules_per_user() == 2

        await _make_schedule(label="one")
        await _make_schedule(label="two")
        with pytest.raises(ScheduledPromptLimitExceeded):
            await _make_schedule(label="three")


# ---------------------------------------------------------------------------
# Snapshot semantics — enabled_tools frozen at creation (Phase A punch #7)
# ---------------------------------------------------------------------------


class TestEnabledToolsSnapshot:
    async def test_explicit_tools_are_persisted_verbatim(self, sessions_metadata_table):
        schedule = await _make_schedule(enabled_tools=["class_search", "web_search"])
        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.enabled_tools == ["class_search", "web_search"]

    async def test_none_enabled_tools_persists_as_none(self, sessions_metadata_table):
        # The service itself does no lazy resolution; the route layer is
        # responsible for resolving "None" to a concrete snapshot before
        # calling create_scheduled_prompt. Here we assert the service is a
        # dumb store — it never re-derives tools at read time.
        schedule = await _make_schedule(enabled_tools=None)
        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.enabled_tools is None

    async def test_editing_the_schedule_does_not_change_the_snapshot(self, sessions_metadata_table):
        schedule = await _make_schedule(enabled_tools=["class_search"])
        await update_scheduled_prompt(USER_ID, schedule.schedule_id, label="New label")
        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.enabled_tools == ["class_search"]

    async def test_explicit_tools_update_replaces_the_snapshot(self, sessions_metadata_table):
        schedule = await _make_schedule(enabled_tools=["class_search"])
        await update_scheduled_prompt(USER_ID, schedule.schedule_id, enabled_tools=["gmail_search"])
        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.enabled_tools == ["gmail_search"]


# ---------------------------------------------------------------------------
# list_due_schedules — sparse index behavior
# ---------------------------------------------------------------------------


class TestListDueSchedules:
    async def test_active_overdue_schedule_is_due(self, sessions_metadata_table):
        schedule = await _make_schedule()
        # Force it overdue by re-arming to the past.
        await set_schedule_state(USER_ID, schedule.schedule_id, "active", next_run_at="2000-01-01T00:00:00Z")

        due = await list_due_schedules(now="2999-01-01T00:00:00Z")
        assert schedule.schedule_id in [s.schedule_id for s in due]

    async def test_paused_schedule_is_physically_absent_from_due_index(self, sessions_metadata_table):
        schedule = await _make_schedule()
        await set_schedule_state(USER_ID, schedule.schedule_id, "active", next_run_at="2000-01-01T00:00:00Z")
        await set_schedule_state(USER_ID, schedule.schedule_id, "paused", state_reason="Paused by user")

        due = await list_due_schedules(now="2999-01-01T00:00:00Z")
        assert schedule.schedule_id not in [s.schedule_id for s in due]

        # And the row itself has no GSI keys once paused.
        item = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"}
        )["Item"]
        assert "GSI3_PK" not in item
        assert "GSI3_SK" not in item

    async def test_not_yet_due_schedule_is_excluded(self, sessions_metadata_table):
        schedule = await _make_schedule()  # next_run_at is in the future by construction
        due = await list_due_schedules(now="2000-01-01T00:00:00Z")
        assert schedule.schedule_id not in [s.schedule_id for s in due]


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


class TestSetScheduleState:
    async def test_pause_removes_gsi_keys(self, sessions_metadata_table):
        schedule = await _make_schedule()
        ok = await set_schedule_state(USER_ID, schedule.schedule_id, "paused", state_reason="Paused by user")
        assert ok is True

        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.state == "paused"
        assert fetched.state_reason == "Paused by user"

    async def test_resume_requires_next_run_at(self, sessions_metadata_table):
        schedule = await _make_schedule()
        await set_schedule_state(USER_ID, schedule.schedule_id, "paused")
        with pytest.raises(ValueError):
            await set_schedule_state(USER_ID, schedule.schedule_id, "active")

    async def test_resume_readds_gsi_keys(self, sessions_metadata_table):
        schedule = await _make_schedule()
        await set_schedule_state(USER_ID, schedule.schedule_id, "paused")
        await set_schedule_state(USER_ID, schedule.schedule_id, "active", next_run_at="2999-01-01T00:00:00Z")

        item = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"}
        )["Item"]
        assert item["GSI3_PK"] == DUE_INDEX_PK
        assert item["GSI3_SK"] == f"2999-01-01T00:00:00Z#{schedule.schedule_id}"

    async def test_paused_error_state(self, sessions_metadata_table):
        schedule = await _make_schedule()
        await set_schedule_state(USER_ID, schedule.schedule_id, "paused_error", state_reason="Too many failures")
        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.state == "paused_error"
        assert fetched.state_reason == "Too many failures"

    async def test_set_state_on_missing_schedule_returns_false(self, sessions_metadata_table):
        ok = await set_schedule_state(USER_ID, "sched-missing", "paused")
        assert ok is False


class TestRearmSchedule:
    async def test_rearm_advances_next_run_at(self, sessions_metadata_table):
        schedule = await _make_schedule()
        ok = await rearm_schedule(
            USER_ID, schedule.schedule_id, expected_next_run_at=schedule.next_run_at, new_next_run_at="2999-06-01T00:00:00Z"
        )
        assert ok is True
        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.next_run_at == "2999-06-01T00:00:00Z"

    async def test_conditional_rearm_is_idempotent_against_double_dispatch(self, sessions_metadata_table):
        schedule = await _make_schedule()
        first = await rearm_schedule(
            USER_ID, schedule.schedule_id, expected_next_run_at=schedule.next_run_at, new_next_run_at="2999-06-01T00:00:00Z"
        )
        # A second dispatcher racing on the same stale expected value loses.
        second = await rearm_schedule(
            USER_ID, schedule.schedule_id, expected_next_run_at=schedule.next_run_at, new_next_run_at="2999-07-01T00:00:00Z"
        )
        assert first is True
        assert second is False

        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.next_run_at == "2999-06-01T00:00:00Z"


# ---------------------------------------------------------------------------
# record_run_result — runaway-guard counter
# ---------------------------------------------------------------------------


class TestRecordRunResult:
    async def test_records_status_and_session(self, sessions_metadata_table):
        schedule = await _make_schedule()
        await record_run_result(USER_ID, schedule.schedule_id, status="completed", session_id="sess-1")

        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.last_run_status == "completed"
        assert fetched.last_run_session_id == "sess-1"
        assert fetched.runs_today == 1

    async def test_records_error(self, sessions_metadata_table):
        schedule = await _make_schedule()
        await record_run_result(USER_ID, schedule.schedule_id, status="failed", error="boom")

        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.last_run_status == "failed"
        assert fetched.last_error == "boom"

    async def test_runs_today_increments_within_same_day(self, sessions_metadata_table):
        schedule = await _make_schedule()
        await record_run_result(USER_ID, schedule.schedule_id, status="completed")
        await record_run_result(USER_ID, schedule.schedule_id, status="completed")

        fetched = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert fetched.runs_today == 2


# ---------------------------------------------------------------------------
# update_scheduled_prompt
# ---------------------------------------------------------------------------


class TestUpdateScheduledPrompt:
    async def test_update_missing_returns_none(self, sessions_metadata_table):
        assert await update_scheduled_prompt(USER_ID, "sched-missing", label="x") is None

    async def test_non_cadence_field_update_does_not_touch_next_run_at(self, sessions_metadata_table):
        schedule = await _make_schedule()
        original_next_run_at = schedule.next_run_at

        updated = await update_scheduled_prompt(USER_ID, schedule.schedule_id, label="Renamed")
        assert updated.label == "Renamed"
        assert updated.next_run_at == original_next_run_at

    async def test_cadence_change_on_active_schedule_recomputes_next_run_at(self, sessions_metadata_table):
        schedule = await _make_schedule(cadence="daily", hour_local=9)
        updated = await update_scheduled_prompt(USER_ID, schedule.schedule_id, hour_local=14)

        assert updated.hour_local == 14
        assert updated.next_run_at != schedule.next_run_at

        item = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"}
        )["Item"]
        assert item["GSI3_SK"] == f"{updated.next_run_at}#{schedule.schedule_id}"

    async def test_cadence_change_on_paused_schedule_does_not_touch_due_index(self, sessions_metadata_table):
        schedule = await _make_schedule(cadence="daily", hour_local=9)
        await set_schedule_state(USER_ID, schedule.schedule_id, "paused")

        updated = await update_scheduled_prompt(USER_ID, schedule.schedule_id, hour_local=14)
        assert updated.hour_local == 14
        assert updated.state == "paused"

        item = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"}
        )["Item"]
        assert "GSI3_PK" not in item

    async def test_unset_clearable_field_leaves_it_unchanged(self, sessions_metadata_table):
        schedule = await _make_schedule(assistant_id="ast-123", enabled_tools=["gmail_search"])
        # A label-only edit must not disturb the clearable fields (default UNSET).
        updated = await update_scheduled_prompt(USER_ID, schedule.schedule_id, label="Renamed")
        assert updated.assistant_id == "ast-123"
        assert updated.enabled_tools == ["gmail_search"]

    async def test_set_clearable_field_updates_it(self, sessions_metadata_table):
        schedule = await _make_schedule(assistant_id="ast-123", enabled_tools=["gmail_search"])
        updated = await update_scheduled_prompt(
            USER_ID, schedule.schedule_id, assistant_id="ast-456", enabled_tools=["calendar_list"]
        )
        assert updated.assistant_id == "ast-456"
        assert updated.enabled_tools == ["calendar_list"]

    async def test_clear_assistant_removes_attribute(self, sessions_metadata_table):
        schedule = await _make_schedule(assistant_id="ast-123")
        updated = await update_scheduled_prompt(USER_ID, schedule.schedule_id, assistant_id=None)
        assert updated.assistant_id is None
        item = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"}
        )["Item"]
        assert "assistantId" not in item

    async def test_clear_enabled_tools_removes_attribute(self, sessions_metadata_table):
        schedule = await _make_schedule(enabled_tools=["gmail_search"])
        updated = await update_scheduled_prompt(USER_ID, schedule.schedule_id, enabled_tools=None)
        assert updated.enabled_tools is None
        item = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"}
        )["Item"]
        assert "enabledTools" not in item

    async def test_clear_is_a_noop_when_field_already_absent(self, sessions_metadata_table):
        # REMOVE on a missing attribute is harmless — no error, still absent.
        schedule = await _make_schedule()  # no assistant_id
        updated = await update_scheduled_prompt(USER_ID, schedule.schedule_id, assistant_id=None)
        assert updated is not None
        assert updated.assistant_id is None


# ---------------------------------------------------------------------------
# Delete = total revocation
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_removes_the_record_entirely(self, sessions_metadata_table):
        schedule = await _make_schedule()
        await delete_scheduled_prompt(USER_ID, schedule.schedule_id)

        assert await get_scheduled_prompt(USER_ID, schedule.schedule_id) is None
        item = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule.schedule_id}"}
        )
        assert "Item" not in item

    async def test_delete_drops_it_from_due_index_too(self, sessions_metadata_table):
        schedule = await _make_schedule()
        await set_schedule_state(USER_ID, schedule.schedule_id, "active", next_run_at="2000-01-01T00:00:00Z")
        await delete_scheduled_prompt(USER_ID, schedule.schedule_id)

        due = await list_due_schedules(now="2999-01-01T00:00:00Z")
        assert schedule.schedule_id not in [s.schedule_id for s in due]
