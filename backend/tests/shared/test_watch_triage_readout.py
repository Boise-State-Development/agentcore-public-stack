"""The triage shadow readout's rules (docs/specs/quiet-watcher-triage.md §6.5)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apis.shared.scheduled_prompts.runs import ScheduleRun
from apis.shared.scheduled_prompts.triage_readout import MIN_TRIAGED, percentile, render, summarize

PRICING = {
    "inputPricePerMtok": 0.11,
    "outputPricePerMtok": 0.55,
    "cacheWritePricePerMtok": 0.1375,
    "cacheReadPricePerMtok": 0.011,
}


def _run(i: int, status: str = "quiet", triage=None, **tokens) -> ScheduleRun:
    return ScheduleRun(
        run_id=f"run-{i}",
        schedule_id="sched-1",
        status=status,
        finished_at=f"2026-11-27T10:{i:02d}:00Z",
        triage=triage,
        input_tokens=tokens.get("inp", 8),
        output_tokens=tokens.get("out", 300),
        cache_write_tokens=tokens.get("cw", 6000),
        cache_read_tokens=tokens.get("cr", 12000),
    )


QUIET = {"verdict": "quiet", "reason": "unchanged", "tier": 1, "ms": 2000}
CHANGED = {"verdict": "escalate", "reason": "changed_no_decider", "tier": 1, "ms": 3000}


def test_verdicts_reasons_and_misses_are_counted():
    rows = [
        _run(0, triage={"verdict": "skipped", "reason": "no_probe"}),
        _run(1, triage=QUIET),
        _run(2, status="notify", triage=QUIET),
        _run(3, status="notify", triage=CHANGED),
        _run(4),
    ]
    r = summarize("Watcher", "m", rows)
    assert r.runs == 5
    assert r.statuses == {"quiet": 3, "notify": 2}
    assert r.verdicts == {"skipped": 1, "quiet": 2, "escalate": 1, "none": 1}
    assert r.reasons == {"no_probe": 1, "unchanged": 2, "changed_no_decider": 1}
    assert r.triage_ms == [2000, 2000, 3000]
    assert r.shadow_misses == ["run-2"]  # quiet triage, notifying run
    assert r.triaged == 3


def test_a_settle_rate_under_the_floor_is_no_evidence_not_zero():
    few = summarize("W", "m", [_run(i, triage=CHANGED) for i in range(MIN_TRIAGED - 1)])
    assert few.settle_rate is None
    enough = summarize("W", "m", [_run(i, triage=QUIET if i % 4 else CHANGED) for i in range(MIN_TRIAGED)])
    assert enough.settle_rate == pytest.approx(0.75)


def test_cost_is_the_full_runs_tokens_at_catalog_rates():
    r = summarize("W", "m", [_run(0), _run(1)], pricing=PRICING)
    expected = (16 * 0.11 + 600 * 0.55 + 12000 * 0.1375 + 24000 * 0.011) / 1_000_000
    assert r.cost_usd == pytest.approx(expected)
    assert r.cost_per_run == pytest.approx(expected / 2)
    assert summarize("W", "m", [_run(0)]).cost_usd is None  # no rates, no number


def test_raw_items_read_like_models():
    item = {"runId": "run-9", "status": "notify", "triage": QUIET, "inputTokens": 1}
    assert summarize("W", "m", [item]).shadow_misses == ["run-9"]


def test_percentile_is_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([5], 95) == 5
    assert percentile(list(range(1, 101)), 50) == 50
    assert percentile(list(range(1, 101)), 95) == 95


def test_the_rendered_readout_has_no_ids_but_the_misses():
    readouts = [summarize("Triage readout: clock", "m", [_run(1, status="notify", triage=QUIET)], pricing=PRICING)]
    text = render(readouts, window="since 2026-11-27T00:00:00Z")
    assert "Triage readout: clock" in text and "run-1" in text
    assert "sched-1" not in text
    assert "| 1 | " in text  # one shadow miss


def test_since_takes_a_duration_or_a_timestamp():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "watch_triage_readout.py"
    spec = importlib.util.spec_from_file_location("watch_triage_readout", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    assert module.since_iso("6h", now) == "2026-11-27T06:00:00Z"
    assert module.since_iso("7d", now) == "2026-11-20T12:00:00Z"
    assert module.since_iso("2026-11-27T15:00:00") == "2026-11-27T15:00:00Z"
