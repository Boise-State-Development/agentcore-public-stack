"""Connection health (docs/specs/proactive-trust.md §5.1, §5.3, PR 4.3).

* The grant-expiry warning: the rule, one warning per grant epoch, and the
  worker raising it after a run only while ``PROACTIVE_RUNS_ENABLED`` is on.
* Reconnect-resumes: reconnecting a provider resumes the personal schedules a
  run paused on that exact provider, as the Resume button does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from apis.shared.harness.models import RunResult
from apis.shared.scheduled_prompts import grant_warning, reconnect
from apis.shared.scheduled_prompts.models import ScheduledPrompt
from apis.shared.scheduled_prompts.service import (
    create_scheduled_prompt,
    get_scheduled_prompt,
    list_scheduled_prompts,
    set_schedule_state,
)
from lambdas.scheduled_runs_worker import worker

pytestmark = pytest.mark.asyncio

USER_ID = "user-1"
NOW = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _schedule(*, next_run_at: Optional[datetime] = None, owner_email: Optional[str] = "owner@example.edu") -> ScheduledPrompt:
    return ScheduledPrompt.model_validate(
        {
            "scheduleId": "sched-1",
            "userId": USER_ID,
            "label": "Digest",
            "promptText": "x",
            "cadence": "daily",
            "hourLocal": 7,
            "timezone": "America/Boise",
            "nextRunAt": _iso(next_run_at) if next_run_at else None,
            "ownerEmail": owner_email,
            "createdAt": "2026-11-01T00:00:00Z",
            "updatedAt": "2026-11-01T00:00:00Z",
        }
    )


def _result(*, expires: Optional[datetime], issued: int = 1_000, grant_id: Optional[str] = "hlg-1") -> RunResult:
    return RunResult(
        run_id="run-1",
        session_id="s-1",
        user_id=USER_ID,
        status="completed",
        grant_id=grant_id,
        grant_expires_at=_epoch(expires) if expires else None,
        grant_issued_at=issued,
    )


# =====================================================================
# The rule
# =====================================================================


class TestNeedsExpiryWarning:
    def test_a_frequent_schedule_warns_three_days_ahead(self):
        soon = NOW + timedelta(minutes=15)
        assert grant_warning.needs_expiry_warning(_epoch(NOW + timedelta(days=2, hours=23)), _iso(soon), NOW)
        assert not grant_warning.needs_expiry_warning(_epoch(NOW + timedelta(days=3, hours=1)), _iso(soon), NOW)

    def test_a_weekly_schedule_warns_at_its_last_run_before_the_lapse(self):
        next_week = NOW + timedelta(days=7)
        # Lapses before the next run: this is the last run that can warn.
        assert grant_warning.needs_expiry_warning(_epoch(NOW + timedelta(days=6)), _iso(next_week), NOW)
        # Lapses after the next run: the next run warns instead.
        assert not grant_warning.needs_expiry_warning(_epoch(NOW + timedelta(days=8)), _iso(next_week), NOW)

    def test_an_already_lapsed_grant_is_left_to_the_reauth_pause(self):
        assert not grant_warning.needs_expiry_warning(_epoch(NOW - timedelta(hours=1)), None, NOW)

    def test_no_expiry_means_no_warning(self):
        assert not grant_warning.needs_expiry_warning(None, None, NOW)

    def test_a_re_pin_past_the_window_clears_it(self):
        assert grant_warning.expiry_clears_window(_epoch(NOW + timedelta(days=20)), NOW)
        assert not grant_warning.expiry_clears_window(_epoch(NOW + timedelta(days=2)), NOW)


# =====================================================================
# Raising the warning
# =====================================================================


class FakeGrants:
    def __init__(self, win: bool = True):
        self.win = win
        self.calls: list[tuple[str, int]] = []

    async def mark_expiry_warned(self, grant_id: str, token_issued_at: int) -> bool:
        self.calls.append((grant_id, token_issued_at))
        return self.win


class FakeInbox:
    def __init__(self):
        self.rows: list[dict] = []
        self.marked: list[tuple] = []

    def notify_own(self, *, recipient_email, kind, payload):
        self.rows.append({"to": recipient_email, "kind": kind, "payload": payload})
        return object()

    def mark_read_matching(self, email, *, kinds, payload):
        self.marked.append((email, tuple(kinds), payload))
        return 1


class TestMaybeWarnGrantExpiry:
    async def test_warns_once_with_the_lapse_and_the_active_count(self, sessions_metadata_table):
        await create_scheduled_prompt(
            user_id=USER_ID, label="A", prompt_text="x", cadence="daily", hour_local=7, timezone_name="America/Boise"
        )
        grants, inbox = FakeGrants(), FakeInbox()
        expires = NOW + timedelta(days=2)

        wrote = await grant_warning.maybe_warn_grant_expiry(
            _schedule(next_run_at=NOW + timedelta(hours=1)), _result(expires=expires), grants=grants, notifications=inbox, now=NOW
        )

        assert wrote
        assert grants.calls == [("hlg-1", 1_000)]
        (row,) = inbox.rows
        assert row["kind"] == "connection_expiring"
        assert row["to"] == "owner@example.edu"
        assert row["payload"] == {"scope": "grant", "expiresAt": _iso(expires), "scheduleCount": 1}

    async def test_a_second_run_in_the_same_epoch_writes_nothing(self):
        inbox = FakeInbox()
        wrote = await grant_warning.maybe_warn_grant_expiry(
            _schedule(), _result(expires=NOW + timedelta(days=1)), grants=FakeGrants(win=False), notifications=inbox, now=NOW
        )
        assert not wrote and inbox.rows == []

    async def test_a_grant_far_from_lapsing_claims_nothing(self):
        grants, inbox = FakeGrants(), FakeInbox()
        await grant_warning.maybe_warn_grant_expiry(
            _schedule(), _result(expires=NOW + timedelta(days=20)), grants=grants, notifications=inbox, now=NOW
        )
        assert grants.calls == [] and inbox.rows == []

    async def test_no_owner_email_claims_nothing(self):
        grants = FakeGrants()
        await grant_warning.maybe_warn_grant_expiry(
            _schedule(owner_email=None), _result(expires=NOW + timedelta(days=1)), grants=grants, notifications=FakeInbox(), now=NOW
        )
        assert grants.calls == []

    async def test_a_run_that_minted_another_way_claims_nothing(self):
        grants = FakeGrants()
        await grant_warning.maybe_warn_grant_expiry(
            _schedule(), _result(expires=NOW + timedelta(days=1), grant_id=None), grants=grants, notifications=FakeInbox(), now=NOW
        )
        assert grants.calls == []

    async def test_never_raises(self):
        class Broken(FakeGrants):
            async def mark_expiry_warned(self, grant_id, token_issued_at):
                raise RuntimeError("ddb down")

        assert not await grant_warning.maybe_warn_grant_expiry(
            _schedule(), _result(expires=NOW + timedelta(days=1)), grants=Broken(), notifications=FakeInbox(), now=NOW
        )


class TestGrantExpirySettled:
    def test_a_re_pin_past_the_window_marks_the_warnings_read(self):
        inbox = FakeInbox()
        far = _epoch(datetime.now(timezone.utc) + timedelta(days=29))
        assert grant_warning.grant_expiry_settled("owner@example.edu", far, notifications=inbox) == 1
        assert inbox.marked == [("owner@example.edu", ("connection_expiring",), {"scope": "grant"})]

    def test_a_re_pin_still_inside_the_window_settles_nothing(self):
        inbox = FakeInbox()
        near = _epoch(datetime.now(timezone.utc) + timedelta(days=2))
        assert grant_warning.grant_expiry_settled("owner@example.edu", near, notifications=inbox) == 0
        assert inbox.marked == []


# =====================================================================
# The worker raises it after a run
# =====================================================================


class TestWorkerWarns:
    async def _run(self, monkeypatch, *, flag: str):
        monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", flag)
        schedule = await create_scheduled_prompt(
            user_id=USER_ID, label="A", prompt_text="x", cadence="daily", hour_local=7, timezone_name="America/Boise"
        )
        calls = []

        async def spy(schedule_arg, result_arg):
            calls.append((schedule_arg.schedule_id, result_arg.grant_expires_at))
            return False

        async def fake_run(**kwargs):
            return _result(expires=NOW + timedelta(days=1))

        monkeypatch.setattr(worker, "maybe_warn_grant_expiry", spy)
        monkeypatch.setattr(worker, "run_agent_headless", fake_run)
        await worker.run_schedule({"scheduleId": schedule.schedule_id, "userId": USER_ID})
        return schedule, calls

    async def test_a_run_checks_the_grant_while_the_flag_is_on(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        schedule, calls = await self._run(monkeypatch, flag="true")
        assert calls == [(schedule.schedule_id, _epoch(NOW + timedelta(days=1)))]

    async def test_nothing_is_checked_while_the_flag_is_off(self, sessions_metadata_table, bff_sessions_table, monkeypatch):
        _, calls = await self._run(monkeypatch, flag="false")
        assert calls == []


# =====================================================================
# Reconnect-resumes
# =====================================================================


async def _paused(provider: str, **overrides) -> ScheduledPrompt:
    kwargs = dict(user_id=USER_ID, label=f"Paused {provider}", prompt_text="x", cadence="daily", hour_local=7, timezone_name="America/Boise")
    kwargs.update(overrides)
    schedule = await create_scheduled_prompt(**kwargs)
    await set_schedule_state(USER_ID, schedule.schedule_id, "paused_error", state_reason="oauth_required")
    return schedule


def _set_last_error(table, schedule_id: str, error: str) -> None:
    table.update_item(
        Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{schedule_id}"},
        UpdateExpression="SET lastError = :e",
        ExpressionAttributeValues={":e": error},
    )


class TestResumeAfterReconnect:
    async def test_resumes_the_schedules_paused_on_that_provider(self, sessions_metadata_table):
        github = await _paused("github")
        _set_last_error(sessions_metadata_table, github.schedule_id, "provider=github authorization_url=https://consent.example/x")
        inbox = FakeInbox()

        resumed = await reconnect.resume_after_reconnect(USER_ID, "github", email="owner@example.edu", notifications=inbox)

        assert resumed == [github.schedule_id]
        after = await get_scheduled_prompt(USER_ID, github.schedule_id)
        assert after.state == "active" and after.next_run_at
        assert inbox.marked == [("owner@example.edu", ("schedule_paused",), {"scheduleId": github.schedule_id})]

    async def test_a_provider_whose_name_contains_anothers_is_not_matched(self, sessions_metadata_table):
        enterprise = await _paused("github-enterprise")
        _set_last_error(sessions_metadata_table, enterprise.schedule_id, "provider=github-enterprise authorization_url=https://x")

        assert await reconnect.resume_after_reconnect(USER_ID, "github") == []
        assert (await get_scheduled_prompt(USER_ID, enterprise.schedule_id)).state == "paused_error"

    async def test_other_pauses_are_left_alone(self, sessions_metadata_table):
        schedule = await create_scheduled_prompt(
            user_id=USER_ID, label="Reauth", prompt_text="x", cadence="daily", hour_local=7, timezone_name="America/Boise"
        )
        await set_schedule_state(USER_ID, schedule.schedule_id, "paused_error", state_reason="reauth_required")
        _set_last_error(sessions_metadata_table, schedule.schedule_id, "provider=github authorization_url=https://x")

        assert await reconnect.resume_after_reconnect(USER_ID, "github") == []

    async def test_a_project_schedule_is_left_to_its_project(self, sessions_metadata_table):
        project = await _paused("github", project_id="proj-1", owner_email="owner@example.edu")
        _set_last_error(sessions_metadata_table, project.schedule_id, "provider=github authorization_url=https://x")

        assert await reconnect.resume_after_reconnect(USER_ID, "github") == []
        assert (await get_scheduled_prompt(USER_ID, project.schedule_id)).state == "paused_error"

    async def test_a_budget_hold_is_respected(self, sessions_metadata_table):
        github = await _paused("github")
        _set_last_error(sessions_metadata_table, github.schedule_id, "provider=github authorization_url=https://x")
        hold = _iso(datetime.now(timezone.utc) + timedelta(days=1, hours=2))
        sessions_metadata_table.update_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDPROMPT#{github.schedule_id}"},
            UpdateExpression="SET budgetHoldUntil = :h",
            ExpressionAttributeValues={":h": hold},
        )

        await reconnect.resume_after_reconnect(USER_ID, "github")

        after = await get_scheduled_prompt(USER_ID, github.schedule_id)
        assert after.state == "active"
        assert after.next_run_at >= hold

    async def test_the_resume_button_uses_the_same_time(self, sessions_metadata_table):
        schedules = await list_scheduled_prompts(USER_ID)
        assert schedules == []  # a clean table: the helper below is the shared one
        from apis.app_api.schedules import routes as schedules_routes

        schedule = await _paused("github")
        assert schedules_routes._resume_time(schedule)[:13] == reconnect.resume_next_run_at(schedule)[:13]
