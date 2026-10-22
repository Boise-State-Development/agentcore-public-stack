"""Admin eval-sampling routes (spec §11 PR-4):

- GET  /admin/feedback/evaluations       → 200, content-free queue with verdicts
- POST /admin/feedback/evaluations/run   → 202 and a background batch with the injected judge;
                                           404 while FEEDBACK_EVAL_SAMPLING_ENABLED is off
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.feedback import routes as feedback_routes
from apis.shared.observability.content_policy import content_bearing_paths
from tests.conftest import override_admin_auth


def _app(storage, judge=None):
    app = FastAPI()
    app.include_router(feedback_routes.router)
    override_admin_auth(app, lambda: SimpleNamespace(user_id="admin", email="a@x", roles=["system_admin"]))
    app.dependency_overrides[feedback_routes.get_storage] = lambda: storage
    app.dependency_overrides[feedback_routes.get_judge] = lambda: judge or SimpleNamespace(judge=lambda *a, **k: [])
    return app


def test_queue_lists_recent_down_thumbs_with_verdicts(monkeypatch):
    monkeypatch.delenv("FEEDBACK_EVAL_SAMPLING_ENABLED", raising=False)
    storage = SimpleNamespace(get_recent_down_thumbs=AsyncMock(return_value=[
        {"sessionId": "s1", "messageId": 3, "reason": "wrong", "updatedAt": "t2", "evaluatedAt": "t3",
         "evaluation": {"reason": "wrong", "evaluators": ["Builtin.Correctness"],
                        "scores": {"Builtin.Correctness": {"value": 0.5, "n": 2, "tokens": 100, "rating": "Meh"}}}},
        {"sessionId": "s2", "messageId": 1, "updatedAt": "t1"},
        {"garbage": True},
    ]))
    resp = TestClient(_app(storage)).get("/feedback/evaluations", params={"limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["samplingEnabled"] is False and body["pending"] == 1
    assert [i["sessionId"] for i in body["items"]] == ["s1", "s2"]
    assert body["items"][0]["evaluation"]["scores"]["Builtin.Correctness"]["rating"] == "Meh"
    assert content_bearing_paths(body) == []
    storage.get_recent_down_thumbs.assert_awaited_once_with(limit=5)


def test_run_is_404_while_off_and_202_with_a_batch_when_on(monkeypatch):
    calls = {}

    async def fake_batch(table, judge, *, limit, cost_row_lookup=None):
        calls["limit"] = limit
        calls["judge"] = judge
        return SimpleNamespace()

    monkeypatch.setattr("apis.shared.feedback_eval.sampler.run_sampling_batch", fake_batch)
    storage = SimpleNamespace(sessions_metadata_table=object(), get_recent_down_thumbs=AsyncMock(return_value=[]))
    judge = SimpleNamespace(judge=lambda *a, **k: [])

    monkeypatch.setenv("FEEDBACK_EVAL_SAMPLING_ENABLED", "false")
    assert TestClient(_app(storage, judge)).post("/feedback/evaluations/run").status_code == 404
    assert calls == {}

    monkeypatch.setenv("FEEDBACK_EVAL_SAMPLING_ENABLED", "true")
    resp = TestClient(_app(storage, judge)).post("/feedback/evaluations/run", params={"limit": 7})
    assert resp.status_code == 202
    assert resp.json()["accepted"] is True and resp.json()["limit"] == 7
    # TestClient runs background tasks before returning.
    assert calls["limit"] == 7 and calls["judge"] is judge


def test_run_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("FEEDBACK_EVAL_SAMPLING_ENABLED", "true")
    storage = SimpleNamespace(sessions_metadata_table=object())
    assert TestClient(_app(storage)).post("/feedback/evaluations/run", params={"limit": 500}).status_code == 422
