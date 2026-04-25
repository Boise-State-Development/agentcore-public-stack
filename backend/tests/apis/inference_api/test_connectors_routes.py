"""Route-level tests for `/connectors/complete-consent`.

`complete-consent` is a thin wrapper around AgentCore's
`CompleteResourceTokenAuth` — the auth boundary is `current_user`
(verified by `get_current_user_trusted`) and AgentCore's own
`userIdentifier` binding rejects mismatched completion attempts. The
AgentCore control-plane client is patched out — we test our forwarding
and error surface, not the downstream call itself.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.inference_api.connectors import routes
from apis.shared.auth.models import User


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


class TestCompleteConsent:
    """`complete-consent` is a thin wrapper around AgentCore's
    `CompleteResourceTokenAuth`. The auth boundary is `current_user`
    (verified by `get_current_user_trusted`) — we forward that identity
    as `userIdentifier` and AgentCore's own binding rejects mismatches.
    """

    def test_forwards_caller_identity_to_agentcore(self, app_for_user, monkeypatch):
        mock_client = MagicMock()
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        app = app_for_user("alice")
        response = TestClient(app).post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": "google"},
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mock_client.complete_resource_token_auth.assert_called_once_with(
            userIdentifier={"userId": "alice"},
            sessionUri="uri-abc",
        )

    def test_surfaces_agentcore_error_as_502(self, app_for_user, monkeypatch):
        mock_client = MagicMock()
        mock_client.complete_resource_token_auth.side_effect = RuntimeError("agentcore down")
        monkeypatch.setattr(routes, "_agentcore_control_client", lambda: mock_client)

        app = app_for_user("alice")
        response = TestClient(app).post(
            "/connectors/complete-consent",
            json={"session_uri": "uri-abc", "provider_id": "google"},
        )

        assert response.status_code == 502
        assert "agentcore down" in response.json()["detail"]
