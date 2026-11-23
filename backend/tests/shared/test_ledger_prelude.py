"""Per-build prelude stages for the release readout (``ledger_prelude`` + ``compare_prelude``)."""

from __future__ import annotations

import asyncio

import pytest

from apis.shared.costs import ledger_prelude, ledger_readout
from apis.shared.costs.ledger_reader import Unit

LOG_GROUP = "/aws/bedrock-agentcore/runtimes/test_runtime-AbC123-DEFAULT"
WINDOW = ("2026-11-10", "2026-11-16")


def _cells(values: dict) -> list:
    """A Logs Insights ``stats`` row for the given ``{stage: (n, p50, p90)}``."""
    row = []
    for index, name in enumerate(ledger_prelude.STAGES):
        n, p50, p90 = values.get(name, (0, None, None))
        row.append({"field": f"s{index}n", "value": str(n)})
        if p50 is not None:
            row.append({"field": f"s{index}p50", "value": str(p50)})
            row.append({"field": f"s{index}p90", "value": str(p90)})
    return [row]


class FakeLogs:
    def __init__(self, statuses=("Running", "Complete"), values=None, fail=None):
        self.statuses = list(statuses)
        self.values = values or {"preludeTotalMs": (500, 980.4, 1500.2), "preambleMs": (500, 345.0, 600.0)}
        self.fail = fail
        self.started = []
        self.stopped = []

    def start_query(self, **kwargs):
        if self.fail:
            raise self.fail
        self.started.append(kwargs)
        return {"queryId": "q-1"}

    def get_query_results(self, queryId):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"status": status, "results": _cells(self.values) if status == "Complete" else []}

    def stop_query(self, queryId):
        self.stopped.append(queryId)


def _fetch(unit=("1.39.0", None), client=None, **kwargs):
    return ledger_prelude.fetch_unit_prelude(
        (unit[0], unit[1], WINDOW), client=client or FakeLogs(), log_group=LOG_GROUP, sleep=lambda _: None, **kwargs,
    )


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


def test_query_filters_to_the_version_and_reads_both_record_shapes():
    query, scope = ledger_prelude.query_for(Unit("1.39.0"))
    assert scope == "version"
    assert 'appVersion = "1.39.0"' in query and "build =" not in query
    # turn_prelude's record carries PreludeTotalMs, turn_first_token's FirstTokenMs.
    assert "ispresent(PreludeTotalMs) or ispresent(FirstTokenMs)" in query
    for metric in ledger_prelude.STAGES.values():
        assert f"count({metric})" in query and f"pct({metric}, 50)" in query and f"pct({metric}, 90)" in query


def test_query_never_selects_the_session_id():
    query, _ = ledger_prelude.query_for(Unit("1.39.0", "73487c3e10a70349"))
    assert "sessionId" not in query


def test_a_stamped_build_narrows_to_the_build():
    query, scope = ledger_prelude.query_for(Unit("1.39.0", "73487c3e10a70349"))
    assert scope == "build" and 'build = "73487c3e10a70349"' in query


def test_a_commit_attributed_build_reads_its_version():
    """A deploy-map label is never a stamp an EMF record carries."""
    query, scope = ledger_prelude.query_for(Unit("1.39.0", "git-abc1234"))
    assert scope == "version" and "build =" not in query


def test_a_value_that_is_not_a_stamp_is_refused_not_quoted():
    with pytest.raises(ValueError):
        ledger_prelude.query_for(Unit('1.0" or 1=1'))
    assert _fetch(unit=('1.0" | stats', None))["reason"] == "unstamped"


def test_model_time_is_not_a_stage():
    assert "ModelMs" not in ledger_prelude.STAGES.values()
    assert "FirstTokenMs" not in ledger_prelude.STAGES.values()


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------


def test_a_complete_query_returns_every_stage_with_its_own_n():
    client = FakeLogs()
    out = _fetch(client=client)
    assert out["available"] and out["scope"] == "version"
    assert out["stages"]["preludeTotalMs"] == {"p50": 980.4, "p90": 1500.2, "n": 500}
    assert out["stages"]["ragMs"] == {"p50": None, "p90": None, "n": 0}
    started = client.started[0]
    assert started["logGroupName"] == LOG_GROUP
    assert started["endTime"] > started["startTime"]


def test_a_stage_under_the_share_floor_has_no_percentile():
    out = _fetch(client=FakeLogs(values={"preludeTotalMs": (19, 900.0, 1200.0)}))
    assert out["stages"]["preludeTotalMs"] == {"p50": None, "p90": None, "n": 19}


def test_no_log_group_is_unavailable_and_reads_nothing(monkeypatch):
    monkeypatch.delenv(ledger_prelude.RUNTIME_LOG_GROUP_ENV, raising=False)
    client = FakeLogs()
    out = ledger_prelude.fetch_unit_prelude(("1.39.0", None, WINDOW), client=client)
    assert out == {"available": False, "reason": "no_log_group", "stages": {}}
    assert client.started == []


def test_a_query_that_does_not_finish_is_stopped():
    client = FakeLogs(statuses=("Running",))
    out = _fetch(client=client, timeout=0)
    assert out["reason"] == "timeout" and client.stopped == ["q-1"]


@pytest.mark.parametrize("status", ["Failed", "Cancelled", "Timeout"])
def test_a_failed_query_is_unavailable(status):
    assert _fetch(client=FakeLogs(statuses=(status,)))["reason"] == "query_failed"


def test_an_error_is_unavailable_not_raised():
    assert _fetch(client=FakeLogs(fail=RuntimeError("AccessDenied")))["reason"] == "read_failed"


def test_unit_prelude_is_cached_per_unit_and_window():
    ledger_prelude.clear_cache()
    seen = []

    def loader(key):
        seen.append(key)
        return {"available": True, "scope": "version", "stages": {}}

    async def run():
        await asyncio.gather(*(ledger_prelude.unit_prelude(Unit("1.39.0"), WINDOW, loader) for _ in range(4)))
        await ledger_prelude.unit_prelude(Unit("1.38.0"), WINDOW, loader)

    asyncio.run(run())
    assert seen == [("1.39.0", None, WINDOW), ("1.38.0", None, WINDOW)]
    ledger_prelude.clear_cache()


# ---------------------------------------------------------------------------
# The comparison (the readout is the only place one is made)
# ---------------------------------------------------------------------------


def _side(version="1.39.0", build=None):
    return {"unit": Unit(version, build)}


def _side_prelude(stages, scope="version"):
    return {"available": True, "scope": scope, "stages": {
        name: {"p50": p50, "p90": p50 * 1.5 if p50 is not None else None, "n": n} for name, (n, p50) in stages.items()}}


def _compare(base_stages, cand_stages, **kwargs):
    return ledger_readout.compare_prelude(
        {"baseline": _side_prelude(base_stages, kwargs.get("scope_b", "version")),
         "candidate": _side_prelude(cand_stages, kwargs.get("scope_c", "version"))},
        kwargs.get("cand", _side()), kwargs.get("base", _side("1.38.0")),
    )


def test_a_rise_past_five_percent_with_enough_turns_moved_worse():
    out, caveats = _compare({"preludeTotalMs": (500, 1000.0)}, {"preludeTotalMs": (400, 1060.0)})
    assert out["stages"]["preludeTotalMs"]["deltaMs"] == 60.0
    assert out["stages"]["preludeTotalMs"]["moved"] == "worse"
    assert out["moved"] == [{"stage": "preludeTotalMs", "deltaMs": 60.0, "direction": "worse"}]
    assert caveats == []


def test_inside_the_noise_band_did_not_move():
    """+40 ms on a 1 s prelude is inside dev's 966–1,027 ms daily band (the 5% bar is 50 ms)."""
    out, _ = _compare({"preludeTotalMs": (500, 1000.0)}, {"preludeTotalMs": (500, 1040.0)})
    assert out["stages"]["preludeTotalMs"]["moved"] is None and out["moved"] == []


def test_a_small_stage_needs_the_30ms_floor_not_five_percent():
    out, _ = _compare({"headOfTurnMs": (500, 50.0), "preludeTotalMs": (500, 1000.0)},
                      {"headOfTurnMs": (500, 75.0), "preludeTotalMs": (500, 1000.0)})
    assert out["stages"]["headOfTurnMs"]["moved"] is None
    out, _ = _compare({"headOfTurnMs": (500, 50.0), "preludeTotalMs": (500, 1000.0)},
                      {"headOfTurnMs": (500, 85.0), "preludeTotalMs": (500, 1000.0)})
    assert out["stages"]["headOfTurnMs"]["moved"] == "worse"


def test_a_fall_is_better():
    out, _ = _compare({"agentBuildMs": (500, 320.0), "preludeTotalMs": (500, 1000.0)},
                      {"agentBuildMs": (500, 280.0), "preludeTotalMs": (500, 1000.0)})
    assert out["moved"] == [{"stage": "agentBuildMs", "deltaMs": -40.0, "direction": "better"}]


def test_too_few_turns_is_shown_but_never_moved():
    out, caveats = _compare({"preludeTotalMs": (199, 1000.0)}, {"preludeTotalMs": (500, 1500.0)})
    assert out["stages"]["preludeTotalMs"]["deltaMs"] == 500.0
    assert out["stages"]["preludeTotalMs"]["moved"] is None
    assert [c["code"] for c in caveats] == ["prelude_below_floor"]


def test_worse_stages_list_first():
    out, _ = _compare(
        {"preludeTotalMs": (500, 1000.0), "agentBuildMs": (500, 400.0), "ragMs": (500, 50.0)},
        {"preludeTotalMs": (500, 1100.0), "agentBuildMs": (500, 250.0), "ragMs": (500, 100.0)},
    )
    assert [(m["stage"], m["direction"]) for m in out["moved"]] == [
        ("preludeTotalMs", "worse"), ("ragMs", "worse"), ("agentBuildMs", "better")]


def test_an_unreadable_side_is_a_caveat_with_its_reason():
    out, caveats = ledger_readout.compare_prelude(
        {"baseline": {"available": False, "reason": "timeout", "stages": {}},
         "candidate": _side_prelude({"preludeTotalMs": (500, 1000.0)})},
        _side(), _side("1.38.0"),
    )
    assert out["available"] is False
    assert caveats == [{"code": "prelude_unavailable",
                        "message": "per-build prelude stages could not be read from Logs Insights (timeout)"}]


def test_a_build_comparison_read_by_version_says_so():
    out, caveats = _compare({"preludeTotalMs": (500, 1000.0)}, {"preludeTotalMs": (500, 1000.0)},
                            cand=_side("1.39.0", "git-abc"), base=_side("1.39.0", "git-def"))
    assert out["scope"] == "version"
    assert "prelude_version_scoped" in {c["code"] for c in caveats}


def test_no_prelude_given_is_an_empty_panel_and_no_caveat():
    out, caveats = ledger_readout.compare_prelude(None, _side(), _side("1.38.0"))
    assert out["available"] is False and caveats == []


def test_the_bundle_carries_the_prelude_through_the_response_model():
    from apis.app_api.admin.costs.ledger_models import ReadoutPrelude

    out, _ = _compare({"preludeTotalMs": (500, 1000.0)}, {"preludeTotalMs": (500, 1060.0)})
    dumped = ReadoutPrelude.model_validate(out).model_dump(by_alias=True, mode="json")
    assert dumped["stages"]["preludeTotalMs"]["deltaMs"] == 60.0
    assert dumped["moved"][0]["direction"] == "worse"
    assert dumped["rule"] == {"minMs": 30.0, "minRelative": 0.05, "minN": 200}


# ---------------------------------------------------------------------------
# Mix
# ---------------------------------------------------------------------------


def test_the_mix_is_counted_on_the_prelude_record_only():
    """Both records carry hasAssistant; summing both would double every streamed turn."""
    query, _ = ledger_prelude.query_for(Unit("1.39.0"))
    assert "ispresent(PreludeTotalMs) and hasAssistant = 1 as mixAssistant" in query
    assert "ispresent(PreludeTotalMs) and isResume = 1 as mixResume" in query
    rows = _cells({"preludeTotalMs": (500, 980.0, 1500.0)})
    rows[0] += [{"field": "mixAssistant", "value": "100"}, {"field": "mixResume", "value": "5"}]
    assert ledger_prelude.parse_mix(rows) == {"turns": 500, "assistantTurns": 100, "resumeTurns": 5}


def _with_mix(prelude, turns, assistant, resume=0):
    prelude["mix"] = {"turns": turns, "assistantTurns": assistant, "resumeTurns": resume}
    return prelude


def test_a_shift_in_agent_turns_is_a_prelude_mix_caveat():
    """Prod 1.37.0 → 1.38.0: rag fell 215 → 50 ms because Agent turns fell, not because retrieval got faster."""
    out, caveats = ledger_readout.compare_prelude(
        {"baseline": _with_mix(_side_prelude({"preludeTotalMs": (8933, 564.0)}), 8933, 6000),
         "candidate": _with_mix(_side_prelude({"preludeTotalMs": (2033, 769.0)}), 2033, 500)},
        _side("1.38.0"), _side("1.37.0"),
    )
    assert out["mix"]["assistantShare"] == [pytest.approx(0.6717, abs=1e-3), pytest.approx(0.2459, abs=1e-3)]
    assert [c["code"] for c in caveats] == ["prelude_mix_shift"]
    assert "assistantShare 0.67 → 0.25" in caveats[0]["message"]


def test_a_steady_mix_raises_nothing():
    out, caveats = ledger_readout.compare_prelude(
        {"baseline": _with_mix(_side_prelude({"preludeTotalMs": (873, 983.0)}), 873, 170, 15),
         "candidate": _with_mix(_side_prelude({"preludeTotalMs": (556, 942.0)}), 556, 110, 10)},
        _side(), _side("1.38.0"),
    )
    assert out["mix"]["resumeShare"][0] is not None and caveats == []
