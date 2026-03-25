"""Property-based and parametrized tests for auth enforcement and RBAC across all routes.

Uses route introspection on the full App API to discover protected endpoints,
then verifies:
- Property 4: Non-admin role rejection (Hypothesis)
- Property 7: Auth enforcement across all protected routes (parametrized)

Requirements: 7.2, 7.3, 17.1, 17.2, 17.3
"""

import re
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from apis.shared.auth.dependencies import get_current_user
from apis.shared.auth.models import User
from apis.shared.auth.rbac import require_admin

from tests.routes.conftest import mock_no_auth


# ---------------------------------------------------------------------------
# Route introspection helpers (Task 10.1)
# Requirements: 17.1, 17.2, 17.3
# ---------------------------------------------------------------------------

# Known public routes that do NOT require the standard get_current_user
# auth dependency.  These are excluded from the auth-enforcement sweep.
# /chat/api-converse uses X-API-Key header auth instead of JWT.
PUBLIC_ROUTE_PATTERNS: set[str] = {
    "/health",
    "/auth/providers",
    "/auth/login",
    "/auth/token",
    "/auth/refresh",
    "/auth/logout",
    "/oauth/callback",
    "/chat/api-converse",
    "/system/status",
    "/system/first-boot",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


def _is_public_route(path: str) -> bool:
    """Return True if *path* matches a known public route."""
    return path in PUBLIC_ROUTE_PATTERNS


def _import_full_app() -> FastAPI:
    """Import the full App API application.

    We patch the lifespan to avoid startup side-effects (RBAC seeding,
    directory creation, etc.) that require AWS credentials.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    with patch("apis.app_api.main.lifespan", _noop_lifespan):
        # Re-import to pick up the patched lifespan
        import importlib
        import apis.app_api.main as app_module
        importlib.reload(app_module)
        return app_module.app


def discover_protected_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Return a list of (method, path) tuples for all protected API routes.

    Iterates ``app.routes``, keeps only ``APIRoute`` instances, expands
    each route's HTTP methods, and filters out known public paths.
    """
    protected: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if _is_public_route(path):
            continue
        for method in route.methods:
            protected.append((method.upper(), path))
    return protected


def _dummy_path(path: str) -> str:
    """Replace FastAPI path parameters with dummy values.

    E.g. ``/sessions/{session_id}`` → ``/sessions/test-id-000``
    """
    return re.sub(r"\{[^}]+\}", "test-id-000", path)


# ---------------------------------------------------------------------------
# Discover routes once at module level so parametrize can use them.
# ---------------------------------------------------------------------------

_FULL_APP = _import_full_app()
_PROTECTED_ROUTES = discover_protected_routes(_FULL_APP)

# Also collect admin-only routes (prefix /admin) for Property 4.
_ADMIN_ROUTES = [(m, p) for m, p in _PROTECTED_ROUTES if p.startswith("/admin")]


# ---------------------------------------------------------------------------
# Property 4: Non-admin role rejection (Hypothesis)
# Validates: Requirements 7.2, 7.3
# ---------------------------------------------------------------------------

# Strategy: generate a list of arbitrary role strings for Hypothesis.
non_admin_roles_strategy = st.lists(
    st.text(
        alphabet=st.characters(whitelist_categories=("L",)),
        min_size=1,
        max_size=20,
    ),
    min_size=0,
    max_size=5,
)


class TestNonAdminRoleRejection:
    """Property 4: Non-admin role rejection.

    For any User whose JWT roles do not map to the ``system_admin`` AppRole,
    admin endpoints return HTTP 403.

    Since ``require_admin`` now resolves permissions via the AppRoleService,
    we mock the service to return no admin AppRoles for the generated users.
    """

    @given(roles=non_admin_roles_strategy)
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_non_admin_roles_get_403(self, roles):
        """Feature: api-route-tests, Property 4: Non-admin role rejection

        **Validates: Requirements 7.2, 7.3**
        """
        from apis.app_api.admin.routes import router as admin_router
        from apis.shared.rbac.models import UserEffectivePermissions

        app = FastAPI()
        app.include_router(admin_router)

        # Override get_current_user to return a user with the generated roles
        user = User(
            email="prop4@example.com",
            user_id="prop4-user",
            name="Property 4 User",
            roles=roles,
        )
        app.dependency_overrides[get_current_user] = lambda: user

        # Mock AppRoleService to return no admin AppRoles (simulates
        # JWT roles that don't map to system_admin in DynamoDB)
        mock_service = AsyncMock()
        mock_service.resolve_user_permissions = AsyncMock(
            return_value=UserEffectivePermissions(
                user_id="prop4-user",
                app_roles=["default"],
                tools=[],
                models=[],
                quota_tier=None,
                resolved_at="2025-01-01T00:00:00Z",
            )
        )

        with patch("apis.shared.rbac.service._service_instance", mock_service):
            client = TestClient(app, raise_server_exceptions=False)

            # Pick a representative admin endpoint — GET /admin/managed-models
            resp = client.get("/admin/managed-models")

        assert resp.status_code == 403, (
            f"Expected 403 for roles={roles}, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Property 7: Auth enforcement across all protected routes (parametrized)
# Validates: Requirements 17.1, 17.2, 17.3
# ---------------------------------------------------------------------------


class TestAuthEnforcementSweep:
    """Property 7: Auth enforcement across all protected routes.

    For each protected route discovered via introspection, an
    unauthenticated request returns HTTP 401.
    """

    @pytest.mark.parametrize(
        "method,path",
        _PROTECTED_ROUTES,
        ids=[f"{m} {p}" for m, p in _PROTECTED_ROUTES],
    )
    def test_unauthenticated_request_returns_401(self, method, path):
        """Feature: api-route-tests, Property 7: Auth enforcement across all protected routes

        **Validates: Requirements 17.1, 17.2, 17.3**
        """
        # Use a fresh copy of the full app with auth overridden to 401
        app = _FULL_APP
        mock_no_auth(app)

        client = TestClient(app, raise_server_exceptions=False)
        url = _dummy_path(path)

        resp = client.request(method, url)

        assert resp.status_code == 401, (
            f"Expected 401 for {method} {path}, got {resp.status_code}: {resp.text}"
        )

        # Clean up the override so it doesn't leak to other tests
        app.dependency_overrides.pop(get_current_user, None)

    def test_health_endpoint_accessible_without_auth(self):
        """Requirement 17.3: Health endpoint remains accessible without auth.

        **Validates: Requirements 17.3**
        """
        app = _FULL_APP
        # Ensure no auth override is set
        app.dependency_overrides.pop(get_current_user, None)

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
