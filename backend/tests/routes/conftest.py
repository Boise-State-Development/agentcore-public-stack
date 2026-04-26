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

from apis.shared.auth.dependencies import get_current_user
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
    """Override get_current_user to return the given User.

    Requirement 1.1: authenticated TestClient with Auth_Dependency overridden.
    """
    app.dependency_overrides[get_current_user] = lambda: user


def mock_no_auth(app: FastAPI) -> None:
    """Override get_current_user to raise HTTP 401.

    Requirement 1.2: unauthenticated TestClient behaviour.
    """

    def _raise_401():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    app.dependency_overrides[get_current_user] = _raise_401


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
