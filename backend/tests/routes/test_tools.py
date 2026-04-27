"""Tests for tools routes.

Endpoints under test:
- GET /tools/   → 200 with user's accessible tools (authenticated)
- GET /tools/   → 401 for unauthenticated request

Requirements: 8.1, 8.2
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.tools.routes import router
from apis.app_api.tools.models import UserToolAccess
from apis.shared.rbac.models import UserEffectivePermissions
from tests.routes.conftest import mock_auth_user, mock_no_auth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROUTES_MODULE = "apis.app_api.tools.routes"

SAMPLE_TOOL = UserToolAccess(
    tool_id="fetch_url_content",
    display_name="URL Fetcher",
    description="Fetch and extract text content from web pages",
    category="search",
    protocol="local",
    status="active",
    requires_oauth_provider=None,
    granted_by=["public"],
    enabled_by_default=True,
    user_enabled=None,
    is_enabled=True,
)


def _make_permissions(user_id="user-001", roles=None, tools=None):
    """Create a mock UserEffectivePermissions."""
    return UserEffectivePermissions(
        user_id=user_id,
        app_roles=roles or ["User"],
        tools=tools or ["*"],
        models=["*"],
        quota_tier=None,
        resolved_at="2024-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the tools router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


# ---------------------------------------------------------------------------
# Requirement 8.1: GET /tools returns 200 with tool list for authenticated user
# ---------------------------------------------------------------------------


class TestGetToolsAuthenticated:
    """GET /tools/ returns 200 with tool list for authenticated user."""

    def test_returns_200_with_tools(self, app, make_user):
        """Req 8.1: Authenticated user gets 200 with tool list."""
        user = make_user()
        mock_auth_user(app, user)

        mock_service = MagicMock()
        mock_service.get_user_accessible_tools = AsyncMock(return_value=[SAMPLE_TOOL])
        mock_service.get_categories = AsyncMock(return_value=["utility"])

        mock_role_service = MagicMock()
        mock_role_service.resolve_user_permissions = AsyncMock(
            return_value=_make_permissions()
        )

        with patch(
            f"{ROUTES_MODULE}.get_tool_catalog_service", return_value=mock_service
        ), patch(
            f"{ROUTES_MODULE}.get_app_role_service", return_value=mock_role_service
        ):
            client = TestClient(app)
            resp = client.get("/tools/")

        assert resp.status_code == 200
        body = resp.json()
        assert "tools" in body
        assert "categories" in body
        assert "appRolesApplied" in body
        assert len(body["tools"]) == 1
        assert body["categories"] == ["utility"]

    def test_returns_200_with_empty_tools(self, app, make_user):
        """Req 8.1: Authenticated user gets 200 with empty list when no tools."""
        user = make_user()
        mock_auth_user(app, user)

        mock_service = MagicMock()
        mock_service.get_user_accessible_tools = AsyncMock(return_value=[])
        mock_service.get_categories = AsyncMock(return_value=[])

        mock_role_service = MagicMock()
        mock_role_service.resolve_user_permissions = AsyncMock(
            return_value=_make_permissions()
        )

        with patch(
            f"{ROUTES_MODULE}.get_tool_catalog_service", return_value=mock_service
        ), patch(
            f"{ROUTES_MODULE}.get_app_role_service", return_value=mock_role_service
        ):
            client = TestClient(app)
            resp = client.get("/tools/")

        assert resp.status_code == 200
        body = resp.json()
        assert body["tools"] == []
        assert body["categories"] == []


# ---------------------------------------------------------------------------
# Requirement 8.2: GET /tools returns 401 for unauthenticated request
# ---------------------------------------------------------------------------


class TestGetToolsUnauthenticated:
    """GET /tools/ returns 401 for unauthenticated request."""

    def test_returns_401_unauthenticated(self, app, unauthenticated_client):
        """Req 8.2: Unauthenticated request gets 401."""
        client = unauthenticated_client(app)
        resp = client.get("/tools/")

        assert resp.status_code == 401
