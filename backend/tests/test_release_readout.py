"""scripts/release_readout.py (docs/specs/release-ledger.md §6.5)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import date

import pytest

from apis.shared.costs import ledger_readout
from apis.shared.costs.ledger_reader import Unit

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "release_readout.py"
_spec = importlib.util.spec_from_file_location("release_readout", SCRIPT)
readout = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = readout
_spec.loader.exec_module(readout)

TODAY = date(2026, 10, 10)
AGENT_ID = "agent-7f3e9c1d"
PROJECT_ID = "proj-a1b2c3d4"
OLD, NEW = ("1.0.0", "aaaaaaaaaaaaaaaa"), ("1.1.0", "bbbbbbbbbbbbbbbb")


def _version_row(day: str, unit, **counters):
    v, b = unit
    base = {"calls": 40, "turns": 25, "cost": 0.5, "outcomeTurns": 25, "completedTurns": 25,
            "cacheReadTokens": 9000, "cacheWriteTokens": 1000, "toolCalls": 30, "toolErrors": 1}
    base.update(counters)
    return {"PK": "LEDGER#VERSION", "SK": f"{day}#{v}#{b}", "date": day, "appVersion": v, "build": b, **base}


def _family_row(family: str, label: str, key: str, day: str, unit, **counters):
    v, b = unit
    return {"PK": f"LEDGER#{family}#{day[:7]}", "SK": f"{day}#{v}#{b}#{key}", "date": day,
            "appVersion": v, "build": b, label: key, **counters}


def _rows():
    rows = []
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        rows.append(_version_row(day, OLD))
        rows.append(_family_row("MODEL", "modelId", "model-a", day, OLD, calls=40, turns=25, cost=0.5))
        rows.append(_family_row("TOOL", "toolId", "calculator", day, OLD, calls=30, errors=1, resultChars=900, timedCalls=30, durationMsSum=300))
        rows.append(_family_row("AGENT", "agentId", AGENT_ID, day, OLD, calls=4, turns=2, cost=0.1))
    for day in ("2026-10-04", "2026-10-05", "2026-10-06"):
        rows.append(_version_row(day, NEW, cost=0.8, emptyAnswerTurns=3, toolErrors=6))
        rows.append(_family_row("MODEL", "modelId", "model-b", day, NEW, calls=40, turns=25, cost=0.8))
        rows.append(_family_row("TOOL", "toolId", "calculator", day, NEW, calls=30, errors=6, resultChars=900, timedCalls=30, durationMsSum=300))
        rows.append(_family_row("PROJECT", "projectId", PROJECT_ID, day, NEW, calls=4, turns=2, cost=0.2))
    rows.append({"PK": "LEDGER#DEPLOY", "SK": f"app-api#{NEW[1]}", "service": "app-api", "appVersion": NEW[0],
                 "build": NEW[1], "firstSeenAt": "2026-10-04T00:01:00Z"})
    return rows


def _bundle(**kwargs):
    return readout.readout_from_export(_rows(), version=kwargs.get("version", "1.1.0"),
                                       baseline=kwargs.get("baseline", "1.0.0"), days=7, today=TODAY)


def test_export_readout_is_the_shared_readout_over_the_same_rows():
    """The offline path adds no computation: it is select_windows + build_readout over the exported rows."""
    rows = _rows()
    version_rows = [r for r in rows if r["PK"] == "LEDGER#VERSION"]
    selection = ledger_readout.select_windows(
        version_rows, candidate=Unit.parse("1.1.0"), baseline=Unit.parse("1.0.0"), days=7, today=TODAY,
        markers=[r for r in rows if r["PK"] == "LEDGER#DEPLOY"],
    )
    expected = ledger_readout.build_readout(
        selection, version_rows=version_rows,
        model_rows=[r for r in rows if r["PK"].startswith("LEDGER#MODEL#")],
        tool_rows=[r for r in rows if r["PK"].startswith("LEDGER#TOOL#")],
        agent_rows=[r for r in rows if r["PK"].startswith("LEDGER#AGENT#")],
        project_rows=[r for r in rows if r["PK"].startswith("LEDGER#PROJECT#")],
    )
    bundle = _bundle()
    assert bundle["kpis"]["costPerTurn"]["baseline"] == pytest.approx(expected["kpis"]["costPerTurn"]["baseline"])
    assert bundle["kpis"]["costPerTurn"]["candidate"] == pytest.approx(expected["kpis"]["costPerTurn"]["candidate"])
    assert [c["code"] for c in bundle["caveats"]] == [c["code"] for c in expected["caveats"]]
    assert bundle["candidate"]["window"] == ["2026-10-04", "2026-10-06"]
    assert bundle["baseline"]["window"] == ["2026-10-01", "2026-10-03"]


def test_export_store_respects_ranges_and_months():
    store = readout.ExportStore(_rows())
    assert {r["date"] for r in store.version_rows("2026-10-02", "2026-10-04")} == {"2026-10-02", "2026-10-03", "2026-10-04"}
    assert len(store.family_rows("TOOL", "2026-09-01", "2026-10-31")) == 6
    with pytest.raises(ValueError):
        store.family_rows("NOPE", "2026-10-01", "2026-10-02")


def test_markdown_puts_caveats_before_any_number():
    bundle = _bundle()
    md = readout.render_markdown(bundle, source="test", generated_at="2026-10-10T00:00Z")
    assert md.index("Caveats") < md.index("**KPIs**")
    assert md.index("Mix shift") < md.index("**KPIs**")
    for item in bundle["caveats"]:
        assert f"`{item['code']}`" in md


def test_markdown_leaves_agent_and_project_ids_out_unless_asked():
    bundle = _bundle()
    md = readout.render_markdown(bundle, source="test")
    assert AGENT_ID not in md and PROJECT_ID not in md
    assert "agent 1" in md and "project 1" in md
    local = readout.render_markdown(bundle, source="test", show_ids=True)
    assert AGENT_ID in local and PROJECT_ID in local


def test_a_value_under_its_floor_prints_as_a_dash_never_zero():
    bundle = _bundle()
    # 3 compaction events at most: forcedCompactionRate has no denominator at all.
    assert bundle["kpis"]["forcedCompactionRate"]["candidate"] is None
    md = readout.render_markdown(bundle, source="test")
    row = next(line for line in md.splitlines() if line.startswith("| forcedCompactionRate |"))
    assert "0.0%" not in row and readout.DASH in row


def test_moved_reads_the_bundle_delta_and_names_the_direction():
    bundle = _bundle()
    movers = {m["kpi"]: m for m in readout.moved(bundle)}
    assert movers["costPerTurn"]["direction"] == "worse"  # 0.02 → 0.032
    assert movers["toolErrorRate"]["direction"] == "worse"
    assert movers["costPerTurn"]["delta"] == pytest.approx(bundle["kpis"]["costPerTurn"]["delta"])
    assert "forcedCompactionRate" not in movers  # no delta under the floor
    assert "documentTurnShare" not in movers or movers["documentTurnShare"]["direction"] == "shifted"


def test_main_writes_json_and_markdown_from_an_export(tmp_path, capsys):
    export = tmp_path / "ledger.ndjson"
    export.write_text("".join(json.dumps(r) + "\n" for r in _rows()))
    out = tmp_path / "out"
    assert readout.main(["--export", str(export), "--version", "1.1.0", "--baseline", "1.0.0",
                         "--today", TODAY.isoformat(), "--out", str(out)]) == 0
    saved = json.loads((out / "readout.json").read_text())
    assert saved["candidate"]["key"] == "1.1.0"
    md = (out / "readout.md").read_text()
    assert "Not in this source: CloudWatch latency" in md
    assert md == capsys.readouterr().out


def test_a_saved_readout_renders_the_same(tmp_path, capsys):
    saved = tmp_path / "readout.json"
    saved.write_text(json.dumps(_bundle()))
    assert readout.main(["--readout-json", str(saved)]) == 0
    assert "### Release readout: 1.1.0 vs 1.0.0" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["--export", "a", "--readout-json", "b"], ["--profile", "dev"]])
def test_exactly_one_usable_source(argv):
    with pytest.raises(SystemExit):
        readout.main(argv)


def test_unknown_unit_is_refused_not_guessed(tmp_path):
    export = tmp_path / "ledger.ndjson"
    export.write_text("".join(json.dumps(r) + "\n" for r in _rows()))
    with pytest.raises(SystemExit, match="readout refused"):
        readout.main(["--export", str(export), "--version", "9.9.9", "--today", TODAY.isoformat()])


def test_http_source_needs_a_session(monkeypatch):
    monkeypatch.delenv(readout.COOKIE_ENV, raising=False)
    with pytest.raises(SystemExit, match=readout.COOKIE_ENV):
        readout.readout_from_http(base_url="https://example.invalid/api", version=None, baseline=None, days=7)
