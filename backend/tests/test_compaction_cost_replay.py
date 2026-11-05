"""Tests for the counterfactual compaction cost replay (scripts/compaction_cost_replay.py)."""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from compaction_cost_replay import (  # noqa: E402
    ArmResult, Call, CutModel, fill_bases, idle_profile, load_sessions, parse_arm, replay,
)

BASE = 2e-6  # $/token at 1x: Sonnet 5 Global's $2.00/MTok
T0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
NO_SUMMARY = CutModel(summary_usd=0.0, summary_tokens=0, protected_turns=1)


def call(seconds: float, *, read: float = 0, write: float = 0, uncached: float = 0, output_usd: float = 0.0) -> Call:
    read_cost, write_cost = read * 0.1 * BASE, write * 1.25 * BASE
    return Call(
        ts=T0 + timedelta(seconds=seconds), model="m", uncached=uncached, read=read, write=write,
        cost=read_cost + write_cost + output_usd, read_cost=read_cost, write_cost=write_cost, base=BASE,
    )


def run_arm(calls, spec, model=NO_SUMMARY) -> ArmResult:
    result = ArmResult()
    replay(calls, parse_arm(spec, 300), model, result)
    return result


def chat(*turns):
    """One call per (second, read, write) turn."""
    return [call(s, read=r, write=w) for s, r, w in turns]


class TestArms:
    def test_parse(self):
        arm = parse_arm("idle:50k:20k", 300)
        assert (arm.kind, arm.trigger, arm.target, arm.idle_after) == ("idle", 50_000, 20_000, 300)
        assert parse_arm("idle:50k:20k:900", 300).idle_after == 900
        assert parse_arm("ttl1h+idle:50k:20k", 300).idle_after == 3600

    @pytest.mark.parametrize("spec", ["idle:20k:50k", "nope", "idle:50k"])
    def test_parse_rejects(self, spec):
        with pytest.raises(ValueError):
            parse_arm(spec, 300)

    def test_observed_reproduces_the_bill(self):
        calls = chat((0, 0, 10_000), (100, 10_000, 5_000), (900, 0, 15_000))
        assert run_arm(calls, "observed").cost == pytest.approx(sum(c.cost for c in calls))


class TestIdleCut:
    def session(self):
        # Static 10k; three warm turns grow history to 60k; a 10-minute pause; a full re-write.
        return chat((0, 0, 10_000), (100, 10_000, 20_000), (200, 30_000, 20_000),
                    (300, 50_000, 10_000), (900, 0, 62_000), (1000, 62_000, 1_000))

    def test_return_rewrites_only_what_survives(self):
        calls = self.session()
        r = run_arm(calls, "idle:50k:20k")
        # history 60k - 10k static = 50k, cut to 20k: 30k fewer tokens on the return and after.
        saved = 30_000 * 1.25 * BASE + 30_000 * 0.1 * BASE
        assert r.cuts == 1 and r.tokens_removed == 30_000
        assert sum(c.cost for c in calls) - r.cost == pytest.approx(saved)

    def test_below_trigger_never_cuts(self):
        assert run_arm(self.session(), "idle:100k:20k").cuts == 0

    def test_job_after_last_call_is_wasted_when_nobody_returns(self):
        calls = self.session()[:4]  # ends at 60k, never resumed
        r = run_arm(calls, "idle:50k:20k", CutModel(summary_usd=0.01, summary_tokens=0, protected_turns=1))
        assert (r.cuts, r.wasted_cuts, r.summary_cost) == (1, 1, pytest.approx(0.01))
        assert r.cost == pytest.approx(sum(c.cost for c in calls))

    def test_cold_cut_spends_nothing_on_a_session_that_ends(self):
        r = run_arm(self.session()[:4], "cold:50k:20k", CutModel(summary_usd=0.01, summary_tokens=0))
        assert (r.cuts, r.summary_cost) == (0, 0)

    def test_protected_tail_bounds_the_cut(self):
        # The last turn alone added 40k: the cut keeps it plus the summary, so 50k -> 43k.
        calls = chat((0, 0, 10_000), (100, 10_000, 5_000), (200, 15_000, 5_000),
                     (300, 20_000, 40_000), (900, 0, 62_000))
        r = run_arm(calls, "idle:50k:20k", CutModel(summary_usd=0, summary_tokens=3_000, protected_turns=1))
        assert sum(c.cost for c in calls) - r.cost == pytest.approx((50_000 - 43_000) * 1.25 * BASE)

    def test_too_few_turns_is_all_tail(self):
        calls = chat((0, 0, 10_000), (100, 10_000, 50_000), (900, 0, 62_000))
        assert run_arm(calls, "idle:50k:20k", CutModel(summary_usd=0, summary_tokens=0, protected_turns=3)).cuts == 0

    def test_production_compaction_catches_up(self):
        # Production cuts to 20k on its own after the return; the arm stops claiming the removal.
        before = self.session()
        after = chat((1100, 0, 20_000), (1200, 20_000, 1_000))
        whole = run_arm(before + after, "idle:50k:20k").cost
        assert whole - run_arm(before, "idle:50k:20k").cost == pytest.approx(sum(c.cost for c in after))


class TestTtl1h:
    def test_return_within_the_hour_reads(self):
        calls = chat((0, 0, 40_000), (900, 0, 41_000))
        r = run_arm(calls, "ttl1h")
        # Both writes at 2x; the return reads the 40k the first call cached and writes 1k.
        expected = 40_000 * 2 * BASE + 40_000 * 0.1 * BASE + 1_000 * 2 * BASE
        assert r.cost == pytest.approx(expected)

    def test_return_after_the_hour_still_rewrites(self):
        calls = chat((0, 0, 40_000), (4000, 0, 41_000))
        assert run_arm(calls, "ttl1h").cost == pytest.approx(81_000 * 2 * BASE)


class TestLoading:
    def test_unmarshals_and_skips_api_and_unpriced(self):
        def item(sid, ts, cost):
            row = {"sessionId": {"S": sid}, "timestamp": {"S": ts},
                   "tokenUsage": {"M": {"cacheWriteInputTokens": {"N": "1000"}}}}
            if cost is not None:
                row["cost"] = {"M": {"total": {"N": str(cost)}, "cacheWriteCost": {"N": str(cost)}}}
            return row

        sessions, skipped = load_sessions([
            item("s1", "2026-10-20T00:00:10+00:00", 0.0025),
            item("s1", "2026-10-20T00:00:00+00:00", 0.0025),
            item("api-converse-x", "2026-10-20T00:00:00+00:00", 0.1),
            item("s2", "2026-10-20T00:00:00+00:00", None),
        ])
        assert list(sessions) == ["s1"] and skipped == 2
        assert sessions["s1"][0].ts < sessions["s1"][1].ts
        assert sessions["s1"][0].base == pytest.approx(0.0025 / 1250)

    def test_fill_bases_uses_the_model_median(self):
        sessions = {"s": [call(0, write=1000), Call(T0, "m", 5, 0, 0, 0.0, 0, 0, None)]}
        fill_bases(sessions)
        assert sessions["s"][1].base == pytest.approx(BASE)


def test_idle_profile_counts_returns_and_censors_the_tail():
    end = T0 + timedelta(days=10)
    sessions = {
        "returned": chat((0, 0, 30_000), (900, 0, 31_000)),
        "abandoned": chat((0, 0, 30_000)),
        "recent": [call((end - T0).total_seconds() - 3600, write=30_000)],
    }
    p = idle_profile(sessions, 300, end, censor_hours=72)
    band = p["return_rate"]["25-50k"]
    assert (band["returned"], band["idle_events"]) == (1, 3)  # "returned"'s last call is a non-return
    assert p["by_context"]["25-50k"]["calls"] == 1
    assert p["idle_rewrite_usd"] == pytest.approx(31_000 * 1.25 * BASE)
