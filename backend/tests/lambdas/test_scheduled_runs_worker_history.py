"""Scheduled-runs worker, quiet watcher PR 1.1 (docs/specs/quiet-watcher.md §3.1, §3.4).

- a schedule's ``modelId`` reaches ``run_agent_headless``, and an unset one adds nothing
- while ``PROACTIVE_RUNS_ENABLED`` is on, every personal run leaves one
  ``SCHEDRUN#`` row, whatever its outcome; a project schedule leaves none
- while it is off, the worker writes no row at all

moto DynamoDB with the real scheduled_prompts service; only ``run_agent_headless`` is stubbed.
"""

from datetime import datetime, timezone

import pytest
from boto3.dynamodb.conditions import Key

from apis.shared.harness.auth import HeadlessAuthError
from apis.shared.harness.models import OAuthConsentRequired, RunResult
from apis.shared.scheduled_prompts.runs import list_schedule_runs
from apis.shared.scheduled_prompts.service import create_scheduled_prompt
from lambdas.scheduled_runs_worker import worker

pytestmark = pytest.mark.asyncio

USER_ID = "user-1"
FINISHED = "2026-11-25T01:00:00Z"
USAGE = {"usage": {"inputTokens": 2000, "outputTokens": 40, "cacheReadInputTokens": 1200, "cacheWriteInputTokens": 4300}}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _result(status: str, **overrides) -> RunResult:
    base = dict(
        run_id="run-abc",
        session_id="headless-abc",
        user_id=USER_ID,
        status=status,
        started_at=_now_iso(),
        finished_at=FINISHED,
        usage=USAGE,
    )
    base.update(overrides)
    return RunResult(**base)


async def _make_schedule(**overrides):
    kwargs = dict(
        user_id=USER_ID,
        label="Inbox watcher",
        prompt_text="Tell me what needs me",
        cadence="daily",
        hour_local=9,
        timezone_name="America/Boise",
    )
    kwargs.update(overrides)
    return await create_scheduled_prompt(**kwargs)


def _payload(schedule):
    return {"scheduleId": schedule.schedule_id, "userId": USER_ID}


def _run_rows(table) -> list:
    return table.query(
        KeyConditionExpression=Key("PK").eq(f"USER#{USER_ID}") & Key("SK").begins_with("SCHEDRUN#")
    )["Items"]


def _stub(monkeypatch, outcome):
    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(worker, "run_agent_headless", fake_run)
    return calls


@pytest.fixture
def proactive_on(monkeypatch):
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")


class TestModelId:
    async def test_a_schedule_model_is_passed_to_the_run(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _make_schedule(model_id="us.anthropic.claude-haiku-4-5")
        calls = _stub(monkeypatch, _result("completed"))
        await worker.run_schedule(_payload(schedule))
        assert calls[0]["model_id"] == "us.anthropic.claude-haiku-4-5"

    async def test_no_model_adds_no_argument(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule = await _make_schedule()
        calls = _stub(monkeypatch, _result("completed"))
        await worker.run_schedule(_payload(schedule))
        assert "model_id" not in calls[0]


class TestHistoryRows:
    async def test_completed(self, sessions_metadata_table, bff_sessions_table, monkeypatch, proactive_on):
        schedule = await _make_schedule()
        _stub(monkeypatch, _result("completed"))
        await worker.run_schedule(_payload(schedule))

        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert run.status == "completed"
        assert (run.run_id, run.session_id, run.finished_at) == ("run-abc", "headless-abc", FINISHED)
        assert (run.input_tokens, run.output_tokens, run.cache_read_tokens, run.cache_write_tokens) == (2000, 40, 1200, 4300)
        assert run.item_count == 0

    @pytest.mark.parametrize("status", ["error", "timeout"])
    async def test_error_and_timeout(self, sessions_metadata_table, bff_sessions_table, monkeypatch, proactive_on, status):
        schedule = await _make_schedule()
        _stub(monkeypatch, _result(status, error="boom"))
        await worker.run_schedule(_payload(schedule))

        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert run.status == status
        assert run.session_id == "headless-abc"

    async def test_a_breaker_pause_still_records_the_failed_run(
        self, sessions_metadata_table, bff_sessions_table, monkeypatch, proactive_on
    ):
        monkeypatch.setenv("SCHEDULED_RUNS_MAX_FAILURES", "1")
        schedule = await _make_schedule()
        _stub(monkeypatch, _result("error", error="boom"))
        result = await worker.run_schedule(_payload(schedule))

        assert result["reason"] == "repeated_failures"
        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert run.status == "error"

    async def test_oauth_required_is_paused(self, sessions_metadata_table, bff_sessions_table, monkeypatch, proactive_on):
        schedule = await _make_schedule()
        _stub(
            monkeypatch,
            _result(
                "oauth_required",
                oauth_required=[OAuthConsentRequired(provider_id="github", authorization_url="https://example.com/c")],
            ),
        )
        await worker.run_schedule(_payload(schedule))

        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert (run.status, run.run_id, run.session_id) == ("paused", "run-abc", "headless-abc")

    async def test_a_dead_grant_is_paused_with_a_fresh_run_id(
        self, sessions_metadata_table, bff_sessions_table, monkeypatch, proactive_on
    ):
        schedule = await _make_schedule()
        _stub(monkeypatch, HeadlessAuthError("no active grant"))
        await worker.run_schedule(_payload(schedule))

        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert run.status == "paused"
        assert run.run_id.startswith("run-")
        assert run.session_id is None
        assert run.finished_at

    async def test_an_unexpected_exception_is_an_error(
        self, sessions_metadata_table, bff_sessions_table, monkeypatch, proactive_on
    ):
        schedule = await _make_schedule()
        _stub(monkeypatch, RuntimeError("kaboom"))
        await worker.run_schedule(_payload(schedule))

        (run,) = list_schedule_runs(USER_ID, schedule.schedule_id)
        assert run.status == "error"
        assert run.session_id is None
        assert (run.input_tokens, run.output_tokens) == (0, 0)

    async def test_one_row_per_run(self, sessions_metadata_table, bff_sessions_table, monkeypatch, proactive_on):
        schedule = await _make_schedule()
        results = iter([_result("completed", run_id="run-1"), _result("completed", run_id="run-2")])

        async def fake_run(**kwargs):
            return next(results)

        monkeypatch.setattr(worker, "run_agent_headless", fake_run)
        await worker.run_schedule(_payload(schedule))
        await worker.run_schedule(_payload(schedule))
        assert sorted(r.run_id for r in list_schedule_runs(USER_ID, schedule.schedule_id)) == ["run-1", "run-2"]

    async def test_a_project_schedule_gets_no_row(
        self, sessions_metadata_table, bff_sessions_table, monkeypatch, proactive_on
    ):
        schedule = await _make_schedule(project_id="proj-1", owner_email="a@example.com")

        async def no_project_row(*args, **kwargs):
            return None

        monkeypatch.setattr(worker, "_record_project_run", no_project_row)
        _stub(monkeypatch, _result("completed"))
        await worker.run_schedule(_payload(schedule))
        assert _run_rows(sessions_metadata_table) == []


class TestFlagOff:
    @pytest.mark.parametrize("value", [None, "", "false"])
    async def test_no_row_for_any_outcome(self, sessions_metadata_table, bff_sessions_table, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("PROACTIVE_RUNS_ENABLED", raising=False)
        else:
            monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", value)
        schedule = await _make_schedule()
        for outcome in (
            _result("completed"),
            _result("error", error="boom"),
            _result("timeout"),
            RuntimeError("kaboom"),
        ):
            _stub(monkeypatch, outcome)
            await worker.run_schedule(_payload(schedule))
        _stub(monkeypatch, HeadlessAuthError("no active grant"))
        await worker.run_schedule(_payload(schedule))
        assert _run_rows(sessions_metadata_table) == []
