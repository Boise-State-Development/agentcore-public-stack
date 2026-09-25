"""Fleet-level feedback attribution (response-feedback spec §7, compaction
spec §7.2): the arm math, and the route that assembles it.

The rules under test are §9's, because they are what stop this surface
becoming the quality KPI the spec forbids: every arm carries its n, an arm
below the floor reports no rate at all, coverage is reported rather than
implied, and no fleet-wide rate field exists.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.feedback import fleet, routes as feedback_routes
from apis.shared.observability.content_policy import content_bearing_paths
from tests.conftest import override_admin_auth

WINDOW = {"start": "2026-09-01T00:00:00Z", "end": "2026-09-18T00:00:00Z", "days": 17}


def _thumb(session_id, message_id, value, reason=None, signal="explicit"):
    row = {"sessionId": session_id, "messageId": message_id, "value": value, "signal": signal}
    if reason:
        row["reason"] = reason
    return row


def _call(message_id, *, model="haiku", switched=False, compaction=False, has_documents=None):
    row = {"messageId": message_id, "modelInfo": {"modelId": model}, "agentSwitched": switched}
    if compaction:
        row["compactionEvents"] = [{"kind": "applied"}]
    if has_documents is not None:
        row["hasDocuments"] = has_documents
    return row


# ── arm math ───────────────────────────────────────────────────────────────


def test_an_arm_below_the_floor_reports_no_rate_at_all():
    arm = fleet.ArmCounts(up=1, down=1)
    assert arm.to_dict(floor=20) == {"up": 1, "down": 1, "n": 2, "downRate": None, "belowFloor": True}
    big = fleet.ArmCounts(up=15, down=5)
    assert big.to_dict(floor=20) == {"up": 15, "down": 5, "n": 20, "downRate": 0.25, "belowFloor": False}


def test_the_floor_is_env_tunable_but_never_zero(monkeypatch):
    monkeypatch.delenv("FEEDBACK_ARM_MINIMUM_N", raising=False)
    assert fleet.minimum_n() == fleet.DEFAULT_MINIMUM_N
    monkeypatch.setenv("FEEDBACK_ARM_MINIMUM_N", "5")
    assert fleet.minimum_n() == 5
    monkeypatch.setenv("FEEDBACK_ARM_MINIMUM_N", "0")
    assert fleet.minimum_n() == 1
    monkeypatch.setenv("FEEDBACK_ARM_MINIMUM_N", "nonsense")
    assert fleet.minimum_n() == fleet.DEFAULT_MINIMUM_N


def test_distance_from_the_last_compaction_cut_buckets_by_calls():
    # positions:      0        1                  2      3      4      5
    records = [_call(0), _call(1, compaction=True), _call(2), _call(3), _call(4), _call(5)]
    index = fleet.index_session(records)
    assert fleet.calls_since_compaction(index, 0) == "never"   # before any cut
    assert fleet.calls_since_compaction(index, 1) == "same call"
    assert fleet.calls_since_compaction(index, 2) == "1-3 calls"
    assert fleet.calls_since_compaction(index, 4) == "1-3 calls"
    assert fleet.calls_since_compaction(index, 5) == "4+ calls"
    # A session that was never compacted is the control arm, not an omission.
    plain = fleet.index_session([_call(0), _call(1)])
    assert fleet.calls_since_compaction(plain, 1) == "never"
    # An unknown message cannot be placed.
    assert fleet.calls_since_compaction(index, 99) == "never"


def test_only_the_applied_cut_starts_the_distance_not_the_decision():
    # The cut is decided on call 1 (its `checkpoint` rides the call whose input
    # triggered it, answered on the full history) and first reaches the model
    # on call 2 (`applied`). Other ledger kinds never mark a cut.
    decided = _call(1)
    decided["compactionEvents"] = [{"kind": "checkpoint"}, {"kind": "forced"}, {"kind": "floor_unreachable"}]
    other = _call(3)
    other["compactionEvents"] = [{"kind": "truncation_anchor"}, {"kind": "document_offload"}]
    index = fleet.index_session([_call(0), decided, _call(2, compaction=True), other])
    assert fleet.calls_since_compaction(index, 1) == "never"
    assert fleet.calls_since_compaction(index, 2) == "same call"
    assert fleet.calls_since_compaction(index, 3) == "1-3 calls"


def test_a_turns_tool_round_trips_share_a_message_id_and_the_last_row_wins():
    records = [_call(3, model="haiku"), _call(3, model="sonnet", compaction=True)]
    index = fleet.index_session(records)
    assert index.by_message[3]["modelInfo"]["modelId"] == "sonnet"
    assert index.position[3] == 1


def test_report_splits_every_dimension_and_counts_what_it_could_not_join():
    thumbs = [
        _thumb("s1", 0, -1, "wrong"),
        _thumb("s1", 1, 1),
        _thumb("s1", 2, -1, "instructions"),
        _thumb("s2", 0, -1, "wrong"),
        _thumb("s9", 0, -1),           # session never joined
        _thumb("s1", 77, 1),           # no cost row for that message
        _thumb("s1", 3, -1, signal="implicit"),  # §10 row: never a thumb
    ]
    records = {
        "s1": [_call(0, model="haiku"), _call(1, model="haiku", compaction=True), _call(2, model="sonnet", switched=True)],
        "s2": [_call(0, model="sonnet", has_documents=True)],
    }
    report = fleet.build_fleet_report(thumbs, records, window=WINDOW, floor=1)

    assert report["totals"] == {"up": 2, "down": 4, "thumbs": 6}
    assert report["coverage"]["joined"] == 4
    assert report["coverage"]["unjoined"] == 2
    assert report["coverage"]["joinRate"] == round(4 / 6, 4)
    assert report["coverage"]["sessionsWithFeedback"] == 3
    assert report["coverage"]["sessionsJoined"] == 2

    by_key = {a["key"]: a for a in report["arms"]["model"]}
    assert by_key["haiku"]["n"] == 2 and by_key["haiku"]["down"] == 1
    assert by_key["sonnet"]["n"] == 2 and by_key["sonnet"]["down"] == 2
    switch = {a["key"]: a for a in report["arms"]["agentSwitch"]}
    assert switch["switched agent"]["down"] == 1 and switch["same agent"]["n"] == 3
    turn = {a["key"]: a for a in report["arms"]["turnClass"]}
    assert turn["full"]["down"] == 1
    assert report["reasons"] == {"wrong": 2, "instructions": 1, "unspecified": 1}
    assert report["minimumN"] == 1


def test_no_fleet_wide_rate_field_exists_to_be_quoted():
    report = fleet.build_fleet_report([_thumb("s1", 0, -1)], {}, window=WINDOW, floor=1)
    assert "downRate" not in report
    assert "downRate" not in report["totals"]
    assert "score" not in repr(report)


def test_compaction_arm_keeps_distance_order_others_lead_with_the_biggest_sample():
    thumbs = [_thumb("s1", i, -1) for i in range(6)] + [_thumb("s1", 6, 1)]
    records = {"s1": [_call(0, compaction=True), _call(1), _call(2), _call(3), _call(4), _call(5), _call(6, model="zzz")]}
    report = fleet.build_fleet_report(thumbs, records, window=WINDOW, floor=1)
    assert [a["key"] for a in report["arms"]["callsSinceCompaction"]] == ["same call", "1-3 calls", "4+ calls"]
    assert [a["key"] for a in report["arms"]["model"]][0] == "haiku"  # 6 thumbs beats zzz's 1


def test_an_empty_window_is_zeroes_not_a_crash():
    report = fleet.build_fleet_report([], {}, window=WINDOW)
    assert report["totals"] == {"up": 0, "down": 0, "thumbs": 0}
    assert report["coverage"]["joinRate"] is None
    assert all(arm == [] for arm in report["arms"].values())


# ── route ──────────────────────────────────────────────────────────────────


def _app(storage):
    app = FastAPI()
    app.include_router(feedback_routes.router)
    override_admin_auth(app, lambda: SimpleNamespace(user_id="admin", email="a@x", roles=["system_admin"]))
    app.dependency_overrides[feedback_routes.get_storage] = lambda: storage
    app.dependency_overrides[feedback_routes.get_judge] = lambda: SimpleNamespace(judge=lambda *a, **k: [])
    return app


def test_fleet_route_windows_joins_and_stays_content_free():
    thumbs = [_thumb("s1", 0, -1, "wrong"), _thumb("s1", 1, 1), _thumb("s2", 0, -1, "length")]
    storage = SimpleNamespace(
        get_feedback_in_window=AsyncMock(return_value=thumbs),
        get_session_cost_records=AsyncMock(side_effect=lambda sid: {
            "s1": [_call(0, model="haiku"), _call(1, model="haiku", compaction=True)],
            "s2": [_call(0, model="sonnet")],
        }.get(sid, [])),
    )
    resp = TestClient(_app(storage)).get("/feedback/fleet", params={"days": 7})

    assert resp.status_code == 200
    body = resp.json()
    assert body["window"]["days"] == 7
    assert body["totals"] == {"up": 1, "down": 2, "thumbs": 3}
    assert body["coverage"]["joined"] == 3 and body["coverage"]["truncated"] is False
    assert {a["key"] for a in body["arms"]["model"]} == {"haiku", "sonnet"}
    # Below the default floor of 20, every arm reports counts but no rate.
    assert all(a["downRate"] is None and a["belowFloor"] for a in body["arms"]["model"])
    assert body["minimumN"] == 20
    assert content_bearing_paths(body) == []
    # The window passed to storage is the one reported back.
    kwargs = storage.get_feedback_in_window.await_args.kwargs
    assert kwargs["start"] == body["window"]["start"] and kwargs["end"] == body["window"]["end"]


def test_fleet_route_reports_truncation_and_omitted_sessions(monkeypatch):
    monkeypatch.setattr(feedback_routes, "MAX_THUMBS_PER_WINDOW", 3)
    monkeypatch.setattr(feedback_routes, "MAX_SESSIONS_JOINED", 2)
    thumbs = [_thumb(f"s{i}", 0, -1) for i in range(3)]
    storage = SimpleNamespace(
        get_feedback_in_window=AsyncMock(return_value=thumbs),
        get_session_cost_records=AsyncMock(return_value=[_call(0)]),
    )
    body = TestClient(_app(storage)).get("/feedback/fleet").json()
    assert body["coverage"]["truncated"] is True
    assert body["coverage"]["sessionsOmitted"] == 1
    assert body["coverage"]["sessionsJoined"] == 2
    assert body["coverage"]["unjoined"] == 1


def test_a_session_whose_rows_fail_to_read_degrades_to_unjoined():
    storage = SimpleNamespace(
        get_feedback_in_window=AsyncMock(return_value=[_thumb("s1", 0, -1), _thumb("s2", 0, -1)]),
        get_session_cost_records=AsyncMock(side_effect=lambda sid: (_ for _ in ()).throw(RuntimeError("boom"))
                                           if sid == "s2" else [_call(0)]),
    )
    body = TestClient(_app(storage)).get("/feedback/fleet").json()
    assert body["coverage"]["joined"] == 1 and body["coverage"]["unjoined"] == 1


def test_fleet_route_bounds_the_window_and_maps_failure_to_500():
    storage = SimpleNamespace(get_feedback_in_window=AsyncMock(return_value=[]), get_session_cost_records=AsyncMock(return_value=[]))
    client = TestClient(_app(storage))
    assert client.get("/feedback/fleet", params={"days": 0}).status_code == 422
    assert client.get("/feedback/fleet", params={"days": 400}).status_code == 422

    storage.get_feedback_in_window = AsyncMock(side_effect=RuntimeError("dynamo down"))
    assert TestClient(_app(storage)).get("/feedback/fleet").status_code == 500
