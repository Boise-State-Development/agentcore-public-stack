"""Scheduled-runs worker, quiet watcher triage PR T.2 in shadow (docs/specs/quiet-watcher-triage.md §6.5).

While ``WATCH_TRIAGE_ENABLED`` is on, each watch run asks the Runtime to triage first (unless
§2.4 says to skip), records the verdict on its ``SCHEDRUN#`` row as ``triage``, and then runs
the full run **regardless**, on the same session id. A triage that fails is an escalation and
never fails the run. With the flag off nothing is invoked and the row has no ``triage``.

moto DynamoDB with the real worker, state and history code; ``run_agent_headless`` and
``run_watch_triage`` are stubbed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from apis.shared.harness import HeadlessAuthError
from apis.shared.scheduled_prompts.runs import list_schedule_runs
from apis.shared.scheduled_prompts.watch_probe import FINGERPRINT_VERSION
from apis.shared.scheduled_prompts.watch_state import WatchState, save_watch_state
from apis.shared.scheduled_prompts.watch_triage import TriageVerdict, escalate
from lambdas.scheduled_runs_worker import worker
from tests.lambdas.test_scheduled_runs_watch import USER_ID, _report, _result, _run, _schedule, _stub
from tests.shared.test_project_schedules import TABLE, _make_projects_table

pytestmark = pytest.mark.asyncio

FP = "cd" * 16
PROBE = [{"name": "github___list_notifications", "input": '{"since":"2026-11-24T00:00:00Z"}', "fingerprint": FP}]


@pytest.fixture()
def inbox(sessions_metadata_table, monkeypatch):
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    _make_projects_table()


@pytest.fixture()
def triage_on(monkeypatch):
    monkeypatch.setenv("WATCH_TRIAGE_ENABLED", "true")


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_probe(schedule, **overrides) -> None:
    now = datetime.now(timezone.utc)
    fields = {
        "probe": PROBE,
        "fingerprint_version": FINGERPRINT_VERSION,
        "probe_run_id": "run-earlier",
        "last_full_run_at": _iso(now - timedelta(hours=1)),
        "wake_at": None,
    }
    fields.update(overrides)
    from apis.shared.scheduled_prompts.watch_probe import FullRunTriage

    triage = FullRunTriage(
        probe=fields["probe"],
        fingerprint_version=fields["fingerprint_version"],
        probe_run_id=fields["probe_run_id"],
        last_full_run_at=fields["last_full_run_at"],
        wake_at=fields["wake_at"],
    )
    assert save_watch_state(USER_ID, schedule.schedule_id, WatchState(), expected_version=None, triage=triage)


def _stub_triage(monkeypatch, outcome):
    calls = []

    async def fake_triage(**kwargs):
        calls.append(kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome, 1234

    monkeypatch.setattr(worker, "run_watch_triage", fake_triage)
    return calls


def _only_run(schedule):
    runs = list_schedule_runs(USER_ID, schedule.schedule_id)
    assert len(runs) == 1
    return runs[0]


QUIET = TriageVerdict(verdict="quiet", reason="unchanged", tier=1, fingerprints=[FP])


async def test_a_quiet_verdict_is_recorded_and_the_full_run_still_runs(inbox, monkeypatch, triage_on):
    schedule = await _schedule()
    _seed_probe(schedule)
    triaged = _stub_triage(monkeypatch, QUIET)
    runs = _stub(monkeypatch, _result(trace=[_report(outcome="nothing_new")]))

    assert (await _run(schedule))["result"] == "quiet"

    assert len(triaged) == 1 and len(runs) == 1
    # One session for both, so the full run lands on the triage's microVM (§4.1).
    assert triaged[0]["session_id"] == runs[0]["session_id"]
    assert triaged[0]["session_id"].startswith("headless-")
    request = triaged[0]["request"]
    assert [c.name for c in request.calls] == ["github___list_notifications"]
    assert request.baseline == [FP]
    assert triaged[0]["enabled_tools"] == schedule.enabled_tools
    assert _only_run(schedule).triage == {"verdict": "quiet", "reason": "unchanged", "tier": 1, "ms": 1234}


async def test_an_escalation_is_recorded_on_the_notifying_run(inbox, monkeypatch, triage_on):
    schedule = await _schedule()
    _seed_probe(schedule)
    _stub_triage(monkeypatch, escalate("changed_no_decider", [None], tier=1))
    _stub(monkeypatch, _result(trace=[_report(outcome="notify", items=[{"key": "k1", "title": "New"}])]))

    assert (await _run(schedule))["result"] == "notify"
    assert _only_run(schedule).triage == {"verdict": "escalate", "reason": "changed_no_decider", "tier": 1, "ms": 1234}


async def test_a_quiet_triage_before_a_notify_is_logged_as_a_shadow_miss(inbox, monkeypatch, triage_on, caplog):
    schedule = await _schedule()
    _seed_probe(schedule)
    _stub_triage(monkeypatch, QUIET)
    _stub(monkeypatch, _result(trace=[_report(outcome="notify", items=[{"key": "k1", "title": "New"}])]))

    with caplog.at_level(logging.WARNING, logger=worker.logger.name):
        assert (await _run(schedule))["result"] == "notify"
    assert any("shadow miss" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"probe": None}, "no_probe"),
        ({"fingerprint_version": FINGERPRINT_VERSION + 1}, "version_mismatch"),
        ({"wake_at": "2020-01-01T00:00:00Z"}, "wake_due"),
        ({"last_full_run_at": "2020-01-01T00:00:00Z"}, "stale"),
    ],
)
async def test_a_skipped_run_sends_no_triage_and_records_why(inbox, monkeypatch, triage_on, overrides, reason):
    schedule = await _schedule()
    _seed_probe(schedule, **overrides)
    triaged = _stub_triage(monkeypatch, QUIET)
    runs = _stub(monkeypatch, _result(trace=[_report(outcome="nothing_new")]))

    await _run(schedule)
    assert triaged == [] and len(runs) == 1
    assert _only_run(schedule).triage == {"verdict": "skipped", "reason": reason}


async def test_a_watcher_with_no_state_skips_as_no_probe(inbox, monkeypatch, triage_on):
    schedule = await _schedule()
    triaged = _stub_triage(monkeypatch, QUIET)
    _stub(monkeypatch, _result(trace=[_report(outcome="nothing_new")]))

    await _run(schedule)
    assert triaged == []
    assert _only_run(schedule).triage == {"verdict": "skipped", "reason": "no_probe"}


async def test_a_triage_that_raises_escalates_and_the_run_completes(inbox, monkeypatch, triage_on):
    schedule = await _schedule()
    _seed_probe(schedule)
    _stub_triage(monkeypatch, RuntimeError("boom"))
    runs = _stub(monkeypatch, _result(trace=[_report(outcome="nothing_new")]))

    assert (await _run(schedule))["result"] == "quiet"
    assert len(runs) == 1
    assert _only_run(schedule).triage == {"verdict": "escalate", "reason": "triage_failed", "ms": 0}


async def test_a_triage_that_cannot_authenticate_pauses_like_the_full_run(inbox, monkeypatch, triage_on):
    schedule = await _schedule()
    _seed_probe(schedule)
    _stub_triage(monkeypatch, HeadlessAuthError("no grant"))
    runs = _stub(monkeypatch, _result(trace=[_report(outcome="nothing_new")]))

    out = await _run(schedule)
    assert runs == []
    assert out["result"] == "paused_error"  # reauth_required, the full run's own path


async def test_with_the_flag_off_nothing_is_triaged(inbox, monkeypatch):
    schedule = await _schedule()
    _seed_probe(schedule)
    triaged = _stub_triage(monkeypatch, QUIET)
    _stub(monkeypatch, _result(trace=[_report(outcome="nothing_new")]))

    await _run(schedule)
    assert triaged == []
    assert _only_run(schedule).triage is None


async def test_a_standard_schedule_is_never_triaged(inbox, monkeypatch, triage_on):
    schedule = await _schedule(mode="standard")
    triaged = _stub_triage(monkeypatch, QUIET)
    runs = _stub(monkeypatch, _result(final_message="Done"))

    await _run(schedule)
    assert triaged == []
    assert "session_id" not in runs[0]


async def test_one_runs_triage_never_reaches_the_next(inbox, monkeypatch, triage_on):
    first = await _schedule()
    _seed_probe(first)
    _stub_triage(monkeypatch, QUIET)
    _stub(monkeypatch, _result(trace=[_report(outcome="nothing_new")]))
    await _run(first)

    monkeypatch.setenv("WATCH_TRIAGE_ENABLED", "false")
    second = await _schedule()
    await _run(second)
    assert _only_run(second).triage is None
