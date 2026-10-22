"""Routes for the thumbs signal:

- PUT    /sessions/{id}/messages/{messageId}/feedback → 200 with the stored thumb
- DELETE /sessions/{id}/messages/{messageId}/feedback → 204
- 404 for another user's session, 404 while RESPONSE_FEEDBACK_ENABLED=false,
  422 for anything but ±1 / a fixed reason code, 503 with no table.

Auth is the cookie-aware `get_current_user_from_session` (SPA-facing route);
storage is swapped through the module's own function names.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.sessions import routes as session_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.sessions.feedback import SessionNotOwned
from apis.shared.sessions.models import MessageFeedback


def _user() -> User:
    return User(user_id="user-1", email="u@example.com", name="U", roles=["default"], raw_token="tok")


def _client(monkeypatch, put=None, delete=None, signal=None) -> TestClient:
    monkeypatch.delenv("RESPONSE_FEEDBACK_ENABLED", raising=False)
    monkeypatch.setattr(session_routes, "record_implicit_signal", signal or AsyncMock(return_value=None))
    monkeypatch.setattr(session_routes, "put_message_feedback", put or AsyncMock(
        return_value=MessageFeedback(value=1, updated_at="2026-09-16T00:00:00Z")
    ))
    monkeypatch.setattr(session_routes, "delete_message_feedback", delete or AsyncMock(return_value=True))
    app = FastAPI()
    app.include_router(session_routes.router)
    app.dependency_overrides[get_current_user_from_session] = _user
    return TestClient(app)


def test_put_stores_and_echoes_the_thumb(monkeypatch):
    put = AsyncMock(return_value=MessageFeedback(value=-1, reason="instructions", updated_at="2026-09-16T00:00:00Z"))
    client = _client(monkeypatch, put=put)

    resp = client.put("/sessions/s1/messages/3/feedback", json={"value": -1, "reason": "instructions"})

    assert resp.status_code == 200
    assert resp.json() == {"value": -1, "reason": "instructions", "updatedAt": "2026-09-16T00:00:00Z"}
    put.assert_awaited_once_with(session_id="s1", user_id="user-1", message_id=3, value=-1, reason="instructions", retry_message_id=None)


def test_put_forwards_the_retry_link(monkeypatch):
    put = AsyncMock(return_value=MessageFeedback(value=-1, retry_message_id=4, updated_at="t"))
    client = _client(monkeypatch, put=put)
    resp = client.put("/sessions/s1/messages/3/feedback", json={"value": -1, "retryMessageId": 4})
    assert resp.status_code == 200 and resp.json()["retryMessageId"] == 4
    assert put.await_args.kwargs["retry_message_id"] == 4
    assert client.put("/sessions/s1/messages/3/feedback", json={"value": -1, "retryMessageId": -1}).status_code == 422


def test_put_rejects_free_text_and_out_of_range_values(monkeypatch):
    client = _client(monkeypatch)
    assert client.put("/sessions/s1/messages/3/feedback", json={"value": 1, "reason": "it lied to me"}).status_code == 422
    assert client.put("/sessions/s1/messages/3/feedback", json={"value": 0}).status_code == 422
    assert client.put("/sessions/s1/messages/3/feedback", json={"value": 5}).status_code == 422
    assert client.put("/sessions/s1/messages/three/feedback", json={"value": 1}).status_code == 422
    assert client.put("/sessions/s1/messages/-1/feedback", json={"value": 1}).status_code == 422


def test_delete_returns_204(monkeypatch):
    delete = AsyncMock(return_value=True)
    client = _client(monkeypatch, delete=delete)
    resp = client.delete("/sessions/s1/messages/3/feedback")
    assert resp.status_code == 204
    delete.assert_awaited_once_with(session_id="s1", user_id="user-1", message_id=3)


def test_another_users_session_is_404(monkeypatch):
    client = _client(
        monkeypatch,
        put=AsyncMock(side_effect=SessionNotOwned("s1")),
        delete=AsyncMock(side_effect=SessionNotOwned("s1")),
    )
    assert client.put("/sessions/s1/messages/3/feedback", json={"value": 1}).status_code == 404
    assert client.delete("/sessions/s1/messages/3/feedback").status_code == 404


def test_kill_switch_hides_the_surface(monkeypatch):
    put = AsyncMock()
    client = _client(monkeypatch, put=put)
    monkeypatch.setenv("RESPONSE_FEEDBACK_ENABLED", "false")
    assert client.put("/sessions/s1/messages/3/feedback", json={"value": 1}).status_code == 404
    assert client.delete("/sessions/s1/messages/3/feedback").status_code == 404
    put.assert_not_awaited()


def test_implicit_signal_is_fire_and_forget_204(monkeypatch):
    signal = AsyncMock(return_value=None)
    client = _client(monkeypatch, signal=signal)
    assert client.post("/sessions/s1/messages/3/signals", json={"kind": "copy"}).status_code == 204
    signal.assert_awaited_once_with(session_id="s1", user_id="user-1", message_id=3, kind="copy")
    assert client.post("/sessions/s1/messages/3/signals", json={"kind": "abandon"}).status_code == 422
    assert client.post("/sessions/s1/messages/3/signals", json={"kind": "I copied it"}).status_code == 422
    monkeypatch.setenv("RESPONSE_FEEDBACK_ENABLED", "false")
    assert client.post("/sessions/s1/messages/3/signals", json={"kind": "copy"}).status_code == 404


def test_missing_table_is_503_not_500(monkeypatch):
    client = _client(monkeypatch, put=AsyncMock(side_effect=RuntimeError("DYNAMODB_SESSIONS_METADATA_TABLE_NAME")))
    assert client.put("/sessions/s1/messages/3/feedback", json={"value": 1}).status_code == 503
