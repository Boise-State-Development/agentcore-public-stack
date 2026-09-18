"""Routes for the content-free drill-down.

- GET /admin/costs/users/{user_id}/sessions  → 200, query params reach the service
- GET /admin/costs/sessions/{session_id}/profile → 200 / 404 / 500 mapping

Auth is satisfied via `override_admin_auth` (the scope closure by qualname);
the service is swapped through the router's own `get_cost_service` dependency.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.costs import routes as cost_routes
from apis.app_api.admin.costs.models import (
    AttachmentProfile,
    SessionProfile,
    UserSessionSummary,
    UserSessionsResponse,
)
from tests.conftest import override_admin_auth


def _app(service):
    app = FastAPI()
    app.include_router(cost_routes.router)
    override_admin_auth(app, lambda: SimpleNamespace(user_id="admin", email="a@x", roles=["system_admin"]))
    app.dependency_overrides[cost_routes.get_cost_service] = lambda: service
    return app


def _summary(session_id="s1"):
    return UserSessionSummary(session_id=session_id, cost_known=True, total_cost=0.5)


def _profile():
    return SessionProfile(
        session_id="s1",
        user_id="u1",
        session=_summary(),
        call_count=0,
        compaction_threshold=100_000,
        attachments=AttachmentProfile(),
    )


def test_user_sessions_returns_200_and_forwards_query_params():
    service = SimpleNamespace(get_user_sessions=AsyncMock(return_value=UserSessionsResponse(
        user_id="u1", period="2026-09", sessions=[_summary()], total=1,
    )))
    client = TestClient(_app(service))

    resp = client.get("/costs/users/u1/sessions", params={"period": "2026-09", "sort": "recent", "limit": 7})

    assert resp.status_code == 200
    body = resp.json()
    assert body["userId"] == "u1" and body["sessions"][0]["sessionId"] == "s1"
    assert body["sessions"][0]["costKnown"] is True
    service.get_user_sessions.assert_awaited_once_with(
        user_id="u1", period="2026-09", all_time=False, sort="recent", limit=7,
    )


def test_all_time_overrides_the_period():
    service = SimpleNamespace(get_user_sessions=AsyncMock(return_value=UserSessionsResponse(
        user_id="u1", period=None, sessions=[], total=0,
    )))
    client = TestClient(_app(service))

    resp = client.get("/costs/users/u1/sessions", params={"period": "2026-09", "allTime": "true"})

    assert resp.status_code == 200
    service.get_user_sessions.assert_awaited_once_with(
        user_id="u1", period=None, all_time=True, sort="cost", limit=100,
    )


def test_user_sessions_rejects_an_unknown_sort_and_a_bad_period():
    service = SimpleNamespace(get_user_sessions=AsyncMock())
    client = TestClient(_app(service))
    assert client.get("/costs/users/u1/sessions", params={"sort": "title"}).status_code == 422
    assert client.get("/costs/users/u1/sessions", params={"period": "Sept 2026"}).status_code == 422
    service.get_user_sessions.assert_not_awaited()


def test_user_sessions_maps_service_failure_to_500():
    service = SimpleNamespace(get_user_sessions=AsyncMock(side_effect=RuntimeError("boom")))
    client = TestClient(_app(service))
    resp = client.get("/costs/users/u1/sessions")
    assert resp.status_code == 500
    assert "boom" not in resp.text  # no internal detail leaks


def test_session_profile_returns_200():
    service = SimpleNamespace(get_session_profile=AsyncMock(return_value=_profile()))
    client = TestClient(_app(service))
    resp = client.get("/costs/sessions/s1/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sessionId"] == "s1" and body["compactionThreshold"] == 100_000
    assert body["dataCoverage"] == {
        "toolCensus": False, "compactionCount": False, "fingerprints": False, "cost": False,
        "prefixTokens": False, "windowTrim": False, "compactionEvents": False,
        "feedback": False,
        "documents": False,
    }
    assert body["feedback"] == {
        "up": 0, "down": 0, "byTurnClass": None, "reasons": {},
        "unjoined": 0, "retried": 0, "reworkUsd": None,
    }
    service.get_session_profile.assert_awaited_once_with("s1")


def test_session_profile_404s_when_the_session_has_no_row():
    service = SimpleNamespace(get_session_profile=AsyncMock(return_value=None))
    client = TestClient(_app(service))
    assert client.get("/costs/sessions/missing/profile").status_code == 404


def test_session_profile_maps_service_failure_to_500():
    service = SimpleNamespace(get_session_profile=AsyncMock(side_effect=RuntimeError("boom")))
    client = TestClient(_app(service))
    assert client.get("/costs/sessions/s1/profile").status_code == 500


def test_both_routes_are_denied_without_the_scope():
    app = FastAPI()
    app.include_router(cost_routes.router)

    def deny():
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Access denied.")

    override_admin_auth(app, deny)
    app.dependency_overrides[cost_routes.get_cost_service] = lambda: SimpleNamespace()
    client = TestClient(app)
    assert client.get("/costs/users/u1/sessions").status_code == 403
    assert client.get("/costs/sessions/s1/profile").status_code == 403


# ---------------------------------------------------------------------------
# GET /admin/costs/feedback — the fleet outcome signal
# ---------------------------------------------------------------------------


def test_fleet_feedback_returns_200_and_passes_its_params():
    from apis.app_api.admin.costs.models import FleetFeedbackClass, FleetFeedbackSummary

    summary = FleetFeedbackSummary(
        period="2026-09", up=30, down=10, n=40, down_rate=0.25,
        coverage=0.02, assistant_calls=2_000,
        reasons={"tool_failed": 6, "length": 4},
        by_turn_class={
            "full": FleetFeedbackClass(up=18, down=2, n=20, down_rate=0.1, calls=500),
            "digestOnly": FleetFeedbackClass(up=12, down=8, n=20, down_rate=0.4, calls=900),
        },
        retried=7, rework_usd=1.25,
        sessions_with_feedback=12, sessions_scanned=12, tracked=True,
    )
    service = SimpleNamespace(get_fleet_feedback=AsyncMock(return_value=summary))
    client = TestClient(_app(service))

    # Distinct values so the assertion below cannot pass on a default.
    resp = client.get(
        "/costs/feedback",
        params={"period": "2026-09", "users_to_scan": 17, "sessions_to_scan": 33},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == "2026-09"
    assert body["byTurnClass"]["digestOnly"]["downRate"] == 0.4
    assert body["byTurnClass"]["digestOnly"]["n"] == 20, "a rate is never reported without its base"
    assert body["reasons"] == {"tool_failed": 6, "length": 4}
    assert body["coverage"] == 0.02 and body["assistantCalls"] == 2_000
    assert body["reworkUsd"] == 1.25
    service.get_fleet_feedback.assert_awaited_once_with(
        period="2026-09", users_to_scan=17, sessions_to_scan=33
    )


def test_fleet_feedback_reports_untracked_rather_than_zero():
    from apis.app_api.admin.costs.models import FleetFeedbackSummary

    service = SimpleNamespace(
        get_fleet_feedback=AsyncMock(return_value=FleetFeedbackSummary(period="2026-08"))
    )
    body = TestClient(_app(service)).get("/costs/feedback").json()
    assert body["tracked"] is False
    assert body["downRate"] is None and body["coverage"] is None


def test_fleet_feedback_500s_cleanly():
    service = SimpleNamespace(get_fleet_feedback=AsyncMock(side_effect=RuntimeError("boom")))
    resp = TestClient(_app(service)).get("/costs/feedback")
    assert resp.status_code == 500
    assert "boom" not in resp.text, "an internal error message must not reach the client"
