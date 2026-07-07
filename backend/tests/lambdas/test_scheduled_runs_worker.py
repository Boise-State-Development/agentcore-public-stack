"""Scheduled-runs worker tests (moto DynamoDB, stubbed run_agent_headless).

Mirrors backend/tests/lambdas/test_kb_sync_worker.py's structure: the real
scheduled_prompts service manages schedule state so the worker's raw
bookkeeping is cross-checked against the actual storage schema; only
run_agent_headless (the harness's HTTP/SSE boundary) is stubbed.
"""

from datetime import datetime, timezone

import pytest

from apis.shared.harness.auth import HeadlessAuthError
from apis.shared.harness.models import OAuthConsentRequired, RunResult
from apis.shared.scheduled_prompts.service import create_scheduled_prompt, get_scheduled_prompt
from lambdas.scheduled_runs_worker import worker

pytestmark = pytest.mark.asyncio

USER_ID = "user-1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _result(status: str, **overrides) -> RunResult:
    base = dict(
        run_id="run-1",
        session_id="sess-1",
        user_id=USER_ID,
        status=status,
        started_at=_now_iso(),
        finished_at=_now_iso(),
    )
    base.update(overrides)
    return RunResult(**base)


async def _make_schedule(**overrides):
    kwargs = dict(
        user_id=USER_ID,
        label="Morning Briefing",
        prompt_text="Summarize my day",
        cadence="daily",
        hour_local=9,
        timezone_name="America/Boise",
    )
    kwargs.update(overrides)
    return await create_scheduled_prompt(**kwargs)


def _payload(schedule):
    return {"scheduleId": schedule.schedule_id, "userId": USER_ID}


class TestSuccess:
    async def test_completed_run_records_session(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _make_schedule()

        async def fake_run(**kwargs):
            assert kwargs["user_id"] == USER_ID
            assert kwargs["prompt"] == schedule.prompt_text
            assert kwargs["trigger"] == "schedule"
            return _result("completed", session_id="sess-abc")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)

        result = await worker.run_schedule(_payload(schedule))

        assert result["result"] == "completed"
        assert result["sessionId"] == "sess-abc"
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.last_run_status == "completed"
        assert updated.last_run_session_id == "sess-abc"
        assert updated.state == "active"


class TestHeadlessAuthFailure:
    async def test_auth_error_pauses_reauth_required(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _make_schedule()

        async def fake_run(**kwargs):
            raise HeadlessAuthError("no active grant")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)

        result = await worker.run_schedule(_payload(schedule))

        assert result["result"] == "paused_error"
        assert result["reason"] == "reauth_required"
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.state == "paused_error"
        assert updated.state_reason == "reauth_required"
        assert updated.last_run_status == "error"


class TestOAuthRequired:
    async def test_oauth_required_pauses(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _make_schedule()

        async def fake_run(**kwargs):
            return _result(
                "oauth_required",
                oauth_required=[
                    OAuthConsentRequired(provider_id="google-drive", authorization_url="https://example.com/consent")
                ],
            )

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)

        result = await worker.run_schedule(_payload(schedule))

        assert result["result"] == "paused_error"
        assert result["reason"] == "oauth_required"
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.state == "paused_error"
        assert updated.state_reason == "oauth_required"
        assert "google-drive" in (updated.last_error or "")
        assert "https://example.com/consent" in (updated.last_error or "")


class TestGenericFailure:
    async def test_single_error_records_without_pausing(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _make_schedule()

        async def fake_run(**kwargs):
            return _result("error", error="transport: boom")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)

        result = await worker.run_schedule(_payload(schedule))

        assert result["result"] == "error"
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.state == "active"  # not paused on an isolated failure
        assert updated.last_run_status == "error"

    async def test_repeated_failures_pause(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _make_schedule()

        async def fake_run(**kwargs):
            return _result("error", error="transport: boom")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)
        monkeypatch.setenv("SCHEDULED_RUNS_MAX_FAILURES", "2")

        first = await worker.run_schedule(_payload(schedule))
        assert first["result"] == "error"

        second = await worker.run_schedule(_payload(schedule))

        assert second["result"] == "paused_error"
        assert second["reason"] == "repeated_failures"
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.state == "paused_error"
        assert updated.state_reason == "repeated_failures"

    async def test_repeated_failures_pause_at_default_threshold(
        self, sessions_metadata_table, bff_sessions_table, monkeypatch
    ):
        """The breaker must trip at the PRODUCTION default (3), not only at 2.

        Regression guard: the original last-status proxy capped the streak at
        2, so with the default SCHEDULED_RUNS_MAX_FAILURES=3 it could never
        pause. The persistent counter must reach 3.
        """
        schedule = await _make_schedule()

        async def fake_run(**kwargs):
            return _result("error", error="transport: boom")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)
        # No SCHEDULED_RUNS_MAX_FAILURES override — exercise the default of 3.

        assert (await worker.run_schedule(_payload(schedule)))["result"] == "error"
        assert (await worker.run_schedule(_payload(schedule)))["result"] == "error"
        third = await worker.run_schedule(_payload(schedule))

        assert third["result"] == "paused_error"
        assert third["reason"] == "repeated_failures"
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.state == "paused_error"
        assert updated.consecutive_failures == 3

    async def test_completed_run_resets_failure_streak(
        self, sessions_metadata_table, bff_sessions_table, monkeypatch
    ):
        """A successful run clears the streak so old failures don't accumulate."""
        schedule = await _make_schedule()
        statuses = iter(["error", "completed"])

        async def fake_run(**kwargs):
            return _result(next(statuses), session_id="sess-x")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)

        await worker.run_schedule(_payload(schedule))  # error -> streak 1
        await worker.run_schedule(_payload(schedule))  # completed -> streak 0
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.consecutive_failures == 0
        assert updated.state == "active"

    async def test_timeout_status_treated_as_failure(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _make_schedule()

        async def fake_run(**kwargs):
            return _result("timeout", error="stream exceeded 300s budget")

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)

        result = await worker.run_schedule(_payload(schedule))

        assert result["result"] == "timeout"
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.last_run_status == "error"

    async def test_unexpected_exception_still_records_run(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _make_schedule()

        async def exploding_run(**kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(worker, "run_agent_headless", exploding_run)

        result = await worker.run_schedule(_payload(schedule))

        assert result["result"] == "error"
        updated = await get_scheduled_prompt(USER_ID, schedule.schedule_id)
        assert updated.last_run_status == "error"
        assert "kaboom" in (updated.last_error or "")


class TestMissingSchedule:
    async def test_missing_schedule_drops_run(self, sessions_metadata_table, bff_sessions_table):
        result = await worker.run_schedule({"scheduleId": "sched-missing", "userId": USER_ID})
        assert result["result"] == "dropped"
