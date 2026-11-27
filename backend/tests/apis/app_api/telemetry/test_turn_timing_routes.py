"""Client-measured time to first token (docs/specs/turn-path-ttft.md §6)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.telemetry import routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User


def _user(user_id: str = "user-sub") -> User:
    user = User(email="alice@example.com", user_id=user_id, name="Alice", roles=["user"])
    user.raw_token = "t"
    return user


def _app(user: Optional[User] = None) -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)
    if user is not None:
        app.dependency_overrides[get_current_user_from_session] = lambda: user
    return app


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLIENT_TURN_TIMING_ENABLED", "true")
    routes._limiter.reset()
    yield
    routes._limiter.reset()


@pytest.fixture()
def emitted(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    seen: List[Dict[str, Any]] = []
    monkeypatch.setattr(routes, "emit_emf_metrics", lambda **kw: seen.append(kw))
    return seen


def _post(client: TestClient, **body: Any):
    payload = {"firstTokenMs": 2300, "turnKind": "first"}
    payload.update(body)
    return client.post("/telemetry/turn-timing", json=payload)


def test_requires_a_session_cookie(emitted) -> None:
    assert _post(TestClient(_app())).status_code == 401
    assert emitted == []


def test_404_while_the_flag_is_off(monkeypatch: pytest.MonkeyPatch, emitted) -> None:
    monkeypatch.setenv("CLIENT_TURN_TIMING_ENABLED", "false")
    assert _post(TestClient(_app(_user()))).status_code == 404
    assert emitted == []


def test_records_a_warmed_first_turn(emitted) -> None:
    resp = _post(TestClient(_app(_user())), modelId="us.anthropic.claude-haiku-4-5", warmAgeMs=6100, hasAgent=True)
    assert resp.status_code == 204
    (record,) = emitted
    assert record["namespace"] == "AgentCoreStack/TurnLatency"
    assert record["metrics"] == {
        "ClientFirstTokenMs": 2300,
        "ClientFirstTurnFirstTokenMs": 2300,
        "ClientFirstTurnWarmedFirstTokenMs": 2300,
    }
    assert set(record["units"].values()) == {"Milliseconds"}
    assert record["properties"]["modelId"] == "us.anthropic.claude-haiku-4-5"
    assert record["properties"]["warmAgeMs"] == 6100
    assert record["properties"]["hasAgent"] is True


def test_records_the_bundle_s_version_beside_the_server_s(emitted) -> None:
    """The SPA's `spaVersion` and app-api's own `appVersion` stamp differ for a
    while during a deploy; a client-side regression belongs to the bundle."""
    _post(TestClient(_app(_user())), spaVersion="1.33.0")
    (record,) = emitted
    assert record["properties"]["spaVersion"] == "1.33.0"
    assert "appVersion" not in record["properties"]  # the emitter adds the server's


def test_a_cold_first_turn_and_a_follow_up_use_their_own_names() -> None:
    cold = routes.TurnTimingReport(firstTokenMs=3900, turnKind="first")
    follow = routes.TurnTimingReport(firstTokenMs=1400, turnKind="followup", warmAgeMs=10)
    assert routes.metrics_for(cold) == {
        "ClientFirstTokenMs": 3900,
        "ClientFirstTurnFirstTokenMs": 3900,
        "ClientFirstTurnColdFirstTokenMs": 3900,
    }
    assert routes.metrics_for(follow) == {"ClientFirstTokenMs": 1400, "ClientFollowUpFirstTokenMs": 1400}


def test_a_prepare_arm_records_under_its_own_name(emitted) -> None:
    treated = routes.TurnTimingReport(
        firstTokenMs=1200, turnKind="followup", warmAgeMs=9000, preparedAgeMs=7400, prepareArm="treated"
    )
    assert routes.metrics_for(treated) == {
        "ClientFirstTokenMs": 1200,
        "ClientFollowUpFirstTokenMs": 1200,
        "ClientPrepareTreatedFirstTokenMs": 1200,
    }
    holdout = routes.TurnTimingReport(firstTokenMs=1500, turnKind="followup", prepareArm="holdout")
    assert "ClientPrepareHoldoutFirstTokenMs" in routes.metrics_for(holdout)
    assert "ClientPrepareTreatedFirstTokenMs" not in routes.metrics_for(holdout)
    resp = _post(TestClient(_app(_user())), preparedAgeMs=7400, prepareArm="treated")
    assert resp.status_code == 204
    assert emitted[0]["properties"]["preparedAgeMs"] == 7400
    assert emitted[0]["properties"]["prepareArm"] == "treated"
    assert "ClientPrepareTreatedFirstTokenMs" in emitted[0]["metrics"]
    assert "ClientPreparedFirstTokenMs" not in emitted[0]["metrics"]


def test_first_visible_output_records_under_its_own_name(emitted) -> None:
    """A turn that thinks first: the user reads thinking text long before the
    answer starts. `ClientFirstTokenMs` keeps its meaning; the new name sits
    beside it."""
    resp = _post(TestClient(_app(_user())), firstTokenMs=18_400, firstVisibleMs=2_500, turnKind="followup")
    assert resp.status_code == 204
    (record,) = emitted
    assert record["metrics"] == {
        "ClientFirstTokenMs": 18_400,
        "ClientFollowUpFirstTokenMs": 18_400,
        "ClientFirstVisibleMs": 2_500,
    }
    assert record["units"]["ClientFirstVisibleMs"] == "Milliseconds"


def test_a_turn_that_did_not_think_reports_the_two_clocks_equal() -> None:
    report = routes.TurnTimingReport(firstTokenMs=1400, firstVisibleMs=1400, turnKind="followup")
    assert routes.metrics_for(report)["ClientFirstVisibleMs"] == 1400


def test_a_bundle_without_the_field_records_no_first_visible_metric(emitted) -> None:
    """An SPA bundle that predates the field (a tab left open across a deploy)
    must not be recorded as if its first token were its first visible output."""
    assert _post(TestClient(_app(_user()))).status_code == 204
    assert "ClientFirstVisibleMs" not in emitted[0]["metrics"]


def test_first_visible_never_reads_later_than_the_first_token() -> None:
    report = routes.TurnTimingReport(firstTokenMs=1400, firstVisibleMs=9000, turnKind="followup")
    assert routes.metrics_for(report)["ClientFirstVisibleMs"] == 1400


def test_an_unknown_prepare_arm_is_rejected() -> None:
    assert _post(TestClient(_app(_user())), prepareArm="other").status_code == 422


def test_a_turn_with_no_prepare_keeps_today_s_names(emitted) -> None:
    resp = _post(TestClient(_app(_user())))
    assert resp.status_code == 204
    assert emitted[0]["properties"]["preparedAgeMs"] is None
    assert emitted[0]["properties"]["prepareArm"] is None
    assert not [m for m in emitted[0]["metrics"] if m.startswith("ClientPrepare")]


def test_records_no_identity(emitted) -> None:
    _post(TestClient(_app(_user("someone-specific"))))
    (record,) = emitted
    flat = repr(record)
    assert "someone-specific" not in flat
    assert "alice@example.com" not in flat
    assert not {"userId", "sessionId", "email"} & set(record["properties"])


@pytest.mark.parametrize(
    "body",
    [
        {"firstTokenMs": -1},
        {"firstTokenMs": 600_001},
        {"firstVisibleMs": -1},
        {"firstVisibleMs": 600_001},
        {"turnKind": "resume"},
        {"modelId": "x" * 201},
        {"warmAgeMs": -5},
        {"spaVersion": "v" * 41},
    ],
)
def test_rejects_implausible_reports(body: Dict[str, Any], emitted) -> None:
    assert _post(TestClient(_app(_user())), **body).status_code == 422
    assert emitted == []


def test_per_user_cap_drops_quietly(emitted) -> None:
    client = TestClient(_app(_user("busy")))
    for _ in range(routes._MAX_REPORTS_PER_USER_PER_MINUTE):
        assert _post(client).status_code == 204
    assert _post(client).status_code == 204  # still 204: nothing for the SPA to handle
    assert len(emitted) == routes._MAX_REPORTS_PER_USER_PER_MINUTE
    _post(TestClient(_app(_user("other"))))
    assert len(emitted) == routes._MAX_REPORTS_PER_USER_PER_MINUTE + 1


def test_namespace_matches_the_server_stages() -> None:
    from apis.inference_api.chat import turn_timing

    assert routes.TURN_LATENCY_NAMESPACE == turn_timing.TURN_LATENCY_EMF_NAMESPACE
