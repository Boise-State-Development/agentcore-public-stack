"""Quiet watcher triage tier 1, the shared half (docs/specs/quiet-watcher-triage.md §2.4, §4.1, §6.4).

One test per §2.4 row that the worker decides (the flag rows are the worker's and the Runtime's,
tested there), the tier 1 compare, the request's bounds, and the worker's reading of a frame:
anything it can't trust escalates.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from apis.shared.scheduled_prompts.watch_probe import FINGERPRINT_VERSION, MAX_PROBE_CALLS
from apis.shared.scheduled_prompts.watch_state import WatchState
from apis.shared.scheduled_prompts.watch_triage import (
    TRIAGE_FAILED,
    WatchTriageRequest,
    compare_fingerprints,
    escalate,
    history_triage,
    max_staleness_hours,
    skip_reason,
    triage_request_from_state,
    verdict_from_frame,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
FP_A, FP_B = "a" * 32, "b" * 32
CALL = {"name": "github___list_notifications", "input": '{"since":"2026-11-24T00:00:00Z"}', "fingerprint": FP_A}


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _state(**fields) -> WatchState:
    base = {
        "probe": [CALL],
        "fingerprintVersion": FINGERPRINT_VERSION,
        "probeRunId": "run-1",
        "lastFullRunAt": _iso(NOW - timedelta(hours=1)),
    }
    base.update(fields)
    return WatchState.model_validate(base)


# --- §2.4: when the worker skips triage --------------------------------------------------------


def test_a_fresh_probe_is_triaged():
    assert skip_reason(_state(), NOW) is None


def test_no_state_is_no_probe():
    assert skip_reason(None, NOW) == "no_probe"


def test_a_state_without_a_probe_is_no_probe():
    assert skip_reason(_state(probe=None), NOW) == "no_probe"


def test_a_stale_fingerprint_version_forces_a_full_run():
    assert skip_reason(_state(fingerprintVersion=FINGERPRINT_VERSION + 1), NOW) == "version_mismatch"
    assert skip_reason(_state(fingerprintVersion=None), NOW) == "version_mismatch"


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(minutes=-5)])
def test_a_wake_at_reached_forces_a_full_run(delta):
    assert skip_reason(_state(wakeAt=_iso(NOW + delta)), NOW) == "wake_due"


def test_a_wake_at_still_ahead_does_not():
    assert skip_reason(_state(wakeAt=_iso(NOW + timedelta(minutes=5))), NOW) is None


def test_the_staleness_floor_forces_a_full_run():
    assert skip_reason(_state(lastFullRunAt=_iso(NOW - timedelta(hours=24))), NOW, staleness_hours=24) == "stale"
    assert skip_reason(_state(lastFullRunAt=_iso(NOW - timedelta(hours=23))), NOW, staleness_hours=24) is None


def test_no_last_full_run_is_stale():
    assert skip_reason(_state(lastFullRunAt=None), NOW) == "stale"


def test_the_floor_defaults_to_a_day_and_is_bounded(monkeypatch):
    monkeypatch.delenv("WATCH_TRIAGE_MAX_STALENESS_HOURS", raising=False)
    assert max_staleness_hours() == 24
    monkeypatch.setenv("WATCH_TRIAGE_MAX_STALENESS_HOURS", "6")
    assert max_staleness_hours() == 6
    monkeypatch.setenv("WATCH_TRIAGE_MAX_STALENESS_HOURS", "0")
    assert max_staleness_hours() == 1
    monkeypatch.setenv("WATCH_TRIAGE_MAX_STALENESS_HOURS", "nonsense")
    assert max_staleness_hours() == 24


def test_the_env_floor_applies(monkeypatch):
    monkeypatch.setenv("WATCH_TRIAGE_MAX_STALENESS_HOURS", "2")
    assert skip_reason(_state(lastFullRunAt=_iso(NOW - timedelta(hours=3))), NOW) == "stale"


# --- The request ------------------------------------------------------------------------------


def test_the_request_is_the_stored_probe_in_order():
    second = {"name": "github___get_pull", "input": '{"number":7}', "fingerprint": FP_B}
    request = triage_request_from_state("sched-1", _state(probe=[CALL, second]))
    assert [(c.name, c.arguments()) for c in request.calls] == [
        ("github___list_notifications", {"since": "2026-11-24T00:00:00Z"}),
        ("github___get_pull", {"number": 7}),
    ]
    assert request.baseline == [FP_A, FP_B]
    dumped = request.model_dump(by_alias=True)
    assert set(dumped) == {"scheduleId", "calls", "baseline", "fingerprintVersion"}


def test_a_request_needs_one_baseline_per_call():
    with pytest.raises(ValidationError):
        WatchTriageRequest(schedule_id="s", calls=[{"name": "t", "input": "{}"}], baseline=[], fingerprint_version=1)
    with pytest.raises(ValidationError):
        WatchTriageRequest(schedule_id="s", calls=[{"name": "t", "input": "{}"}], baseline=["short"], fingerprint_version=1)


def test_a_request_is_bounded_like_the_probe():
    calls = [{"name": "t", "input": "{}"}] * (MAX_PROBE_CALLS + 1)
    with pytest.raises(ValidationError):
        WatchTriageRequest(schedule_id="s", calls=calls, baseline=[FP_A] * len(calls), fingerprint_version=1)
    with pytest.raises(ValidationError):
        WatchTriageRequest(schedule_id="s", calls=[{"name": "t", "input": "[1]"}], baseline=[FP_A], fingerprint_version=1)


# --- Tier 1 -----------------------------------------------------------------------------------


def test_every_fingerprint_matching_is_quiet():
    verdict = compare_fingerprints([FP_A, FP_B], [FP_A, FP_B])
    assert (verdict.verdict, verdict.reason, verdict.tier) == ("quiet", "unchanged", 1)


@pytest.mark.parametrize("replayed", [[FP_A, FP_A], [FP_B, FP_A], [FP_A], [FP_A, None]])
def test_any_difference_escalates_with_no_decider(replayed):
    verdict = compare_fingerprints([FP_A, FP_B], replayed)
    assert (verdict.verdict, verdict.reason, verdict.tier) == ("escalate", "changed_no_decider", 1)


def test_the_frame_payload():
    payload = compare_fingerprints([FP_A], [FP_A]).payload()
    assert payload == {
        "type": "watch_triage_result",
        "verdict": "quiet",
        "tier": 1,
        "reason": "unchanged",
        "fingerprints": [FP_A],
    }


# --- The worker reading a frame ---------------------------------------------------------------


def test_a_frame_reads_back_as_its_verdict():
    payload = compare_fingerprints([FP_A], [FP_A]).payload()
    assert verdict_from_frame(payload) == compare_fingerprints([FP_A], [FP_A])


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"verdict": "quiet", "reason": "changed_no_decider", "tier": 1},
        {"verdict": "quiet", "reason": "probe_error", "tier": None},
        {"verdict": "maybe", "reason": "unchanged", "tier": 1},
        {"verdict": "quiet", "reason": "unchanged", "tier": 7},
        {"verdict": "escalate", "reason": "something_new", "tier": None},
    ],
)
def test_a_frame_the_worker_cannot_trust_escalates(payload):
    verdict = verdict_from_frame(payload)
    assert (verdict.verdict, verdict.reason) == ("escalate", TRIAGE_FAILED)


def test_the_history_map_holds_enums_and_a_duration_only():
    assert history_triage(compare_fingerprints([FP_A], [FP_A]), ms=812) == {
        "verdict": "quiet",
        "reason": "unchanged",
        "tier": 1,
        "ms": 812,
    }
    assert history_triage(escalate("probe_refused"), ms=40) == {"verdict": "escalate", "reason": "probe_refused", "ms": 40}
    assert history_triage(None, ms=0, skipped="no_probe") == {"verdict": "skipped", "reason": "no_probe"}
