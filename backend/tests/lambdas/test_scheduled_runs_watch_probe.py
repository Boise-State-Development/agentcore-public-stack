"""Scheduled-runs worker, quiet watcher triage PR T.1 (docs/specs/quiet-watcher-triage.md §3.4).

While ``WATCH_TRIAGE_ENABLED`` is on, a completed full watch run stores its probe (from the
``watch_probe`` frame and the tool trace), ``fingerprintVersion``, ``probeRunId``,
``lastFullRunAt`` and ``wakeAt`` on ``SCHEDSTATE#``; a run with no probe removes the old one;
a failed or over-quota run writes nothing new. Outcomes are unchanged either way, and with the
flag off nothing of it is written.

moto DynamoDB with the real worker and state code; only ``run_agent_headless`` is stubbed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apis.shared.harness.models import ToolTraceEntry
from apis.shared.scheduled_prompts.watch_probe import FINGERPRINT_VERSION
from apis.shared.scheduled_prompts.watch_state import WatchState, save_watch_state
from tests.lambdas.test_scheduled_runs_watch import USER_ID, _report, _result, _run, _schedule, _stub
from tests.shared.test_project_schedules import TABLE, _make_projects_table

pytestmark = pytest.mark.asyncio

FP = "cd" * 16
LIST = ToolTraceEntry(tool_use_id="t1", name="github___list_notifications", input={"since": "2026-11-24T00:00:00Z"})
FRAME = {
    "fingerprintVersion": FINGERPRINT_VERSION,
    "probeable": True,
    "calls": [{"toolUseId": "t1", "name": "github___list_notifications", "fingerprint": FP}],
}
WAKE = "2099-01-01T00:00:00Z"


@pytest.fixture()
def inbox(sessions_metadata_table, monkeypatch):
    """As in ``test_scheduled_runs_watch``: the projects table the inbox lives in, the watcher on."""
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("PROACTIVE_RUNS_ENABLED", "true")
    _make_projects_table()


@pytest.fixture()
def triage_on(monkeypatch):
    monkeypatch.setenv("WATCH_TRIAGE_ENABLED", "true")


def _state_item(table, schedule) -> dict:
    return table.get_item(Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDSTATE#{schedule.schedule_id}"}).get("Item") or {}


TRIAGE_FIELDS = {"probe", "fingerprintVersion", "probeRunId", "lastFullRunAt", "wakeAt"}


async def test_a_probeable_quiet_run_stores_its_probe(inbox, sessions_metadata_table, monkeypatch, triage_on):
    schedule = await _schedule()
    _stub(monkeypatch, _result(trace=[LIST, _report(outcome="nothing_new")], watch_probe=FRAME))
    assert (await _run(schedule))["result"] == "quiet"

    item = _state_item(sessions_metadata_table, schedule)
    assert item["probe"] == [
        {"name": "github___list_notifications", "input": '{"since":"2026-11-24T00:00:00Z"}', "fingerprint": FP}
    ]
    assert (item["fingerprintVersion"], item["probeRunId"], item["lastFullRunAt"]) == (
        FINGERPRINT_VERSION,
        "run-abc",
        "2026-11-25T01:00:00Z",
    )
    assert "wakeAt" not in item


async def test_a_notifying_run_stores_its_probe_and_wake_time(inbox, sessions_metadata_table, monkeypatch, triage_on):
    schedule = await _schedule()
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    report = _report(
        outcome="notify", items=[{"key": "k1", "title": "New"}], state={"wakeAt": soon.isoformat()}
    )
    _stub(monkeypatch, _result(trace=[LIST, report], watch_probe=FRAME))
    assert (await _run(schedule))["result"] == "notify"

    item = _state_item(sessions_metadata_table, schedule)
    assert item["probeRunId"] == "run-abc"
    assert item["wakeAt"] == soon.isoformat().replace("+00:00", "Z")
    assert item["seen"] == ["k1"]


async def test_a_wake_time_past_thirty_days_is_clamped(inbox, sessions_metadata_table, monkeypatch, triage_on):
    schedule = await _schedule()
    _stub(monkeypatch, _result(trace=[LIST, _report(outcome="nothing_new", state={"wakeAt": WAKE})], watch_probe=FRAME))
    await _run(schedule)

    wake = datetime.fromisoformat(_state_item(sessions_metadata_table, schedule)["wakeAt"].replace("Z", "+00:00"))
    assert timedelta(days=29) < wake - datetime.now(timezone.utc) <= timedelta(days=30)


async def test_a_run_with_no_probe_removes_the_old_one(inbox, sessions_metadata_table, monkeypatch, triage_on):
    schedule = await _schedule()
    _stub(monkeypatch, _result(trace=[LIST, _report(outcome="nothing_new")], watch_probe=FRAME))
    await _run(schedule)
    unprobeable = {"fingerprintVersion": FINGERPRINT_VERSION, "probeable": False, "reason": "write_tool", "calls": []}
    _stub(monkeypatch, _result(run_id="run-def", trace=[LIST, _report(outcome="nothing_new")], watch_probe=unprobeable))
    await _run(schedule)

    item = _state_item(sessions_metadata_table, schedule)
    assert not {"probe", "fingerprintVersion", "probeRunId"} & set(item)
    assert item["lastFullRunAt"] == "2026-11-25T01:00:00Z"


async def test_a_run_without_the_frame_leaves_no_probe(inbox, sessions_metadata_table, monkeypatch, triage_on):
    """A Runtime with its copy of the flag off sends no frame: the worker fails closed."""
    schedule = await _schedule()
    _stub(monkeypatch, _result(trace=[LIST, _report(outcome="nothing_new")]))
    await _run(schedule)
    item = _state_item(sessions_metadata_table, schedule)
    assert "probe" not in item and item["lastFullRunAt"] == "2026-11-25T01:00:00Z"


async def test_an_over_quota_run_writes_nothing_new(inbox, sessions_metadata_table, monkeypatch, triage_on):
    schedule = await _schedule()
    save_watch_state(USER_ID, schedule.schedule_id, WatchState(seen=["a"]), expected_version=None)
    _stub(monkeypatch, _result(trace=[LIST], watch_probe=FRAME, quota_exceeded=True, final_message="over"))
    assert (await _run(schedule))["result"] == "error"
    assert not TRIAGE_FIELDS & set(_state_item(sessions_metadata_table, schedule))


async def test_a_failed_run_writes_nothing_new(inbox, sessions_metadata_table, monkeypatch, triage_on):
    schedule = await _schedule()
    _stub(monkeypatch, _result("error", trace=[LIST], watch_probe=FRAME, error="boom"))
    await _run(schedule)
    assert _state_item(sessions_metadata_table, schedule) == {}


@pytest.mark.parametrize("value", [None, "", "false"])
async def test_with_the_flag_off_nothing_of_it_is_written(inbox, sessions_metadata_table, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("WATCH_TRIAGE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("WATCH_TRIAGE_ENABLED", value)
    schedule = await _schedule()
    report = _report(outcome="nothing_new", state={"wakeAt": WAKE})
    _stub(monkeypatch, _result(trace=[LIST, report], watch_probe=FRAME))
    assert (await _run(schedule))["result"] == "quiet"
    item = _state_item(sessions_metadata_table, schedule)
    assert item["version"] == 1 and not TRIAGE_FIELDS & set(item)


async def test_the_outcome_is_the_same_either_way(inbox, sessions_metadata_table, monkeypatch):
    outcomes = []
    for value in ("false", "true"):
        monkeypatch.setenv("WATCH_TRIAGE_ENABLED", value)
        schedule = await _schedule()
        report = _report(outcome="notify", items=[{"key": f"k-{value}", "title": "New"}])
        calls = _stub(monkeypatch, _result(trace=[LIST, report], watch_probe=FRAME))
        result = await _run(schedule)
        outcomes.append((result["result"], result["itemCount"], sorted(calls[0])))
    assert outcomes[0] == outcomes[1]
