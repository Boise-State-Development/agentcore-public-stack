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
    monkeypatch.setenv("CLIENT_TURN_TIMING_ENABLED", "")
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


def test_a_cold_first_turn_and_a_follow_up_use_their_own_names() -> None:
    cold = routes.TurnTimingReport(firstTokenMs=3900, turnKind="first")
    follow = routes.TurnTimingReport(firstTokenMs=1400, turnKind="followup", warmAgeMs=10)
    assert routes.metrics_for(cold) == {
        "ClientFirstTokenMs": 3900,
        "ClientFirstTurnFirstTokenMs": 3900,
        "ClientFirstTurnColdFirstTokenMs": 3900,
    }
    assert routes.metrics_for(follow) == {"ClientFirstTokenMs": 1400, "ClientFollowUpFirstTokenMs": 1400}


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
        {"turnKind": "resume"},
        {"modelId": "x" * 201},
        {"warmAgeMs": -5},
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
