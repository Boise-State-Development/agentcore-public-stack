"""Which requests are unattended runs, for the action audit (docs/specs/proactive-trust.md §7.2, PR 4.2).

- an interactive turn (the SPA, the TUI, a user's follow-up in a run's conversation)
  carries no run and is never audited;
- a scheduled run, a Run now and an inbox resume are, on a headless session only;
- a resume takes the run from the paused run's snapshot whoever answers it:
  ``approvedVia: inbox`` from the worker, ``conversation`` from the conversation;
- ``HEADLESS_ACTION_AUDIT_ENABLED=false`` audits nothing;
- the watch-run write gate attaches only to a watch run's agent, while
  ``PROACTIVE_RUNS_ENABLED`` is on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import apis.shared.caching  # noqa: F401  (binds `apis` to src before `tests.apis`)
from apis.inference_api.chat import routes
from apis.inference_api.chat.models import InvocationRequest
from apis.shared.sessions.models import PausedTurnSnapshot

HEADLESS = "headless-abc123"


def _request(**fields) -> InvocationRequest:
    return InvocationRequest(session_id=fields.pop("session_id", HEADLESS), message="go", **fields)


def _snapshot(**fields) -> PausedTurnSnapshot:
    return PausedTurnSnapshot(capturedAt="2026-11-27T00:00:00Z", expiresAt="2026-12-04T00:00:00Z", **fields)


@pytest.fixture(autouse=True)
def audit_on(monkeypatch):
    monkeypatch.delenv("HEADLESS_ACTION_AUDIT_ENABLED", raising=False)


def test_an_interactive_turn_is_not_a_run():
    assert routes._unattended_run(_request(session_id="sess-web-1"), None, is_resume=False, user_id="u") is None
    # A user's own follow-up in a run's conversation carries no run either.
    assert routes._unattended_run(_request(), None, is_resume=False, user_id="u") is None


def test_run_fields_off_a_headless_session_are_ignored():
    request = _request(session_id="sess-web-1", run_id="run-1", run_trigger="schedule")
    assert routes._unattended_run(request, None, is_resume=False, user_id="u") is None


def test_a_scheduled_run_is_one():
    request = _request(run_id="run-1", run_trigger="schedule", run_schedule_id="sched-1", act_without_asking=["m/x"])
    run = routes._unattended_run(request, None, is_resume=False, user_id="u")
    assert (run.run_id, run.trigger, run.schedule_id, run.user_id, run.session_id) == ("run-1", "schedule", "sched-1", "u", HEADLESS)
    assert run.approved == frozenset() and run.approved_via is None
    assert run.act_without_asking == ("m/x",)


def test_the_kill_switch_audits_nothing(monkeypatch):
    monkeypatch.setenv("HEADLESS_ACTION_AUDIT_ENABLED", "false")
    request = _request(run_id="run-1", run_trigger="schedule")
    assert routes._unattended_run(request, None, is_resume=False, user_id="u") is None


def test_an_inbox_resume_takes_the_paused_run_and_says_inbox():
    request = _request(
        run_id="resume-9",
        run_trigger="inbox_respond",
        interrupt_responses=[{"interruptId": "v1:before_tool_call:tu-1:aa", "response": "approved"}],
    )
    snapshot = _snapshot(runId="run-1", runTrigger="schedule", runScheduleId="sched-1", actWithoutAsking=["m/x"])
    run = routes._unattended_run(request, snapshot, is_resume=True, user_id="u")
    assert (run.run_id, run.trigger, run.schedule_id) == ("run-1", "schedule", "sched-1")
    assert (run.approved_via, run.approving_run_id) == ("inbox", "resume-9")
    assert run.approved == frozenset({"tu-1"})
    assert run.act_without_asking == ("m/x",)


def test_an_answer_in_the_conversation_still_lands_on_the_runs_rows():
    request = _request(interrupt_responses=[{"interruptId": "v1:before_tool_call:tu-1:aa", "response": "approved"}])
    snapshot = _snapshot(runId="run-1", runTrigger="schedule", runScheduleId="sched-1")
    run = routes._unattended_run(request, snapshot, is_resume=True, user_id="u")
    assert (run.run_id, run.approved_via, run.approving_run_id) == ("run-1", "conversation", None)


def test_a_resume_of_an_interactive_pause_is_not_a_run():
    request = _request(interrupt_responses=[{"interruptId": "v1:before_tool_call:tu-1:aa", "response": "approved"}])
    assert routes._unattended_run(request, _snapshot(), is_resume=True, user_id="u") is None


def test_the_gate_attaches_only_to_a_watch_runs_agent(monkeypatch):
    added = []
    agent = SimpleNamespace(
        agent=SimpleNamespace(add_hook=lambda callback, event_type: added.append(event_type.__name__)),
        tool_facts=lambda: None,
    )
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    assert routes._attach_watch_write_gate(agent, _request(run_mode="watch"), ()) is True
    assert routes._attach_watch_write_gate(agent, _request(), ()) is False
    assert routes._attach_watch_write_gate(agent, _request(run_mode="watch", session_id="sess-web"), ()) is False
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "false")
    assert routes._attach_watch_write_gate(agent, _request(run_mode="watch"), ()) is False
    assert added == ["BeforeToolCallEvent"]


def test_the_request_fields_are_optional_and_bounded():
    assert _request().run_id is None and _request().act_without_asking is None
    with pytest.raises(Exception):
        _request(run_trigger="manual")
    with pytest.raises(Exception):
        _request(act_without_asking=[f"m/t{i}" for i in range(21)])
