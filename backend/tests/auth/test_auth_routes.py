"""Integration tests for auth API routes using FastAPI TestClient.

Tests the full HTTP request/response cycle for:
- GET /auth/providers

All service dependencies are mocked to isolate route logic.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.auth.routes import router


@pytest.fixture
def app():
    """Create a minimal FastAPI app with the auth router mounted."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
def client(app):
    """TestClient bound to the minimal app."""
    return TestClient(app)


class TestListAuthProviders:
    """GET /auth/providers returns enabled providers."""

    def test_returns_provider_list(self, client, make_provider):
        """Should return a list of enabled providers with public info."""
        provider = make_provider(
            provider_id="okta",
            display_name="Okta",
            logo_url="https://example.com/okta.png",
            button_color="#0066CC",
        )

        mock_repo = AsyncMock()
        mock_repo.enabled = True
        mock_repo.list_providers = AsyncMock(return_value=[provider])

        with patch(
            "apis.shared.auth_providers.repository.get_auth_provider_repository",
            return_value=mock_repo,
        ):
            resp = client.get("/auth/providers")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["providers"]) == 1
        p = body["providers"][0]
        assert p["provider_id"] == "okta"
        assert p["display_name"] == "Okta"
        assert p["logo_url"] == "https://example.com/okta.png"
        assert p["button_color"] == "#0066CC"

    def test_returns_empty_when_repo_disabled(self, client):
        """Should return empty list when provider repo is disabled."""
        mock_repo = AsyncMock()
        mock_repo.enabled = False

        with patch(
            "apis.shared.auth_providers.repository.get_auth_provider_repository",
            return_value=mock_repo,
        ):
            resp = client.get("/auth/providers")

        assert resp.status_code == 200
        assert resp.json()["providers"] == []


class TestCognitoLoginVisible:
    """GET /auth/providers says whether the login page shows the Cognito button."""

    def _get(self, client, providers, hide_requested):
        mock_repo = AsyncMock()
        mock_repo.enabled = True
        mock_repo.list_providers = AsyncMock(return_value=providers)
        read_hide = AsyncMock(return_value=hide_requested)

        with patch(
            "apis.shared.auth_providers.repository.get_auth_provider_repository",
            return_value=mock_repo,
        ), patch(
            "apis.app_api.system.login_page.read_hide_cognito_login", read_hide
        ):
            resp = client.get("/auth/providers")

        assert resp.status_code == 200
        return resp.json(), read_hide

    def test_visible_by_default(self, client, make_provider):
        body, _ = self._get(client, [make_provider()], hide_requested=False)
        assert body["cognito_login_visible"] is True

    def test_hidden_when_requested_and_a_provider_is_enabled(self, client, make_provider):
        body, _ = self._get(client, [make_provider()], hide_requested=True)
        assert body["cognito_login_visible"] is False
        assert len(body["providers"]) == 1

    def test_visible_when_requested_but_no_provider_is_enabled(self, client):
        body, read_hide = self._get(client, [], hide_requested=True)
        assert body["cognito_login_visible"] is True
        read_hide.assert_not_called()

    def test_visible_when_repo_disabled(self, client):
        mock_repo = AsyncMock()
        mock_repo.enabled = False

        with patch(
            "apis.shared.auth_providers.repository.get_auth_provider_repository",
            return_value=mock_repo,
        ):
            resp = client.get("/auth/providers")

        assert resp.json()["cognito_login_visible"] is True
