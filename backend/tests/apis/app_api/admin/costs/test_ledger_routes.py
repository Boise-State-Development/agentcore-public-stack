"""Release-ledger admin API (docs/specs/release-ledger.md §6.1, PR-4).

Pinned here:
- every route is ``admin.costs``, checked before the flag: 401 unauthenticated
  and 403 without the scope whichever way the flag is set, 404 for an admin
  while ``RELEASE_LEDGER_ENABLED`` is off;
- each route reads only the ledger store it is handed (no ``C#`` reads) and
  returns the documented shape;
- every response is content-free: no content-bearing path and no identity
  key (session, user, message, tool use) anywhere in the JSON;
- export round-trips: summing its ``LEDGER#VERSION`` lines gives the versions
  route's totals for the same range.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.app_api.admin.costs import ledger_routes
from apis.shared.costs import ledger_latency
from apis.shared.observability.content_policy import content_bearing_paths
from tests.conftest import override_admin_auth

#: Keys that would identify a person or a conversation. None may appear in
#: any ledger response (D7), even though other admin cost views carry some.
IDENTITY_KEYS = {"sessionId", "userId", "messageId", "toolUseId", "email", "uploadId", "quote", "arguments"}


def _keys(value: Any) -> set:
    found = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            found.add(key)
            found |= _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            found |= _keys(inner)
    return found


def _assert_content_free(body: Any) -> None:
    assert content_bearing_paths(body) == []
    assert not (_keys(body) & IDENTITY_KEYS), _keys(body) & IDENTITY_KEYS


def _v(day: str, version: str, build: str, **counters: Any) -> Dict[str, Any]:
    row = {"PK": "LEDGER#VERSION", "SK": f"{day}#{version}#{build}", "date": day,
           "appVersion": version, "build": build}
    row.update({k: v if isinstance(v, str) and "T" in v else Decimal(str(v)) for k, v in counters.items()})
    return row


def _family(family: str, label: str, day: str, version: str, build: str, key: str, **counters: Any) -> Dict[str, Any]:
    row = {"PK": f"LEDGER#{family}#{day[:7]}", "SK": f"{day}#{version}#{build}#{key}", "date": day,
           "appVersion": version, "build": build, label: key}
    row.update({k: Decimal(str(v)) for k, v in counters.items()})
    return row


BASE = dict(calls=40, turns=30, cost="0.30", outcomeTurns=30, completedTurns=29, stoppedTurns=1,
            cacheReadTokens=9000, cacheWriteTokens=1000, toolCalls=25, toolErrors=1, firstTokenMsSum=40000,
            firstTokenCount=40)
CAND = dict(calls=44, turns=30, cost="0.36", outcomeTurns=30, completedTurns=28, emptyAnswerTurns=2,
            cacheReadTokens=8000, cacheWriteTokens=2000, toolCalls=25, toolErrors=3, estimatedCalls=1,
            firstTokenMsSum=48000, firstTokenCount=44)

VERSION_ROWS = [
    _v("2026-10-01", "1.32.0", "aaaa", **BASE),
    _v("2026-10-02", "1.32.0", "aaaa", **BASE),
    # Deploy day: both versions wrote rows, so it is nobody's full day.
    _v("2026-10-03", "1.32.0", "aaaa", **BASE),
    _v("2026-10-03", "1.33.0", "bbbb", **CAND),
    _v("2026-10-04", "1.33.0", "bbbb", **CAND),
    _v("2026-10-05", "1.33.0", "bbbb", **CAND, firstLiveAt="2026-10-05T01:00:00+00:00"),
]
MODEL_ROWS = [
    _family("MODEL", "modelId", r["date"], r["appVersion"], r["build"], "us.anthropic.claude-haiku-5-5",
            calls=int(r["calls"]), turns=int(r["turns"]), cost=r["cost"])
    for r in VERSION_ROWS
]
TOOL_ROWS = [
    _family("TOOL", "toolId", "2026-10-02", "1.32.0", "aaaa", "browse_web", calls=25, errors=1,
            resultChars=150000, timedCalls=25, durationMsSum=50000, estInputUsd="0.04"),
    _family("TOOL", "toolId", "2026-10-04", "1.33.0", "bbbb", "browse_web", calls=25, errors=3,
            resultChars=175000, timedCalls=25, durationMsSum=75000, estInputUsd="0.05"),
    _family("TOOL", "toolId", "2026-10-04", "1.33.0", "bbbb", "legacy_tool", calls=4),
]
AGENT_ROWS = [_family("AGENT", "agentId", "2026-10-04", "1.33.0", "bbbb", "agent-1", calls=5, turns=4, cost="0.1")]
PROJECT_ROWS = [_family("PROJECT", "projectId", "2026-10-04", "1.33.0", "bbbb", "proj-1", calls=3, turns=2, cost="0.05")]
DEPLOY = [{"PK": "LEDGER#DEPLOY", "SK": "app-api#bbbb", "service": "app-api", "appVersion": "1.33.0",
           "build": "bbbb", "firstSeenAt": "2026-10-03T12:00:00+00:00"}]
EVAL = [{"PK": "LEDGER#EVAL", "SK": "2026-10-04T02:00:00+00:00", "appVersion": "1.33.0", "build": "bbbb",
         "families": {"factual": {"n": 20, "base": 18, "run": 17, "gate": "pass"}}, "costUsd": Decimal("0.4")}]


class FakeStore:
    def __init__(self) -> None:
        self.families = {"MODEL": MODEL_ROWS, "TOOL": TOOL_ROWS, "AGENT": AGENT_ROWS, "PROJECT": PROJECT_ROWS}

    @staticmethod
    def _in(rows: List[Dict[str, Any]], start: str, end: str) -> List[Dict[str, Any]]:
        return [r for r in rows if start <= r["date"] <= end]

    def version_rows(self, start, end):
        return self._in(VERSION_ROWS, start, end)

    def family_rows(self, family, start, end):
        return self._in(self.families[family], start, end)

    def deploy_markers(self):
        return DEPLOY

    def backfill_markers(self):
        return [{"PK": "LEDGER#BACKFILL", "SK": "a#b", "from": "2026-09-01T00:00:00+00:00",
                 "to": "2026-10-05T00:00:00+00:00", "rows": Decimal(10), "ledgerRows": Decimal(4),
                 "status": "complete"}]

    def eval_rows(self, start, end):
        return [r for r in EVAL if start <= r["SK"][:10] <= end]

    def smoke_rows(self, start, end):
        return []

    def daily_rollups(self, start, end):
        return [{"PK": "ROLLUP#DAILY", "SK": d, "activeUsers": Decimal(5)} for d in
                ("2026-10-01", "2026-10-02", "2026-10-04", "2026-10-05")]


def _storage() -> SimpleNamespace:
    # A thumb on a stamped candidate row; nothing for provenance (the ledger
    # shows no verify_sources calls, so the join is skipped without a read).
    return SimpleNamespace(
        get_feedback_in_window=AsyncMock(return_value=[
            {"sessionId": "s1", "messageId": 2, "value": -1, "signal": "explicit", "retryMessageId": 3},
            {"sessionId": "s1", "messageId": 2, "value": 1, "signal": "explicit"},
        ]),
        get_session_cost_records=AsyncMock(return_value=[
            {"sessionId": "s1", "messageId": 2, "timestamp": "2026-10-04T10:00:00+00:00",
             "appVersion": "1.33.0", "build": "bbbb"},
        ]),
        get_top_users_by_cost=AsyncMock(return_value=[]),
    )


def _app(*, authorize: bool = True, deny: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(ledger_routes.router)
    if authorize:
        def impl():
            if deny:
                raise HTTPException(status_code=403, detail="Access denied.")
            return SimpleNamespace(user_id="admin", email="a@x", roles=["system_admin"])

        override_admin_auth(app, impl)
    app.dependency_overrides[ledger_routes.get_ledger_store] = FakeStore
    app.dependency_overrides[ledger_routes.get_storage] = _storage
    return app


ROUTES = [
    "/costs/ledger/versions", "/costs/ledger/series", "/costs/ledger/tools", "/costs/ledger/agents",
    "/costs/ledger/projects", "/costs/ledger/quality", "/costs/ledger/readout", "/costs/ledger/export",
]


@pytest.fixture(autouse=True)
def _flag_and_clock(monkeypatch):
    monkeypatch.setenv("RELEASE_LEDGER_ENABLED", "true")
    monkeypatch.setattr(ledger_routes, "today_utc", lambda: __import__("datetime").date(2026, 10, 6))
    ledger_latency.clear_cache()
    monkeypatch.setattr(
        ledger_latency, "fetch_window_latency",
        lambda window, client=None: {"available": True, "metrics": {
            "serverFirstTokenMs": {"p50": 1800.0, "p90": 4000.0, "n": 120}}},
    )
    monkeypatch.setattr(ledger_routes, "_platform", AsyncMock(return_value=None))
    yield
    ledger_latency.clear_cache()


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("flag", ["true", "false"])
def test_auth_is_checked_before_the_flag(monkeypatch, path, flag):
    monkeypatch.setenv("RELEASE_LEDGER_ENABLED", flag)
    assert TestClient(_app(authorize=False)).get(path).status_code == 401
    assert TestClient(_app(deny=True)).get(path).status_code == 403


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("flag", ["", "false", "TRUE-ish"])
def test_every_route_404s_while_the_flag_is_off(monkeypatch, path, flag):
    monkeypatch.setenv("RELEASE_LEDGER_ENABLED", flag)
    assert TestClient(_app()).get(path).status_code == 404


def _get(path: str, **params: Any) -> Dict[str, Any]:
    resp = TestClient(_app()).get(path, params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_content_free(body)
    return body


def test_versions_lists_each_build_with_markers_and_kpis():
    body = _get("/costs/ledger/versions", **{"from": "2026-10-01", "to": "2026-10-05"})
    assert [v["appVersion"] for v in body["versions"]] == ["1.32.0", "1.33.0"]
    cand = next(b for b in body["builds"] if b["build"] == "bbbb")
    assert cand["firstDate"] == "2026-10-03" and cand["daysObserved"] == 3
    assert cand["firstLiveAt"] == "2026-10-05T01:00:00+00:00"
    assert cand["deployMarkers"] == [{"service": "app-api", "firstSeenAt": "2026-10-03T12:00:00+00:00"}]
    assert cand["totals"]["calls"] == 132 and cand["kpis"]["costPerTurn"]["n"] == 90
    assert body["backfills"][0]["status"] == "complete" and body["backfills"][0]["rows"] == 10


def test_a_bad_range_is_400():
    client = TestClient(_app())
    assert client.get("/costs/ledger/versions", params={"from": "2026-10-05", "to": "2026-10-01"}).status_code == 400
    assert client.get("/costs/ledger/versions", params={"from": "2024-01-01", "to": "2026-10-01"}).status_code == 400
    assert client.get("/costs/ledger/versions", params={"from": "10/01/2026"}).status_code == 422
    assert client.get("/costs/ledger/tools", params={"version": "1.33.0 drop table"}).status_code == 400


def test_series_returns_daily_points_and_version_bands():
    body = _get("/costs/ledger/series", **{"from": "2026-10-01", "to": "2026-10-05", "by": "model"})
    assert body["series"][0]["key"] == "us.anthropic.claude-haiku-5-5"
    assert [p["date"] for p in body["series"][0]["points"]] == [f"2026-10-0{d}" for d in range(1, 6)]
    assert [(b["key"], b["from"], b["to"]) for b in body["bands"]] == [
        ("1.32.0", "2026-10-01", "2026-10-03"), ("1.33.0", "2026-10-03", "2026-10-05")]
    by_build = _get("/costs/ledger/series", **{"from": "2026-10-01", "to": "2026-10-05", "unit": "build"})
    assert {s["key"] for s in by_build["series"]} == {"1.32.0#aaaa", "1.33.0#bbbb"}


def test_tools_table_marks_unmeasured_payload_and_filters_by_version():
    body = _get("/costs/ledger/tools", **{"from": "2026-10-01", "to": "2026-10-05", "version": "1.33.0#bbbb"})
    assert body["version"] == "1.33.0#bbbb"
    tools = {t["toolId"]: t for t in body["tools"]}
    assert set(tools) == {"browse_web", "legacy_tool"}
    assert tools["browse_web"]["payloadMeasured"] is True
    assert tools["browse_web"]["kpis"]["estInputTokens"]["value"] == 175000 / 4
    assert tools["legacy_tool"]["payloadMeasured"] is False
    assert body["notMeasured"] == ["awaitingUser", "callsWithUi"]
    assert body["tools"][0]["toolId"] == "browse_web"  # sorted by estimated input dollars


def test_agents_and_projects_tables():
    agents = _get("/costs/ledger/agents", **{"from": "2026-10-01", "to": "2026-10-05"})
    assert agents["kind"] == "agents" and agents["rows"][0]["id"] == "agent-1"
    assert agents["rows"][0]["kpis"]["costPerTurn"]["value"] == pytest.approx(0.025)
    projects = _get("/costs/ledger/projects", **{"from": "2026-10-01", "to": "2026-10-05"})
    assert projects["rows"][0]["id"] == "proj-1"


def test_quality_reports_counts_below_the_floor_never_a_rate():
    body = _get("/costs/ledger/quality", **{"from": "2026-10-01", "to": "2026-10-05"})
    cand = next(u for u in body["units"] if u["key"] == "1.33.0")
    assert cand["thumbs"] == {"up": 1, "down": 1, "n": 2, "downRate": None, "belowFloor": True, "retries": 1}
    assert cand["eval"][0]["families"]["factual"]["gate"] == "pass"
    assert cand["outcomes"]["emptyAnswer"] == 6
    assert cand["kpis"]["emptyAnswerShare"]["value"] == pytest.approx(6 / 90)
    assert body["provenanceCoverage"]["skipped"] == "no_verify_sources_calls"


def test_readout_compares_equal_windows_and_generates_caveats():
    body = _get("/costs/ledger/readout", days=7)
    assert body["unit"] == "version"
    # The deploy day (10-03) and today (10-06) are nobody's full day.
    assert body["candidate"]["key"] == "1.33.0" and body["candidate"]["window"] == ["2026-10-04", "2026-10-05"]
    assert body["baseline"]["key"] == "1.32.0" and body["baseline"]["window"] == ["2026-10-01", "2026-10-02"]
    cpt = body["kpis"]["costPerTurn"]
    assert cpt["baseline"] == pytest.approx(0.01) and cpt["candidate"] == pytest.approx(0.012)
    assert cpt["delta"] == pytest.approx(0.2) and cpt["n"] == [60, 60]
    assert set(cpt["byModel"]) == {"us.anthropic.claude-haiku-5-5"}
    assert body["latency"]["serverFirstTokenMs"]["p50"] == [1800.0, 1800.0]
    assert body["tools"][0]["toolId"] == "browse_web" and body["tools"][0]["calls"] == [25, 25]
    assert body["quality"]["thumbs"]["n"] == [0, 2] and body["quality"]["thumbs"]["downRate"] == [None, None]
    assert body["quality"]["eval"]["candidate"][0]["build"] == "bbbb"
    codes = [c["code"] for c in body["caveats"]]
    assert "short_window" in codes and "thumbs_below_floor" in codes and "estimated_rows" in codes
    assert "payload_not_measured" in codes


def test_readout_between_two_builds_and_unknown_ones():
    body = _get("/costs/ledger/readout", version="1.33.0#bbbb", baseline="1.32.0#aaaa")
    assert body["unit"] == "build" and body["candidate"]["build"] == "bbbb"
    client = TestClient(_app())
    assert client.get("/costs/ledger/readout", params={"version": "9.9.9"}).status_code == 404
    assert client.get("/costs/ledger/readout", params={"version": "1.33.0#bbbb", "baseline": "1.32.0"}).status_code == 400


def test_export_round_trips_the_versions_totals():
    client = TestClient(_app())
    resp = client.get("/costs/ledger/export", params={"from": "2026-10-01", "to": "2026-10-05"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/x-ndjson")
    rows = [json.loads(line) for line in resp.text.splitlines()]
    for row in rows:
        _assert_content_free(row)
    versions = [r for r in rows if r["PK"] == "LEDGER#VERSION"]
    assert len(versions) == len(VERSION_ROWS)
    exported_cost = sum(Decimal(str(r["cost"])) for r in versions)
    listed = _get("/costs/ledger/versions", **{"from": "2026-10-01", "to": "2026-10-05"})
    assert float(exported_cost) == pytest.approx(sum(v["totals"]["cost"] for v in listed["versions"]))
    assert {r["PK"].split("#")[1] for r in rows} == {"VERSION", "MODEL", "TOOL", "AGENT", "PROJECT", "EVAL", "DEPLOY", "BACKFILL"}
