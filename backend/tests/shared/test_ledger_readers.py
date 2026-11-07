"""The release ledger's read side (docs/specs/release-ledger.md §6.1–§6.3): pure pieces.

Window selection, the unstamped fallback, mix shift and caveats
(``ledger_readout``); the CloudWatch join's floor and cache
(``ledger_latency``); the provenance join's attribution and gate
(``ledger_provenance``); the export's second line of content defence and the
store's query shapes (``ledger_reader``); the fleet feedback version arm.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apis.app_api.admin.feedback.fleet import build_day_key, build_fleet_report, thumbs_by_build_day
from apis.shared.costs import ledger_latency, ledger_provenance, ledger_readout
from apis.shared.costs.ledger_reader import (
    LedgerStore,
    Unit,
    export_row,
    months_between,
    resolve_range,
    version_timeline,
)

TODAY = date(2026, 10, 10)


def _v(day, version, build, **counters):
    return {"PK": "LEDGER#VERSION", "SK": f"{day}#{version}#{build}", "date": day, "appVersion": version,
            "build": build, **{k: Decimal(str(v)) for k, v in counters.items()}}


# ---------------------------------------------------------------------------
# Window selection
# ---------------------------------------------------------------------------

ROWS = [
    _v("2026-10-01", "1.0.0", "a1", calls=10, turns=10, cost=1),
    _v("2026-10-02", "1.0.0", "a1", calls=10, turns=10, cost=1),
    _v("2026-10-03", "1.0.0", "a2", calls=10, turns=10, cost=1),  # a second 1.0.0 build
    _v("2026-10-04", "1.0.0", "a2", calls=10, turns=10, cost=1),
    _v("2026-10-04", "1.1.0", "b1", calls=10, turns=10, cost=2),  # deploy day
    _v("2026-10-05", "1.1.0", "b1", calls=10, turns=10, cost=2),
    _v("2026-10-06", "1.1.0", "b1", calls=10, turns=10, cost=2),
    _v("2026-10-10", "1.1.0", "b1", calls=10, turns=10, cost=2),  # today: never full
]


def test_default_candidate_is_the_newest_with_a_full_day_and_baseline_the_one_before():
    sel = ledger_readout.select_windows(ROWS, today=TODAY, days=7)
    assert sel["candidate"]["unit"] == Unit("1.1.0") and sel["baseline"]["unit"] == Unit("1.0.0")
    # The candidate's FIRST full days, the baseline's LAST; the deploy day is neither's.
    assert sel["candidate"]["dates"] == ["2026-10-05", "2026-10-06"]
    assert sel["baseline"]["dates"] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert sel["byBuild"] is False


def test_days_limits_each_window_from_its_own_end():
    sel = ledger_readout.select_windows(ROWS, today=TODAY, days=1)
    assert sel["candidate"]["dates"] == ["2026-10-05"] and sel["baseline"]["dates"] == ["2026-10-03"]


def test_builds_are_compared_as_builds_and_fall_back_to_partial_days():
    sel = ledger_readout.select_windows(ROWS, candidate=Unit("1.0.0", "a2"), today=TODAY)
    assert sel["byBuild"] is True and sel["baseline"]["unit"] == Unit("1.0.0", "a1")
    assert sel["candidate"]["dates"] == ["2026-10-03"]  # 10-04 is shared with b1
    only_shared = [_v("2026-10-04", "1.0.0", "x", calls=1), _v("2026-10-04", "1.0.0", "y", calls=1)]
    sel = ledger_readout.select_windows(only_shared, candidate=Unit("1.0.0", "y"), baseline=Unit("1.0.0", "x"), today=TODAY)
    assert sel["candidate"]["partial"] and sel["candidate"]["dates"] == ["2026-10-04"]
    assert sel["candidate"]["sharedDates"] == ["2026-10-04"]


def test_unknown_units_and_mixed_granularity_are_refused():
    with pytest.raises(LookupError):
        ledger_readout.select_windows(ROWS, candidate=Unit("9.9.9"), today=TODAY)
    with pytest.raises(ValueError):
        ledger_readout.select_windows(ROWS, candidate=Unit("1.1.0", "b1"), baseline=Unit("1.0.0"), today=TODAY)
    with pytest.raises(LookupError):
        ledger_readout.select_windows([], today=TODAY)


def test_the_oldest_unit_has_no_baseline_and_says_so():
    sel = ledger_readout.select_windows(ROWS, candidate=Unit("1.0.0"), today=TODAY)
    assert sel["baseline"] is None
    bundle = ledger_readout.build_readout(sel, version_rows=ROWS)
    assert bundle["baseline"] is None
    assert [c["code"] for c in bundle["caveats"]][0] == "no_baseline"


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


def _model(day, version, build, model, calls):
    return {"date": day, "appVersion": version, "build": build, "modelId": model, "calls": Decimal(calls),
            "turns": Decimal(calls), "cost": Decimal("0.1")}


def test_mix_shift_warns_on_a_moved_model_share_and_ignores_crumbs():
    sel = ledger_readout.select_windows(ROWS, today=TODAY)
    models = [
        _model("2026-10-02", "1.0.0", "a1", "haiku", 60), _model("2026-10-02", "1.0.0", "a1", "sonnet", 40),
        _model("2026-10-05", "1.1.0", "b1", "haiku", 95), _model("2026-10-05", "1.1.0", "b1", "sonnet", 4),
        _model("2026-10-05", "1.1.0", "b1", "tiny", 1),
    ]
    bundle = ledger_readout.build_readout(sel, version_rows=ROWS, model_rows=models)
    assert bundle["mixShift"]["warn"] is True
    assert bundle["mixShift"]["models"]["haiku"] == [0.6, 0.95]
    message = next(c["message"] for c in bundle["caveats"] if c["code"] == "mix_shift")
    assert "haiku" in message and "sonnet" in message and "tiny" not in message


def test_a_kpi_below_its_floor_is_null_never_zero_and_named_in_a_caveat():
    sel = ledger_readout.select_windows(ROWS, today=TODAY)
    bundle = ledger_readout.build_readout(sel, version_rows=ROWS)
    empty = bundle["kpis"]["emptyAnswerShare"]
    assert empty["baseline"] is None and empty["candidate"] is None and empty["delta"] is None
    assert bundle["kpis"]["costPerTurn"]["delta"] == pytest.approx(1.0)  # $0.10 → $0.20 a turn
    assert set(bundle["kpis"]["costPerTurn"]) >= {"baseline", "candidate", "delta", "n", "byModel"}
    codes = {c["code"] for c in bundle["caveats"]}
    # No outcome rows at all is "not measured", not "under the floor".
    assert {"short_window", "thumbs_below_floor"} <= codes and "below_floor" not in codes
    one_day = ledger_readout.build_readout(ledger_readout.select_windows(ROWS, today=TODAY, days=1), version_rows=ROWS)
    floors = [c["message"] for c in one_day["caveats"] if c["code"] == "below_floor"]
    assert any(m.startswith("partialMissShare under its floor (n=10 / 10)") for m in floors)


def test_every_caveat_uses_a_known_code():
    sel = ledger_readout.select_windows(ROWS, candidate=Unit("1.1.0", "b1"), today=TODAY)
    rows = ROWS + [_v("2026-10-05", "1.1.0", "git-0123456789ab", calls=1, estimatedCalls=1, unmeteredCalls=1)]
    bundle = ledger_readout.build_readout(sel, version_rows=rows, latency={"candidate": {"available": False}})
    assert all(c["code"] in ledger_readout.CAVEAT_CODES for c in bundle["caveats"])
    assert "latency_unavailable" in {c["code"] for c in bundle["caveats"]}


def test_unstamped_items_count_only_on_a_date_one_unit_owns():
    sole = ledger_readout.sole_units_by_date(ROWS, by_build=False)
    assert sole["2026-10-05"] == "1.1.0" and "2026-10-04" not in sole
    by_key = {
        "unstamped#unstamped|2026-10-05": {"up": 1},
        "unstamped#unstamped|2026-10-04": {"up": 5},  # deploy day: ambiguous, dropped
        "1.1.0#b1|2026-10-06": {"up": 2},
        "1.1.0#b1|2026-10-09": {"up": 7},  # outside the window
    }
    side = {"unit": Unit("1.1.0"), "dates": {"2026-10-04", "2026-10-05", "2026-10-06"}}
    assert ledger_readout.counts_in(by_key, side, sole) == {"up": 3}
    assert ledger_readout.counts_in(by_key, side) == {"up": 2}
    assert ledger_readout.counts_in(by_key, {"unit": Unit("1.1.0", "zz"), "dates": side["dates"]}, sole) == {}


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def test_ranges_and_months():
    assert resolve_range("2026-10-01", "2026-10-03") == ("2026-10-01", "2026-10-03")
    with pytest.raises(ValueError):
        resolve_range("2026-10-03", "2026-10-01")
    with pytest.raises(ValueError):
        resolve_range("2025-01-01", "2026-10-01")
    assert months_between("2026-11-30", "2027-02-01") == ["2026-11", "2026-12", "2027-01", "2027-02"]


def test_unit_parse_rejects_text_that_is_not_an_id():
    assert Unit.parse("1.33.0#abc").key == "1.33.0#abc"
    for bad in ("", "#abc", "1.0 <script>", "a" * 200):
        with pytest.raises(ValueError):
            Unit.parse(bad)


def test_store_queries_known_partitions_only():
    calls = []

    class Table:
        def query(self, **kwargs):
            calls.append(kwargs)
            return {"Items": []}

    store = LedgerStore(Table())
    store.version_rows("2026-10-30", "2026-11-02")
    store.family_rows("TOOL", "2026-10-30", "2026-11-02")
    assert [c["ExpressionAttributeValues"][":pk"] for c in calls] == [
        "LEDGER#VERSION", "LEDGER#TOOL#2026-10", "LEDGER#TOOL#2026-11"]
    assert all("BETWEEN" in c["KeyConditionExpression"] for c in calls)
    assert calls[0]["ExpressionAttributeValues"][":hi"] == "2026-11-02#￿"
    with pytest.raises(ValueError):
        store.family_rows("SESSION", "2026-10-30", "2026-11-02")


def test_export_row_keeps_ids_and_numbers_and_drops_prose():
    row = {"PK": "LEDGER#EVAL", "SK": "2026-10-04T02:00:00+00:00", "cost": Decimal("0.25"), "calls": Decimal(3),
           "gitCommit": "8f676bb2e", "note": "the model said: hello there", "families": {
               "factual": {"n": Decimal(20), "gate": "pass", "why": "a sentence, with spaces"}}}
    out = export_row(row)
    assert out["cost"] == 0.25 and out["calls"] == 3 and out["gitCommit"] == "8f676bb2e"
    assert "note" not in out and out["families"] == {"factual": {"n": 20, "gate": "pass"}}


def test_timeline_matches_deploy_markers_by_build():
    markers = [{"service": "app-api", "build": "b1", "firstSeenAt": "2026-10-04T09:00:00+00:00"}]
    versions, builds = version_timeline(ROWS, markers)
    assert [v["appVersion"] for v in versions] == ["1.0.0", "1.1.0"]
    assert versions[0]["builds"] == 2
    b1 = next(b for b in builds if b["build"] == "b1")
    assert b1["deployMarkers"][0]["service"] == "app-api" and b1["buildKind"] == "image"


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------


class FakeCloudWatch:
    def __init__(self, n=50):
        self.calls = []
        self.n = n

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        results = []
        for query in kwargs["MetricDataQueries"]:
            stat = query["MetricStat"]["Stat"]
            value = {"p50": 1500.0, "p90": 3000.0, "SampleCount": float(self.n)}[stat]
            results.append({"Label": query["Label"], "Values": [value]})
        return {"MetricDataResults": results}


def test_one_call_per_window_with_one_period_spanning_it():
    client = FakeCloudWatch()
    out = ledger_latency.fetch_window_latency(("2026-10-01", "2026-10-07"), client=client)
    assert len(client.calls) == 1
    queries = client.calls[0]["MetricDataQueries"]
    assert {q["MetricStat"]["Period"] for q in queries} == {7 * 86400}
    assert {q["MetricStat"]["Metric"]["Dimensions"] == [] for q in queries} == {True}
    assert out["available"] and out["metrics"]["serverFirstTokenMs"] == {"p50": 1500.0, "p90": 3000.0, "n": 50}


def test_percentiles_below_the_floor_are_null():
    out = ledger_latency.fetch_window_latency(("2026-10-01", "2026-10-01"), client=FakeCloudWatch(n=9))
    assert out["metrics"]["probeColdFirstTokenMs"] == {"p50": None, "p90": None, "n": 9}


def test_a_failed_read_is_unavailable_not_an_error():
    class Broken:
        def get_metric_data(self, **kwargs):
            raise RuntimeError("AccessDenied")

    assert ledger_latency.fetch_window_latency(("2026-10-01", "2026-10-01"), client=Broken()) == {
        "available": False, "metrics": {}}


def test_window_latency_is_cached_per_window():
    ledger_latency.clear_cache()
    seen = []

    def loader(window):
        seen.append(window)
        return {"available": True, "metrics": {}}

    async def run():
        await asyncio.gather(*(ledger_latency.window_latency(("2026-10-01", "2026-10-02"), loader) for _ in range(5)))
        await ledger_latency.window_latency(("2026-10-03", "2026-10-04"), loader)

    asyncio.run(run())
    assert seen == [("2026-10-01", "2026-10-02"), ("2026-10-03", "2026-10-04")]
    ledger_latency.clear_cache()


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_verdicts_are_attributed_to_the_cost_row_before_them():
    records = {"s1": [
        {"timestamp": "2026-10-05T10:00:00+00:00", "appVersion": "1.1.0", "build": "b1"},
        {"timestamp": "2026-10-05T12:00:00+00:00", "appVersion": "1.1.0", "build": "b2"},
    ]}
    verdicts = [
        {"sessionId": "s1", "createdAt": "2026-10-05T10:00:05+00:00", "states": ["supported", "quote_unsupported"]},
        {"sessionId": "s1", "createdAt": "2026-10-05T12:30:00+00:00", "states": ["unverified"]},
        {"sessionId": "gone", "createdAt": "2026-10-05T12:30:00+00:00", "states": ["supported"]},
    ]
    out = ledger_provenance.attribute(verdicts, records, build_day_key)
    assert out["unattributed"] == 1
    assert out["byKey"]["1.1.0#b1|2026-10-05"]["quote_unsupported"] == 1
    assert out["byKey"]["1.1.0#b2|2026-10-05"] == {
        "verdicts": 1, "claims": 1, "supported": 0, "quote_unsupported": 0, "unassessed": 0, "unverified": 1}


def test_the_join_reads_nothing_when_the_ledger_shows_no_verify_sources_calls():
    storage = SimpleNamespace(get_top_users_by_cost=AsyncMock(), sessions_metadata_table=object())
    out = asyncio.run(ledger_provenance.provenance_by_unit(
        storage, from_date="2026-10-01", to_date="2026-10-05",
        tool_rows=[{"toolId": "browse_web", "calls": Decimal(9)}], key_of=build_day_key,
    ))
    assert out["coverage"]["skipped"] == "no_verify_sources_calls"
    storage.get_top_users_by_cost.assert_not_awaited()


def test_the_join_walks_top_users_and_returns_states_only():
    queries = []

    class Table:
        def query(self, **kwargs):
            queries.append(kwargs)
            return {"Items": [{"sessionId": "s1", "createdAt": "2026-10-05T10:00:05+00:00",
                               "claims": [{"state": "supported"}, {"state": "quote_unsupported"}]}]}

    storage = SimpleNamespace(
        sessions_metadata_table=Table(),
        get_top_users_by_cost=AsyncMock(return_value=[{"userId": "u1"}]),
        get_session_cost_records=AsyncMock(return_value=[
            {"timestamp": "2026-10-05T10:00:00+00:00", "appVersion": "1.1.0", "build": "b1"}]),
    )
    out = asyncio.run(ledger_provenance.provenance_by_unit(
        storage, from_date="2026-10-01", to_date="2026-10-05",
        tool_rows=[{"toolId": "verify_sources", "calls": Decimal(1)}], key_of=build_day_key,
    ))
    assert out["byKey"]["1.1.0#b1|2026-10-05"]["claims"] == 2
    assert out["coverage"]["usersScanned"] == 1 and out["coverage"]["verdicts"] == 1
    assert queries[0]["ExpressionAttributeValues"][":pk"] == "USER#u1"
    assert "quote" not in queries[0]["ProjectionExpression"]


# ---------------------------------------------------------------------------
# Fleet feedback: the version arm
# ---------------------------------------------------------------------------


def test_fleet_report_has_a_version_arm_with_counts_below_the_floor():
    thumbs = [{"sessionId": "s1", "messageId": 1, "value": -1, "signal": "explicit"},
              {"sessionId": "s1", "messageId": 2, "value": 1, "signal": "explicit"}]
    records = {"s1": [
        {"messageId": 1, "timestamp": "2026-10-05T10:00:00", "appVersion": "1.1.0", "build": "b1"},
        {"messageId": 2, "timestamp": "2026-10-05T11:00:00"},
    ]}
    report = build_fleet_report(thumbs, records, window={"start": "a", "end": "b", "days": 1}, floor=20)
    arms = {a["key"]: a for a in report["arms"]["version"]}
    assert arms["1.1.0"]["down"] == 1 and arms["1.1.0"]["downRate"] is None and arms["1.1.0"]["belowFloor"]
    assert arms["unstamped"]["up"] == 1


def test_thumbs_by_build_day_counts_retries_on_down_thumbs():
    thumbs = [{"sessionId": "s1", "messageId": 1, "value": -1, "retryMessageId": 2},
              {"sessionId": "s1", "messageId": 1, "value": -1, "signal": "implicit"},
              {"sessionId": "s9", "messageId": 1, "value": 1}]
    records = {"s1": [{"messageId": 1, "timestamp": "2026-10-05T10:00:00", "appVersion": "1.1.0", "build": "b1"}]}
    assert thumbs_by_build_day(thumbs, records) == {"1.1.0#b1|2026-10-05": {"up": 0, "down": 1, "retries": 1}}
