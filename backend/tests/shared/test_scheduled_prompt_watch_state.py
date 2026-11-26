"""Quiet watcher storage (docs/specs/quiet-watcher.md §3.1, §3.2, §3.4), on moto DynamoDB.

Covers:
- a schedule's ``mode`` and ``modelId``: stored, cleared, and absent from a
  standard schedule's record
- the ``SCHEDSTATE#`` store: conditional versions, clamping, ``append_seen``,
  clear, and the cascade when the schedule is deleted
- the ``SCHEDRUN#`` rows: what one holds, the TTL, newest-first listing, and
  that a project schedule and a failing write leave nothing and raise nothing
"""

import time

import pytest

from apis.shared.scheduled_prompts.models import ScheduledPrompt
from apis.shared.scheduled_prompts.runs import (
    RUN_TTL_SECONDS,
    list_schedule_runs,
    record_schedule_run,
    record_schedule_run_answered_elsewhere,
    record_schedule_run_resume,
)
from apis.shared.scheduled_prompts.service import (
    UNSET,
    create_scheduled_prompt,
    delete_scheduled_prompt,
    get_scheduled_prompt,
    update_scheduled_prompt,
)
from apis.shared.scheduled_prompts.watch_state import (
    MAX_CURSORS,
    MAX_SEEN,
    MAX_TEXT_CHARS,
    MAX_TRACKING,
    WatchState,
    append_seen,
    clear_watch_state,
    get_watch_state,
    save_watch_state,
)

USER_ID = "user-1"
OTHER_USER_ID = "user-2"


async def _make_schedule(user_id: str = USER_ID, **kwargs) -> ScheduledPrompt:
    return await create_scheduled_prompt(
        user_id=user_id,
        label="Inbox watcher",
        prompt_text="Tell me what needs me",
        cadence="interval",
        hour_local=0,
        timezone_name="America/Boise",
        interval_value=15,
        interval_unit="minutes",
        **kwargs,
    )


def _raw_schedule(table, schedule_id: str, user_id: str = USER_ID) -> dict:
    return table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"SCHEDPROMPT#{schedule_id}"})["Item"]


# ---------------------------------------------------------------------------
# mode and modelId on the schedule
# ---------------------------------------------------------------------------


class TestScheduleFields:
    @pytest.mark.asyncio
    async def test_a_standard_schedule_stores_neither_field(self, sessions_metadata_table):
        schedule = await _make_schedule()
        assert schedule.mode == "standard"
        assert schedule.model_id is None
        item = _raw_schedule(sessions_metadata_table, schedule.schedule_id)
        assert "mode" not in item
        assert "modelId" not in item

    @pytest.mark.asyncio
    async def test_a_watcher_stores_its_mode_and_model(self, sessions_metadata_table):
        schedule = await _make_schedule(mode="watch", model_id="us.anthropic.claude-haiku-4-5")
        item = _raw_schedule(sessions_metadata_table, schedule.schedule_id)
        assert item["mode"] == "watch"
        assert item["modelId"] == "us.anthropic.claude-haiku-4-5"
        read = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert (read.mode, read.model_id) == ("watch", "us.anthropic.claude-haiku-4-5")

    @pytest.mark.asyncio
    async def test_update_sets_and_clears_the_model_and_mode(self, sessions_metadata_table):
        schedule = await _make_schedule()

        updated = await update_scheduled_prompt(USER_ID, schedule.schedule_id, mode="watch", model_id="m-1")
        assert (updated.mode, updated.model_id) == ("watch", "m-1")

        untouched = await update_scheduled_prompt(USER_ID, schedule.schedule_id, label="Renamed", model_id=UNSET)
        assert (untouched.mode, untouched.model_id) == ("watch", "m-1")

        cleared = await update_scheduled_prompt(USER_ID, schedule.schedule_id, mode="standard", model_id=None)
        assert (cleared.mode, cleared.model_id) == ("standard", None)
        item = _raw_schedule(sessions_metadata_table, schedule.schedule_id)
        assert "mode" not in item
        assert "modelId" not in item


# ---------------------------------------------------------------------------
# append_seen and clamping
# ---------------------------------------------------------------------------


class TestAppendSeen:
    def test_appends_newest_last_and_dedupes(self):
        assert append_seen(["a", "b"], ["c", "a"]) == ["b", "c", "a"]

    def test_drops_the_oldest_past_the_cap(self):
        assert append_seen(["a", "b", "c"], ["d", "e"], cap=3) == ["c", "d", "e"]

    def test_skips_empty_keys_and_does_not_mutate(self):
        seen = ["a"]
        assert append_seen(seen, ["", "b"]) == ["a", "b"]
        assert seen == ["a"]

    def test_default_cap_is_500(self):
        assert len(append_seen([], [f"k{i}" for i in range(MAX_SEEN + 50)])) == MAX_SEEN


class TestClamping:
    def test_bounds_are_truncated_never_raised(self):
        long = "x" * (MAX_TEXT_CHARS + 50)
        state = WatchState(
            cursors={f"c{i}": long for i in range(MAX_CURSORS + 5)},
            tracking=[long] * (MAX_TRACKING + 5),
            seen=[f"k{i}" for i in range(MAX_SEEN + 5)],
        )
        assert len(state.cursors) == MAX_CURSORS
        assert all(len(v) == MAX_TEXT_CHARS for v in state.cursors.values())
        assert len(state.tracking) == MAX_TRACKING
        assert all(len(t) == MAX_TEXT_CHARS for t in state.tracking)
        assert len(state.seen) == MAX_SEEN
        assert state.seen[-1] == f"k{MAX_SEEN + 4}"

    def test_wrong_shapes_read_as_empty(self):
        state = WatchState.model_validate({"cursors": "nope", "tracking": None, "seen": 5})
        assert (state.cursors, state.tracking, state.seen) == ({}, [], [])

    def test_serializes_camel_case(self):
        dumped = WatchState(last_run_at="2026-11-25T00:00:00Z").model_dump(by_alias=True)
        assert dumped["lastRunAt"] == "2026-11-25T00:00:00Z"


# ---------------------------------------------------------------------------
# The SCHEDSTATE# store
# ---------------------------------------------------------------------------


class TestWatchStateStore:
    def test_a_watcher_with_no_state_reads_none(self, sessions_metadata_table):
        assert get_watch_state(USER_ID, "sched-000000000000") is None

    def test_first_write_needs_no_state_and_sets_version_1(self, sessions_metadata_table):
        state = WatchState(cursors={"since": "2026-11-25T00:00:00Z"}, tracking=["PR 12"], seen=["a"])
        assert save_watch_state(USER_ID, "sched-1", state, expected_version=None) is True
        read = get_watch_state(USER_ID, "sched-1")
        assert read.version == 1
        assert read.cursors == {"since": "2026-11-25T00:00:00Z"}
        assert read.tracking == ["PR 12"]
        assert read.seen == ["a"]
        assert read.updated_at

        item = sessions_metadata_table.get_item(Key={"PK": f"USER#{USER_ID}", "SK": "SCHEDSTATE#sched-1"})["Item"]
        assert item["version"] == 1

    def test_a_second_first_write_loses(self, sessions_metadata_table):
        assert save_watch_state(USER_ID, "sched-1", WatchState(seen=["a"]), expected_version=None)
        assert save_watch_state(USER_ID, "sched-1", WatchState(seen=["b"]), expected_version=None) is False
        assert get_watch_state(USER_ID, "sched-1").seen == ["a"]

    def test_a_write_naming_the_read_version_advances_it(self, sessions_metadata_table):
        save_watch_state(USER_ID, "sched-1", WatchState(seen=["a"]), expected_version=None)
        read = get_watch_state(USER_ID, "sched-1")
        nxt = WatchState(seen=append_seen(read.seen, ["b"]), last_run_at="2026-11-25T01:00:00Z")
        assert save_watch_state(USER_ID, "sched-1", nxt, expected_version=read.version) is True
        after = get_watch_state(USER_ID, "sched-1")
        assert after.version == 2
        assert after.seen == ["a", "b"]
        assert after.last_run_at == "2026-11-25T01:00:00Z"

    def test_two_runs_holding_the_same_read_cannot_both_advance(self, sessions_metadata_table):
        save_watch_state(USER_ID, "sched-1", WatchState(), expected_version=None)
        assert save_watch_state(USER_ID, "sched-1", WatchState(seen=["x"]), expected_version=1) is True
        assert save_watch_state(USER_ID, "sched-1", WatchState(seen=["y"]), expected_version=1) is False
        assert get_watch_state(USER_ID, "sched-1").seen == ["x"]

    def test_a_write_against_missing_state_with_a_version_loses(self, sessions_metadata_table):
        assert save_watch_state(USER_ID, "sched-1", WatchState(), expected_version=3) is False
        assert get_watch_state(USER_ID, "sched-1") is None

    def test_the_write_is_clamped(self, sessions_metadata_table):
        state = WatchState.model_construct(
            cursors={}, tracking=["t" * 500] * 30, seen=[], version=0, last_run_at=None, updated_at=None
        )
        assert save_watch_state(USER_ID, "sched-1", state, expected_version=None)
        read = get_watch_state(USER_ID, "sched-1")
        assert len(read.tracking) == MAX_TRACKING
        assert len(read.tracking[0]) == MAX_TEXT_CHARS

    def test_clear_removes_it_and_is_idempotent(self, sessions_metadata_table):
        save_watch_state(USER_ID, "sched-1", WatchState(seen=["a"]), expected_version=None)
        assert clear_watch_state(USER_ID, "sched-1") is True
        assert get_watch_state(USER_ID, "sched-1") is None
        assert clear_watch_state(USER_ID, "sched-1") is True
        # Cleared, the next run starts again from no state.
        assert save_watch_state(USER_ID, "sched-1", WatchState(), expected_version=None) is True

    def test_state_is_scoped_by_user(self, sessions_metadata_table):
        save_watch_state(USER_ID, "sched-1", WatchState(seen=["a"]), expected_version=None)
        assert get_watch_state(OTHER_USER_ID, "sched-1") is None

    @pytest.mark.asyncio
    async def test_deleting_the_schedule_deletes_its_state(self, sessions_metadata_table):
        schedule = await _make_schedule(mode="watch")
        save_watch_state(USER_ID, schedule.schedule_id, WatchState(seen=["a"]), expected_version=None)
        await delete_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert get_watch_state(USER_ID, schedule.schedule_id) is None

    @pytest.mark.asyncio
    async def test_a_failed_state_delete_never_fails_the_schedule_delete(self, sessions_metadata_table, monkeypatch):
        from apis.shared.scheduled_prompts import watch_state

        schedule = await _make_schedule()

        def boom(user_id, schedule_id):
            raise RuntimeError("throttled")

        monkeypatch.setattr(watch_state, "clear_watch_state", boom)
        assert await delete_scheduled_prompt(USER_ID, schedule.schedule_id) is True
        assert await get_scheduled_prompt(USER_ID, schedule.schedule_id) is None


# ---------------------------------------------------------------------------
# The SCHEDRUN# rows
# ---------------------------------------------------------------------------


def _schedule(project_id=None) -> ScheduledPrompt:
    return ScheduledPrompt.model_validate(
        dict(
            scheduleId="sched-aaaaaaaaaaaa",
            userId=USER_ID,
            label="Inbox watcher",
            promptText="Go",
            cadence="daily",
            hourLocal=9,
            timezone="America/Boise",
            projectId=project_id,
            createdAt="2026-11-25T00:00:00Z",
            updatedAt="2026-11-25T00:00:00Z",
        )
    )


class TestRunRows:
    def test_a_row_holds_the_run_and_its_tokens(self, sessions_metadata_table):
        before = int(time.time())
        record_schedule_run(
            _schedule(),
            run_id="run-1",
            status="completed",
            session_id="headless-1",
            finished_at="2026-11-25T01:00:00Z",
            usage={"usage": {"inputTokens": 2100, "outputTokens": 80, "cacheReadInputTokens": 1500}},
        )
        item = sessions_metadata_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": "SCHEDRUN#sched-aaaaaaaaaaaa#2026-11-25T01:00:00Z#run-1"}
        )["Item"]
        assert item["runId"] == "run-1"
        assert item["scheduleId"] == "sched-aaaaaaaaaaaa"
        assert item["status"] == "completed"
        assert item["itemCount"] == 0
        assert item["sessionId"] == "headless-1"
        assert item["finishedAt"] == "2026-11-25T01:00:00Z"
        assert (item["inputTokens"], item["outputTokens"], item["cacheReadTokens"]) == (2100, 80, 1500)
        # The table's TTL attribute is ``ttl`` (cost-tracking-tables-construct.ts).
        assert before + RUN_TTL_SECONDS <= int(item["ttl"]) <= int(time.time()) + RUN_TTL_SECONDS

    def test_no_session_and_no_usage(self, sessions_metadata_table):
        record_schedule_run(
            _schedule(), run_id="run-2", status="error", session_id=None, finished_at="2026-11-25T02:00:00Z", usage=None
        )
        (run,) = list_schedule_runs(USER_ID, "sched-aaaaaaaaaaaa")
        assert run.session_id is None
        assert (run.input_tokens, run.output_tokens, run.cache_read_tokens) == (0, 0, 0)
        assert run.model_dump(by_alias=True)["status"] == "error"

    def test_lists_newest_first_and_only_this_schedule(self, sessions_metadata_table):
        schedule = _schedule()
        for i, ts in enumerate(["2026-11-25T01:00:00Z", "2026-11-25T03:00:00Z", "2026-11-25T02:00:00Z"]):
            record_schedule_run(schedule, run_id=f"run-{i}", status="completed", session_id=None, finished_at=ts)
        other = _schedule().model_copy(update={"schedule_id": "sched-bbbbbbbbbbbb"})
        record_schedule_run(other, run_id="run-x", status="completed", session_id=None, finished_at="2026-11-25T09:00:00Z")

        runs = list_schedule_runs(USER_ID, "sched-aaaaaaaaaaaa")
        assert [r.finished_at for r in runs] == [
            "2026-11-25T03:00:00Z",
            "2026-11-25T02:00:00Z",
            "2026-11-25T01:00:00Z",
        ]
        assert list_schedule_runs(USER_ID, "sched-aaaaaaaaaaaa", limit=1)[0].run_id == "run-1"
        assert list_schedule_runs(OTHER_USER_ID, "sched-aaaaaaaaaaaa") == []

    def test_an_answer_in_the_conversation_settles_a_waiting_row(self, sessions_metadata_table):
        key = {"PK": f"USER#{USER_ID}", "SK": "SCHEDRUN#sched-aaaaaaaaaaaa#2026-11-25T01:00:00Z#run-1"}
        record_schedule_run(
            _schedule(), run_id="run-1", status="needs_user", session_id="headless-1",
            finished_at="2026-11-25T01:00:00Z", expires_at="2026-12-02T01:00:00Z", prompt_count=1,
        )
        assert record_schedule_run_answered_elsewhere(
            USER_ID, "sched-aaaaaaaaaaaa", run_finished_at="2026-11-25T01:00:00Z", run_id="run-1"
        )
        item = sessions_metadata_table.get_item(Key=key)["Item"]
        assert item["resolution"] == "answered_elsewhere"
        assert "expiresAt" not in item
        assert item["status"] == "needs_user"

    def test_an_inbox_answer_already_on_the_row_keeps_it(self, sessions_metadata_table):
        record_schedule_run(
            _schedule(), run_id="run-1", status="needs_user", session_id="headless-1",
            finished_at="2026-11-25T01:00:00Z", expires_at="2026-12-02T01:00:00Z",
        )
        record_schedule_run_resume(
            USER_ID, "sched-aaaaaaaaaaaa", run_finished_at="2026-11-25T01:00:00Z", run_id="run-1",
            resolution="approved", resume_run_id="run-r", resumed_at="2026-11-25T02:00:00Z",
            resume_outcome="finished",
        )
        assert not record_schedule_run_answered_elsewhere(
            USER_ID, "sched-aaaaaaaaaaaa", run_finished_at="2026-11-25T01:00:00Z", run_id="run-1"
        )
        (run,) = list_schedule_runs(USER_ID, "sched-aaaaaaaaaaaa")
        assert run.model_dump(by_alias=True)["resolution"] == "approved"

    def test_a_missing_row_is_not_created(self, sessions_metadata_table):
        assert not record_schedule_run_answered_elsewhere(
            USER_ID, "sched-aaaaaaaaaaaa", run_finished_at="2026-11-25T01:00:00Z", run_id="run-gone"
        )
        assert list_schedule_runs(USER_ID, "sched-aaaaaaaaaaaa") == []

    def test_a_project_schedule_gets_no_row(self, sessions_metadata_table):
        record_schedule_run(
            _schedule(project_id="proj-1"), run_id="run-1", status="completed", session_id=None,
            finished_at="2026-11-25T01:00:00Z",
        )
        assert list_schedule_runs(USER_ID, "sched-aaaaaaaaaaaa") == []

    def test_a_failed_write_never_raises(self, monkeypatch):
        from apis.shared.scheduled_prompts import service

        def broken():
            raise RuntimeError("no table")

        monkeypatch.setattr(service, "_get_table", broken)
        record_schedule_run(
            _schedule(), run_id="run-1", status="completed", session_id=None, finished_at="2026-11-25T01:00:00Z"
        )
