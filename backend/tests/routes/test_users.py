"""Tests for users routes.

Endpoints under test:
- GET /users/search?q=...  → 200 with matching users (authenticated)
- GET /users/search?q=...  → 401 for unauthenticated request

Requirements: 11.1, 11.2
"""

from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.users.routes import router, get_user_repository
from apis.shared.users.models import UserProfile, UserStatus, UserListItem
from tests.routes.conftest import mock_auth_user, mock_no_auth, mock_service


SAMPLE_USER_PROFILE = UserProfile(
    user_id="found-001",
    email="alice@example.com",
    name="Alice Smith",
    roles=["User"],
    email_domain="example.com",
    created_at="2025-01-01T00:00:00Z",
    last_login_at="2025-07-01T00:00:00Z",
    status=UserStatus.ACTIVE,
)

SAMPLE_LIST_ITEM = UserListItem(
    user_id="found-002",
    email="bob@example.com",
    name="Bob Jones",
    status=UserStatus.ACTIVE,
    last_login_at="2025-07-01T00:00:00Z",
)


def _make_mock_repo(
    *,
    enabled: bool = True,
    email_result=None,
    list_users_result=None,
):
    """Build an AsyncMock UserRepository with sensible defaults."""
    repo = AsyncMock()
    type(repo).enabled = PropertyMock(return_value=enabled)
    repo.get_user_by_email.return_value = email_result
    repo.list_users_by_status.return_value = (
        list_users_result if list_users_result is not None else ([], None)
    )
    return repo


@pytest.fixture
def app():
    """Minimal FastAPI app mounting only the users router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


# ---------------------------------------------------------------------------
# Requirement 11.1: Users endpoint returns 200 with user profile data
# ---------------------------------------------------------------------------


class TestSearchUsersAuthenticated:
    """GET /users/search returns 200 with user data for authenticated user."""

    def test_returns_200_with_results(self, app, make_user):
        """Req 11.1: Authenticated user gets 200 with matching users."""
        user = make_user()
        mock_auth_user(app, user)

        repo = _make_mock_repo(
            email_result=SAMPLE_USER_PROFILE,
            list_users_result=([SAMPLE_LIST_ITEM], None),
        )
        mock_service(app, get_user_repository, repo)

        client = TestClient(app)
        resp = client.get("/users/search?q=alice")

        assert resp.status_code == 200
        body = resp.json()
        assert "users" in body
        assert len(body["users"]) >= 1

    def test_returns_200_empty_when_repo_disabled(self, app, make_user):
        """Req 11.1: Returns 200 with empty list when repo is disabled."""
        user = make_user()
        mock_auth_user(app, user)

        repo = _make_mock_repo(enabled=False)
        mock_service(app, get_user_repository, repo)

        client = TestClient(app)
        resp = client.get("/users/search?q=test")

        assert resp.status_code == 200
        body = resp.json()
        assert body["users"] == []

    def test_returns_200_empty_for_no_match(self, app, make_user):
        """Req 11.1: Returns 200 with empty list when no users match."""
        user = make_user()
        mock_auth_user(app, user)

        repo = _make_mock_repo(email_result=None, list_users_result=([], None))
        mock_service(app, get_user_repository, repo)

        client = TestClient(app)
        resp = client.get("/users/search?q=nonexistent")

        assert resp.status_code == 200
        body = resp.json()
        assert body["users"] == []


# ---------------------------------------------------------------------------
# Requirement 11.2: Users endpoint returns 401 for unauthenticated request
# ---------------------------------------------------------------------------


class TestSearchUsersUnauthenticated:
    """GET /users/search returns 401 for unauthenticated requests."""

    def test_search_returns_401(self, app, unauthenticated_client):
        """Req 11.2: GET /users/search returns 401 unauthenticated."""
        client = unauthenticated_client(app)
        assert client.get("/users/search?q=test").status_code == 401


# ---------------------------------------------------------------------------
# One email, several PROFILE rows (legacy numeric id + Cognito sub)
# ---------------------------------------------------------------------------


class TestSearchUsersDuplicateEmails:
    """The sharing picker offers each email once, as its live profile."""

    LIVE = UserProfile(
        user_id="uuid-live",
        email="prof@example.com",
        name="Prof Live",
        email_domain="example.com",
        created_at="2026-04-20T00:00:00Z",
        last_login_at="2026-09-20T00:00:00Z",
        status=UserStatus.ACTIVE,
    )

    def test_scan_does_not_re_add_the_exact_match_under_another_id(self, app, make_user):
        mock_auth_user(app, make_user())
        stale = UserListItem(
            user_id="100000001",
            email="prof@example.com",
            name="Prof Stale",
            status=UserStatus.ACTIVE,
            last_login_at="2026-04-15T00:00:00Z",
        )
        repo = _make_mock_repo(email_result=self.LIVE, list_users_result=([stale], None))
        mock_service(app, get_user_repository, repo)

        body = TestClient(app).get("/users/search?q=prof@example.com").json()

        assert [u["userId"] for u in body["users"]] == ["uuid-live"]

    def test_scan_keeps_the_first_row_per_email(self, app, make_user):
        # StatusLoginIndex runs most-recent-login first, so the first row
        # seen for an email is its live one.
        mock_auth_user(app, make_user())
        rows = [
            UserListItem(user_id="uuid-live", email="Prof@example.com", name="Prof",
                         status=UserStatus.ACTIVE, last_login_at="2026-09-20T00:00:00Z"),
            UserListItem(user_id="100000001", email="prof@example.com", name="Prof",
                         status=UserStatus.ACTIVE, last_login_at="2026-04-15T00:00:00Z"),
        ]
        repo = _make_mock_repo(email_result=None, list_users_result=(rows, None))
        mock_service(app, get_user_repository, repo)

        body = TestClient(app).get("/users/search?q=prof").json()

        assert [u["userId"] for u in body["users"]] == ["uuid-live"]
