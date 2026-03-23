"""Integration tests for auth API routes using FastAPI TestClient.

Tests the full HTTP request/response cycle for:
- GET /auth/providers
- GET /auth/login
- POST /auth/token
- POST /auth/refresh
- GET /auth/logout

All service dependencies are mocked to isolate route logic.

Requirements: 11.1–11.8
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.app_api.auth.routes import router


# ---------------------------------------------------------------------------
# App fixture — mounts only the auth router
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Requirement 11.2: GET /auth/providers returns provider list
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Requirement 11.3: GET /auth/login returns auth URL + state
# ---------------------------------------------------------------------------


class TestLogin:
    """GET /auth/login initiates OIDC login."""

    def test_returns_authorization_url_and_state(self, client):
        """Should return authorization_url and state for a valid provider."""
        mock_service = MagicMock()
        mock_service.redirect_uri = "http://localhost:4200/auth/callback"
        mock_service.generate_state.return_value = (
            "state-abc",
            "challenge-xyz",
            "nonce-123",
        )
        mock_service.build_authorization_url.return_value = (
            "https://login.example.com/authorize?state=state-abc"
        )

        with patch(
            "apis.app_api.auth.routes.get_generic_auth_service",
            new_callable=AsyncMock,
            return_value=mock_service,
        ):
            resp = client.get("/auth/login", params={"provider_id": "test"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["authorization_url"] == "https://login.example.com/authorize?state=state-abc"
        assert body["state"] == "state-abc"


# ---------------------------------------------------------------------------
# Requirement 11.4: GET /auth/login unknown provider 400
# ---------------------------------------------------------------------------


    def test_unknown_provider_returns_error(self, client):
        """Should return an error when provider_id is unknown."""
        with patch(
            "apis.app_api.auth.routes.get_generic_auth_service",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=400, detail="Provider not found"),
        ):
            resp = client.get("/auth/login", params={"provider_id": "nonexistent"})

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Requirement 11.5: POST /auth/token valid exchange
# ---------------------------------------------------------------------------


class TestExchangeToken:
    """POST /auth/token exchanges authorization code for tokens."""

    def test_valid_exchange_returns_tokens(self, client):
        """Should return tokens when state and code are valid."""
        mock_service = MagicMock()
        mock_service.exchange_code_for_tokens = AsyncMock(
            return_value={
                "access_token": "at-123",
                "refresh_token": "rt-456",
                "id_token": "id-789",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid profile",
                "provider_id": "test",
            }
        )

        # Mock _peek_provider_from_state to return a provider_id
        with patch(
            "apis.app_api.auth.routes._peek_provider_from_state",
            return_value="test",
        ), patch(
            "apis.app_api.auth.routes.get_generic_auth_service",
            new_callable=AsyncMock,
            return_value=mock_service,
        ):
            resp = client.post(
                "/auth/token",
                json={"code": "auth-code", "state": "valid-state"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["access_token"] == "at-123"
        assert body["refresh_token"] == "rt-456"
        assert body["id_token"] == "id-789"
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == 3600
        assert body["scope"] == "openid profile"


# ---------------------------------------------------------------------------
# Requirement 11.6: POST /auth/token invalid state 400
# ---------------------------------------------------------------------------


    def test_invalid_state_returns_400(self, client):
        """Should return 400 when state cannot be resolved to a provider."""
        with patch(
            "apis.app_api.auth.routes._peek_provider_from_state",
            return_value=None,
        ):
            resp = client.post(
                "/auth/token",
                json={"code": "auth-code", "state": "bogus-state"},
            )

        assert resp.status_code == 400

    def test_exchange_http_exception_propagates(self, client):
        """Should propagate HTTPException from exchange_code_for_tokens."""
        mock_service = MagicMock()
        mock_service.exchange_code_for_tokens = AsyncMock(
            side_effect=HTTPException(status_code=400, detail="Invalid or expired state"),
        )

        with patch(
            "apis.app_api.auth.routes._peek_provider_from_state",
            return_value="test",
        ), patch(
            "apis.app_api.auth.routes.get_generic_auth_service",
            new_callable=AsyncMock,
            return_value=mock_service,
        ):
            resp = client.post(
                "/auth/token",
                json={"code": "auth-code", "state": "bad-state"},
            )

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Requirement 11.7: POST /auth/refresh success
# ---------------------------------------------------------------------------


class TestRefreshToken:
    """POST /auth/refresh refreshes access token."""

    def test_refresh_success(self, client):
        """Should return new tokens on successful refresh."""
        mock_service = MagicMock()
        mock_service.refresh_access_token = AsyncMock(
            return_value={
                "access_token": "new-at-123",
                "refresh_token": "new-rt-456",
                "id_token": None,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid",
            }
        )

        with patch(
            "apis.app_api.auth.routes.get_generic_auth_service",
            new_callable=AsyncMock,
            return_value=mock_service,
        ):
            resp = client.post(
                "/auth/refresh",
                params={"provider_id": "test"},
                json={"refresh_token": "old-rt"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["access_token"] == "new-at-123"
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == 3600


# ---------------------------------------------------------------------------
# Requirement 11.8: GET /auth/logout returns URL
# ---------------------------------------------------------------------------


class TestLogout:
    """GET /auth/logout returns logout URL."""

    def test_logout_returns_url(self, client):
        """Should return a logout_url for the given provider."""
        mock_service = MagicMock()
        mock_service.build_logout_url.return_value = (
            "https://login.example.com/logout?post_logout_redirect_uri=http://localhost"
        )

        with patch(
            "apis.app_api.auth.routes.get_generic_auth_service",
            new_callable=AsyncMock,
            return_value=mock_service,
        ):
            resp = client.get(
                "/auth/logout",
                params={"provider_id": "test"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "logout" in body["logout_url"]

