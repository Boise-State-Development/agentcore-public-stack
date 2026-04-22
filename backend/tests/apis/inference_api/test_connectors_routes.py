"""Route-level tests for `/connectors/complete-consent`.

Focused on the defence-in-depth check that rejects a `session_uri` that
was never remembered for the caller. The AgentCore control-plane client
is patched out — we care about whether our gate blocks the request
before it reaches AWS, not the downstream call itself.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.inference_api.connectors import routes
from apis.shared.auth.models import User
from apis.shared.oauth import session_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    session_cache._pending.clear()  # noqa: SLF001 — test-only
    yield
    session_cache._pending.clear()  # noqa: SLF001 — test-only


@pytest.fixture(autouse=True)
def _reset_control_client():
    """`_agentcore_control_client` is `lru_cache`d; reset between tests."""
    routes._agentcore_control_client.cache_clear()
    yield
    routes._agentcore_control_client.cache_clear()


def _make_user(user_id: str) -> User:
    return User(
        user_id=user_id,
        email=f"{user_id}@example.com",
        name=user_id.capitalize(),
        roles=[],
        raw_token="test-token",
    )


@pytest.fixture
def app_for_user():
    """Build a minimal FastAPI app with the connectors router mounted and
    the `get_current_user_trusted` dependency stubbed to a specific user.
    Returns a factory so each test picks the caller's identity.
    """

    def _build(user_id: str) -> FastAPI:
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.get_current_user_trusted] = lambda: _make_user(user_id)
        return app

    return _build


class TestCompleteConsentAuthorization:
    def test_rejects_unknown_session_uri(self, app_for_user, monkeypatch):
        """No prior `remember` call → 403."""
        mock_client = MagicMock()
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        app = app_for_user("alice")
        client = TestClient(app)
        response = client.post(
            "/connectors/complete-consent",
            json={"session_uri": "never-issued", "provider_id": "google"},
        )

        assert response.status_code == 403
        assert "not initiated by you" in response.json()["detail"]
        # AgentCore must not be called when the gate rejects the request.
        mock_client.complete_resource_token_auth.assert_not_called()

    def test_rejects_session_uri_from_different_user(self, app_for_user, monkeypatch):
        """Alice's session cannot be completed by Bob."""
        mock_client = MagicMock()
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        # Alice starts a flow; Bob's request tries to steal it.
        session_cache.remember("alice", "uri-abc")

        app = app_for_user("bob")
        client = TestClient(app)
        response = client.post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": "google"},
        )

        assert response.status_code == 403
        mock_client.complete_resource_token_auth.assert_not_called()
        # Alice's entry survives — Bob's rejection is non-destructive.
        assert session_cache.consume("alice", "uri-abc") is True

    def test_accepts_session_uri_remembered_for_caller(self, app_for_user, monkeypatch):
        mock_client = MagicMock()
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        session_cache.remember("alice", "uri-abc")

        app = app_for_user("alice")
        client = TestClient(app)
        response = client.post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": "google"},
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mock_client.complete_resource_token_auth.assert_called_once_with(
            userIdentifier={"userId": "alice"},
            sessionUri="uri-abc",
        )

    def test_single_use_rejects_replay(self, app_for_user, monkeypatch):
        """A successful completion consumes the entry — replay gets 403."""
        mock_client = MagicMock()
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        session_cache.remember("alice", "uri-abc")

        app = app_for_user("alice")
        client = TestClient(app)
        first = client.post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": "google"},
        )
        second = client.post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": "google"},
        )

        assert first.status_code == 200
        assert second.status_code == 403
        assert mock_client.complete_resource_token_auth.call_count == 1

    def test_surfaces_agentcore_error_as_502(self, app_for_user, monkeypatch):
        mock_client = MagicMock()
        mock_client.complete_resource_token_auth.side_effect = RuntimeError("agentcore down")
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        session_cache.remember("alice", "uri-abc")

        app = app_for_user("alice")
        client = TestClient(app)
        response = client.post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": "google"},
        )

        assert response.status_code == 502
        assert "agentcore down" in response.json()["detail"]
