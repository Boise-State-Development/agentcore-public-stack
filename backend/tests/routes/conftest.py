"""Shared fixtures for route-level API tests.

Provides reusable helpers for:
- Creating mock User objects (make_user factory)
- Overriding auth dependencies (mock_auth_user, mock_no_auth)
- Creating pre-configured TestClient instances (authenticated, unauthenticated, admin)
- Overriding arbitrary FastAPI Depends() (mock_service)

All route test modules under tests/routes/ inherit these fixtures automatically.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6
"""

import os

# Several modules (e.g. managed_models) call boto3.resource() at import time.
# Provide a default region so imports succeed in the test environment.
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from typing import Any, Callable, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User


# ---------------------------------------------------------------------------
# Auto-stub session-metadata pre-stream hook
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_ensure_session_metadata_exists(monkeypatch):
    """The /invocations route calls ensure_session_metadata_exists() before
    streaming, which raises RuntimeError when DYNAMODB_SESSIONS_METADATA_TABLE_NAME
    is unset. Route tests don't exercise metadata persistence, so stub it to a
    no-op that reports "session already exists" (False) — this skips the
    first-turn title-generation branch too.

    Tests that need real metadata behavior should provision the
    `sessions_metadata_table` fixture from tests/shared/conftest.py and
    monkeypatch this back to the real implementation.
    """
    monkeypatch.setattr(
        "apis.inference_api.chat.routes.ensure_session_metadata_exists",
        AsyncMock(return_value=False),
    )


# ---------------------------------------------------------------------------
# Auto-stub the api-converse profile lookup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def stub_api_key_user_profile(monkeypatch):
    """Give /chat/api-converse a Users-table profile to hydrate roles from.

    The handler reads the key owner's real roles per request and fails closed
    when no profile row exists (an API key stores no roles of its own). Route
    tests that only patch ``_validate_api_key`` would otherwise all 401.

    Tests exercising hydration itself — the fail-closed path, or which roles
    reach RBAC — should re-patch ``converse_routes.get_user_repository``
    directly rather than rely on this default.
    """
    from apis.shared.users import UserProfile

    profile = UserProfile(
        userId="user-001",
        email="test@example.com",
        name="Test User",
        roles=["Staff"],
        emailDomain="example.com",
        createdAt="2026-01-01T00:00:00Z",
        lastLoginAt="2026-01-01T00:00:00Z",
    )
    repo = AsyncMock()
    repo.get_user = AsyncMock(return_value=profile)
    monkeypatch.setattr(
        "apis.app_api.chat.converse_routes.get_user_repository",
        lambda: repo,
    )


# ---------------------------------------------------------------------------
# Requirement 1.3: User factory fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def make_user():
    """Factory fixture that creates User objects with sensible defaults.

    Usage:
        user = make_user()
        admin = make_user(roles=["Admin"], email="admin@example.com")
    """

    def _make_user(
        email: str = "test@example.com",
        user_id: str = "user-001",
        name: str = "Test User",
        roles: Optional[List[str]] = None,
        picture: Optional[str] = None,
        raw_token: Optional[str] = None,
    ) -> User:
        return User(
            email=email,
            user_id=user_id,
            name=name,
            roles=roles if roles is not None else ["User"],
            picture=picture,
            raw_token=raw_token,
        )

    return _make_user


# ---------------------------------------------------------------------------
# Auth override helpers
# ---------------------------------------------------------------------------


def mock_auth_user(app: FastAPI, user: User) -> None:
    """Override the auth dependency to return the given User.

    Requirement 1.1: authenticated TestClient with Auth_Dependency overridden.

    Overrides `get_current_user_from_session` (cookie auth — the only
    user-facing auth dependency in `app_api/` after the BFF migration).
    """
    app.dependency_overrides[get_current_user_from_session] = lambda: user


def mock_no_auth(app: FastAPI) -> None:
    """Override the auth dependency to raise HTTP 401.

    Requirement 1.2: unauthenticated TestClient behaviour.
    """

    def _raise_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    app.dependency_overrides[get_current_user_from_session] = _raise_401


# ---------------------------------------------------------------------------
# Service mock helper
# ---------------------------------------------------------------------------


def mock_service(app: FastAPI, dependency: Callable, mock: Any) -> None:
    """Override any FastAPI Depends() with a mock.

    Requirement 1.5: mocked external service dependencies.

    Usage:
        mock_service(app, get_file_upload_service, my_mock)
    """
    app.dependency_overrides[dependency] = lambda: mock


# ---------------------------------------------------------------------------
# Requirement 1.1: Authenticated client fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def authenticated_client(make_user):
    """Factory fixture returning a TestClient with auth overridden.

    Usage:
        client = authenticated_client(app)
        client = authenticated_client(app, make_user(roles=["Admin"]))
    """

    def _authenticated_client(
        app: FastAPI, user: Optional[User] = None
    ) -> TestClient:
        if user is None:
            user = make_user()
        mock_auth_user(app, user)
        return TestClient(app)

    return _authenticated_client


# ---------------------------------------------------------------------------
# Requirement 1.2: Unauthenticated client fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def unauthenticated_client():
    """Factory fixture returning a TestClient with auth raising 401.

    Usage:
        client = unauthenticated_client(app)
    """

    def _unauthenticated_client(app: FastAPI) -> TestClient:
        mock_no_auth(app)
        return TestClient(app)

    return _unauthenticated_client


# ---------------------------------------------------------------------------
# Requirement 1.4: Admin client fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(make_user):
    """Factory fixture returning a TestClient with an Admin-role user.

    Usage:
        client = admin_client(app)
    """

    def _admin_client(app: FastAPI) -> TestClient:
        admin_user = make_user(
            email="admin@example.com",
            user_id="admin-001",
            name="Admin User",
            roles=["Admin"],
        )
        mock_auth_user(app, admin_user)
        return TestClient(app)

    return _admin_client


@pytest.fixture(autouse=True)
def _no_live_infrastructure_reads(monkeypatch):
    """Stub the infrastructure lookups the route paths make behind whatever the
    test itself mocked.

    Every one of these is fail-open — the route keeps serving if the table is
    unreachable — so a live DynamoDB call from a unit test was swallowed and the
    assertion passed anyway. They are stubbed to benign defaults here rather than
    per file because the same handful recur across the whole directory. A test
    that cares about one of them patches it itself, and that patch wins (it is
    applied inside this one).

    See the off-box socket guard in ``tests/conftest.py``.
    """
    from unittest.mock import AsyncMock as _AsyncMock, patch as _patch

    stubs = [
        # Inference invocation path.
        ("apis.inference_api.chat.system_prompt_resolver.get_session_metadata", None),
        ("apis.shared.files.document_read.session_has_documents", False),
        ("apis.shared.files.document_read.session_has_tabular_files", False),
        # The tool-catalog snapshot behind `freshness.get_always_on_tool_ids`.
        # The invocation path resolves the admin-pinned tool set every turn, so
        # it reads the catalog where it previously did not — and the resolver is
        # fail-open, which is precisely the case this fixture's docstring warns
        # about: swallowed error, assertion still passes, real socket still
        # opened. An empty catalog pins nothing, which is the default behaviour
        # a route test should see.
        ("apis.shared.tools.repository.ToolCatalogRepository.list_tools", []),
        # Converse path: model routing, the rate-limit window, and the quota
        # override lookup. The 429 test drives quota through `get_quota_checker`,
        # which is a different seam, so stubbing these does not weaken it.
        ("apis.shared.models.managed_models.list_managed_models", []),
        ("apis.shared.rate_limit.RateLimiter.check_rate_limit", True),
        ("agents.main_agent.quota.repository.QuotaRepository.get_active_override", None),
        ("agents.main_agent.quota.repository.QuotaRepository.query_user_assignment", None),
        ("agents.main_agent.quota.repository.QuotaRepository.query_role_assignments", []),
        ("agents.main_agent.quota.repository.QuotaRepository.list_assignments_by_type", []),
        # RBAC role resolution. Tests that care about authorization override the
        # auth dependency itself, which never reaches this.
        ("apis.shared.rbac.repository.AppRoleRepository.get_roles_for_jwt_role", []),
        ("apis.shared.rbac.repository.AppRoleRepository.get_role", None),
        # The direct user grant read runs alongside the role lookups on every
        # cold resolve, so it needs the same stub or it opens a real socket.
        ("apis.shared.rbac.repository.AppRoleRepository.get_user_grant", None),
        # Agent-detail label resolution.
        ("apis.shared.memory.repository.MemorySpaceRepository.get_space", None),
        # Session delete cascades into artifact share cleanup. Whether it fires
        # depends on whether an earlier test left artifacts "configured", which
        # is why these passed alone and only failed in full-suite order.
        ("apis.app_api.artifacts.service.ArtifactShareService.delete_for_session", 0),
    ]
    patchers = [_patch(target, new=_AsyncMock(return_value=value)) for target, value in stubs]
    for patcher in patchers:
        patcher.start()
    try:
        yield
    finally:
        for patcher in reversed(patchers):
            patcher.stop()
