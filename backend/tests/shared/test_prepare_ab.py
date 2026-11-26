"""The prepare A/B readout's pure logic (``apis.shared.costs.prepare_ab``) and its script shell."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from apis.shared.costs import prepare_ab as ab

T0 = 1_800_000_000.0
WINDOW = (T0 - 100, T0 + 100_000)


def prelude(sid, arm, total=1000, build=1000, ts=T0, resume=False, assistant=False, version="1.40.0", age=None):
    return {
        "sessionId": sid, "totalMs": total, "stages": {}, "groups": {"agent_build": build, "preamble": 50},
        "isResume": resume, "hasAssistant": assistant, "processBuilds": 1, "preparedAgeMs": age, "prepareArm": arm,
        "appVersion": version, "build": "img1", "_ts": ts,
    }


def first_token(sid, ms, ts, arm="treated"):
    return {"sessionId": sid, "firstTokenMs": ms, "preludeTotalMs": 1, "stages": {}, "prepareArm": arm, "_ts": ts}


def line(kind, payload, ts=T0):
    body = {k: v for k, v in payload.items() if k != "_ts"}
    return ts, f"[INFO] {kind} {json.dumps(body)}"


def otel(kind, payload, ts=T0):
    body = {k: v for k, v in payload.items() if k != "_ts"}
    return ts + 0.2, json.dumps({"resource": {"attributes": {}}, "body": f"{kind} {json.dumps(body)}", "severityText": "INFO"})


def client(ms, arm, kind="first", visible=True, model="m1", ts=T0):
    rec = {"ClientFirstTokenMs": ms, "turnKind": kind, "modelId": model, "visible": visible, "source": "spa",
           "appVersion": "1.40.0"}
    if arm:
        rec["prepareArm"] = arm
    return {**rec, "_ts": ts}


# ---------------------------------------------------------------------------
# Parsing and dedupe
# ---------------------------------------------------------------------------


def test_plain_and_envelope_copies_count_once():
    p = prelude("s1", "treated")
    parsed = ab.parse_runtime([line("turn_prelude", p), otel("turn_prelude", p)])
    assert len(parsed["prelude"]) == 1


def test_two_real_turns_with_the_same_payload_far_apart_both_count():
    p = prelude("s1", "treated")
    parsed = ab.parse_runtime([line("turn_prelude", p, T0), line("turn_prelude", p, T0 + 60)])
    assert len(parsed["prelude"]) == 2


def test_malformed_and_unrelated_lines_are_ignored():
    lines = [
        (T0, "turn_prelude {not json"),
        (T0, "turn_prelude [1,2]"),
        (T0, "something else {\"sessionId\": \"x\"}"),
        (T0, "{broken envelope"),
        (T0, json.dumps({"body": "turn_prepare {\"nosession\": 1}"})),
        line("turn_prepare", {"sessionId": "s", "prepared": True, "reason": "built", "cacheHit": False, "ms": 5}),
    ]
    parsed = ab.parse_runtime(lines)
    assert [len(parsed[k]) for k in ("prelude", "first_token", "prepare")] == [0, 0, 1]


def test_line_with_no_arm_parses_but_is_not_eligible():
    p = prelude("s1", None)
    parsed = ab.parse_runtime([line("turn_prelude", p)])
    assert len(parsed["prelude"]) == 1
    assert ab.eligible_preludes(parsed["prelude"]) == []


def test_resumes_are_not_eligible():
    turns = [prelude("a", "treated"), prelude("b", "holdout", resume=True)]
    assert [t["sessionId"] for t in ab.eligible_preludes(turns)] == ["a"]


def test_client_line_parsing():
    good = json.dumps({"ClientFirstTokenMs": 900, "prepareArm": "holdout", "source": "spa"})
    other = json.dumps({"ClientFirstTokenMs": 900, "source": "probe"})
    assert ab.parse_client([(T0, good), (T0, other), (T0, "nope"), (T0, '{"ClientFirstTokenMs": "x"}')])[0]["prepareArm"] == "holdout"
    assert len(ab.parse_client([(T0, good), (T0, other)])) == 1


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_nearest_rank_percentiles():
    values = list(range(1, 11))
    assert ab.percentile(values, 50) == 5
    assert ab.percentile(values, 90) == 9
    assert ab.percentile(values, 100) == 10
    assert ab.percentile([7], 90) == 7
    assert ab.percentile([], 50) is None
    assert ab.percentile([3, 1, 2], 50) == 2


def test_bootstrap_is_deterministic_under_a_seed_and_differs_across_seeds():
    t = [100 + i for i in range(60)]
    h = [200 + i for i in range(60)]
    a = ab.bootstrap_median_diff(t, h, seed=1)
    assert a == ab.bootstrap_median_diff(t, h, seed=1)
    assert a["point"] == -100 and not a["insideNoise"] and a["high"] < 0


def test_identical_distributions_are_inside_noise():
    same = [100, 110, 120, 130, 140] * 20
    diff = ab.bootstrap_median_diff(same, list(same), seed=3)
    assert diff["insideNoise"] and diff["low"] <= 0 <= diff["high"]


def test_compare_under_floor_prints_nothing_numeric():
    cmp = ab.compare([1.0] * 60, [2.0] * 10, min_n=50, seed=1)
    assert cmp["holdout"]["p50"] is None and cmp["holdout"]["p90"] is None
    assert cmp["diff"] is None and not cmp["available"]
    assert cmp["treated"]["p50"] == 1.0
    assert ab._ms(cmp["holdout"]["p50"]) == "—"


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------


def test_join_takes_nearest_later_first_token_once():
    turns = [prelude("s1", "treated", ts=T0), prelude("s1", "treated", ts=T0 + 100)]
    firsts = [first_token("s1", 500, T0 + 2), first_token("s1", 700, T0 + 103), first_token("s1", 999, T0 - 5)]
    joined, count = ab.join_first_tokens(turns, firsts)
    assert count == 2
    assert [t["firstTokenMs"] for t in joined] == [500, 700]


def test_join_ignores_a_first_token_past_the_next_prelude_and_other_sessions():
    turns = [prelude("s1", "treated", ts=T0), prelude("s1", "treated", ts=T0 + 10)]
    firsts = [first_token("s1", 800, T0 + 12), first_token("s2", 1, T0 + 1)]
    joined, count = ab.join_first_tokens(turns, firsts)
    assert count == 1
    assert joined[0]["firstTokenMs"] is None and joined[1]["firstTokenMs"] == 800


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def runtime_for(treated, holdout, *, t_total=500, h_total=1000, t_build=40, h_build=800, version="1.40.0"):
    preludes, firsts = [], []
    for i in range(treated):
        preludes.append(prelude(f"t{i}", "treated", total=t_total + i % 5, build=t_build, ts=T0 + i, version=version,
                                age=3000, assistant=i % 2 == 0))
        firsts.append(first_token(f"t{i}", t_total + 3000, T0 + i + 1))
    for i in range(holdout):
        preludes.append(prelude(f"h{i}", "holdout", total=h_total + i % 5, build=h_build, ts=T0 + i, version=version,
                                assistant=i % 2 == 0))
        firsts.append(first_token(f"h{i}", h_total + 3000, T0 + i + 1, arm="holdout"))
    return {"prelude": preludes, "first_token": firsts, "prepare": []}


def clients(treated, holdout, t_ms=2000, h_ms=3000):
    return ([client(t_ms + i % 7, "treated") for i in range(treated)]
            + [client(h_ms + i % 7, "holdout") for i in range(holdout)])


def report(rt, cl, **kw):
    return ab.build_report(runtime=rt, client=cl, window=WINDOW, holdout_percent=50, min_n=50, seed=1, **kw)


def fired(rep):
    return {c["id"] for c in rep["caveats"] if c["fired"]}


def test_clean_win_is_confirmed():
    rep = report(runtime_for(80, 80), clients(80, 80))
    assert fired(rep) == set()
    assert rep["verdict"] == "effect confirmed"
    assert rep["joined"] == {"joined": 160, "of": 160}
    assert rep["server"]["overall"]["totalMs"]["diff"]["point"] < 0
    assert rep["mechanism"]["works"] and rep["mechanism"]["treated"]["share"] == 1.0


def test_mixed_builds_blocks_confirmation():
    rt = runtime_for(80, 80)
    rt["prelude"][0]["appVersion"] = "1.41.0"
    rep = report(rt, clients(80, 80))
    assert "mixed_builds" in fired(rep)
    assert rep["verdict"] == "mechanism works, user-facing effect not shown"


def test_arm_imbalance_fires_beyond_three_sigma_only():
    assert "arm_imbalance" in fired(report(runtime_for(120, 60), clients(80, 80)))
    assert "arm_imbalance" not in fired(report(runtime_for(90, 70), clients(80, 80)))


def test_arm_under_floor_is_cannot_tell_and_prints_dash():
    rep = report(runtime_for(80, 20), clients(80, 80))
    assert "arm_under_floor" in fired(rep)
    assert rep["verdict"] == "cannot tell"
    text = ab.render_markdown(rep)
    assert "| totalMs | 80 | " in text and "—" in text
    assert "20 | — / —" in text


def test_server_win_without_client_win_is_mechanism_only():
    rep = report(runtime_for(80, 80), clients(80, 80, t_ms=3000, h_ms=3000))
    assert rep["verdict"] == "mechanism works, user-facing effect not shown"


def test_no_effect():
    rep = report(runtime_for(80, 80, t_total=1000, h_total=1000, t_build=800, h_build=800), clients(80, 80, 3000, 3000))
    assert rep["verdict"] == "no effect"


def test_missing_client_without_mechanism_is_cannot_tell():
    rep = report(runtime_for(80, 80, t_total=1000, h_total=1000, t_build=800, h_build=800), [])
    assert rep["verdict"] == "cannot tell"


def test_null_arm_share_and_hidden_clients():
    rt = runtime_for(80, 80)
    rt["prelude"] += [prelude(f"n{i}", None, ts=T0 + i) for i in range(400)]
    rep = report(rt, clients(80, 80) + [client(9999, "treated", visible=False)] * 30)
    assert "not_in_experiment" not in fired(rep)
    note = next(c for c in rep["caveats"] if c["id"] == "not_in_experiment")
    assert note["note"] and "71.4%" in note["text"] and "71.4%" in ab.render_markdown(rep)
    assert rep["client"]["overall"]["treated"]["n"] == 80
    assert report(rt, clients(80, 80) + [client(9999, "treated", visible=False)] * 30,
                  include_hidden=True)["client"]["overall"]["treated"]["n"] == 110


def test_mix_shift_caveats():
    rt = runtime_for(80, 80)
    for p in rt["prelude"]:
        p["hasAssistant"] = p["prepareArm"] == "treated"
    cl = [client(2000, "treated", kind="first", model="a") for _ in range(80)] + \
         [client(3000, "holdout", kind="followup", model="b") for _ in range(80)]
    got = fired(report(rt, cl))
    assert {"mix_shift_has_assistant", "mix_shift_turn_kind", "mix_shift_model"} <= got
    assert fired(report(runtime_for(80, 80), clients(80, 80))) == set()


def test_by_has_assistant_split_and_turn_kind_split():
    cl = [client(2000 + i, "treated", kind="first") for i in range(60)] + \
         [client(3000 + i, "holdout", kind="first") for i in range(60)]
    rep = report(runtime_for(120, 120), cl)
    assert rep["server"]["byHasAssistant"][True]["totalMs"]["treated"]["n"] == 60
    assert rep["client"]["byTurnKind"]["first"]["available"]


def test_resumes_excluded_from_server_comparison():
    rt = runtime_for(80, 80)
    rt["prelude"].append(prelude("r", "treated", resume=True, total=99999))
    rep = report(rt, clients(80, 80))
    assert rep["server"]["overall"]["totalMs"]["treated"]["n"] == 80


def test_prepares_summary_and_never_followed():
    end = T0 + 10_000
    prepares = [
        {"sessionId": "a", "prepared": True, "reason": "built", "cacheHit": False, "ms": 400, "_ts": T0},
        {"sessionId": "b", "prepared": True, "reason": "built", "cacheHit": True, "ms": 10, "_ts": T0},
        {"sessionId": "c", "prepared": False, "reason": "quota", "cacheHit": False, "ms": 5, "_ts": T0},
        {"sessionId": "late", "prepared": True, "reason": "built", "cacheHit": False, "ms": 5, "_ts": end - 60},
    ]
    preludes = [prelude("a", "treated", ts=T0 + 120), prelude("b", "treated", ts=T0 + 16 * 60)]
    out = ab.prepares_summary(prepares, preludes, window_end=end, min_n=1)
    assert out["byReason"] == {"not prepared/quota": 1, "prepared/built": 3}
    assert out["neverFollowed"] == {"judged": 3, "share": pytest.approx(2 / 3)}
    assert out["cacheHitShare"] == pytest.approx(1 / 3)
    assert ab.prepares_summary(prepares, preludes, window_end=end, min_n=50)["neverFollowed"]["share"] is None


def test_render_is_content_free_and_caveats_first():
    rep = report(runtime_for(80, 80), clients(80, 80))
    text = ab.render_markdown(rep)
    assert text.index("## Caveats") < text.index("## Server") < text.index("## Verdict")
    assert "sessionId" not in text and "h12" not in text
    assert "effect confirmed" in text
    json.dumps(rep, default=str)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def test_fetch_lines_bisects_a_full_page():
    calls = []

    def run(group, start, end, query):
        calls.append((start, end))
        if end - start > 60:
            return [(start, "x")] * ab.QUERY_LIMIT
        return [(start, "x")]

    out = ab.fetch_lines(run, "g", 0, 100, "q")
    assert len(calls) == 3 and len(out) == 2


class FakeLogs:
    def __init__(self, statuses):
        self.statuses = list(statuses)

    def start_query(self, **kw):
        self.kw = kw
        return {"queryId": "q"}

    def get_query_results(self, queryId):
        status = self.statuses.pop(0)
        rows = [[{"field": "@timestamp", "value": "2026-11-26 10:00:00.123"}, {"field": "@message", "value": "m"}]]
        return {"status": status, "results": rows if status == "Complete" else []}


def test_runner_parses_rows_and_fails_closed():
    run = ab.logs_insights_runner(FakeLogs(["Running", "Complete"]), sleep=lambda _: None)
    [(ts, msg)] = run("g", 0, 10, "q")
    assert msg == "m" and ts == pytest.approx(1795687200.123)
    with pytest.raises(RuntimeError):
        ab.logs_insights_runner(FakeLogs(["Failed"]), sleep=lambda _: None)("g", 0, 10, "q")


def test_parse_when():
    assert ab.parse_when("7d", now=1_000_000) == 1_000_000 - 7 * 86400
    assert ab.parse_when("2026-11-26", now=0) == 1795651200.0
    assert ab.parse_when("2026-11-26T01:00:00Z", now=0) == 1795654800.0


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------


def test_script_main_with_injected_session(capsys, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_ab_readout.py"
    spec = importlib.util.spec_from_file_location("prepare_ab_readout_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    rt = runtime_for(60, 60)
    lines = [line("turn_prelude", p, p["_ts"]) for p in rt["prelude"]] + [otel("turn_prelude", p, p["_ts"]) for p in rt["prelude"]]
    lines += [line("turn_first_token", f, f["_ts"]) for f in rt["first_token"]]

    def fake_runner(client, **kw):
        def run(group, start, end, query):
            return [(ts, text) for ts, text in lines] if group == "rt" else []
        return run

    monkeypatch.setattr(module.prepare_ab, "logs_insights_runner", fake_runner)

    class Session:
        def client(self, name):
            return object()

    code = module.main(
        ["--prefix", "p", "--since", "2026-11-01", "--until", "2027-06-01", "--runtime-log-group", "rt",
         "--app-log-group", "app", "--min-n", "50", "--json"],
        boto_session=Session(),
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["server"]["overall"]["totalMs"]["treated"]["n"] == 60
    assert out["joined"]["joined"] == 120


def test_ninety_percent_null_arm_still_confirms():
    rt = runtime_for(80, 80)
    rt["prelude"] += [prelude(f"n{i}", None, ts=T0 + i) for i in range(1440)]
    rep = report(rt, clients(80, 80))
    assert fired(rep) == set()
    assert "90.0%" in ab.render_markdown(rep)
    assert rep["verdict"] == "effect confirmed"
