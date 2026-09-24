"""Admin user search by email shows every profile for the email, live first.

Some emails own a legacy numeric-id PROFILE row beside the Cognito-sub one.
Returning only one of them, arbitrarily, sent support to the stale row, and
the detail page's "Create override" / "Assign tier" act on that row's id.
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.users.routes import get_user_admin_service, require_users_admin, router
from apis.app_api.admin.users.service import UserAdminService
from apis.shared.auth.models import User
from apis.shared.users.models import UserProfile, UserStatus


def _profile(user_id: str, last_login_at: str) -> UserProfile:
    return UserProfile(
        user_id=user_id, email="prof@example.com", name="Prof",
        email_domain="example.com", created_at="2026-03-01T00:00:00Z",
        last_login_at=last_login_at, status=UserStatus.ACTIVE,
    )


def _service(profiles: list) -> UserAdminService:
    repo = AsyncMock()
    type(repo).enabled = PropertyMock(return_value=True)
    repo.get_users_by_email.return_value = profiles
    return UserAdminService(
        user_repository=repo,
        cost_aggregator=MagicMock(),
        quota_resolver=MagicMock(),
        quota_repository=MagicMock(),
    )


@pytest.mark.asyncio
async def test_service_returns_every_profile_in_repository_order():
    service = _service([
        _profile("uuid-live", "2026-09-20T00:00:00Z"),
        _profile("100000001", "2026-04-15T00:00:00Z"),
    ])

    got = await service.search_by_email("prof@example.com")

    assert [u.user_id for u in got] == ["uuid-live", "100000001"]


@pytest.mark.asyncio
async def test_service_no_match_is_empty():
    assert await _service([]).search_by_email("nobody@example.com") == []


def test_route_lists_duplicates_live_first():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_users_admin] = lambda: User(email="admin@example.com", user_id="admin-001", name="Admin", roles=["Admin"])
    app.dependency_overrides[get_user_admin_service] = lambda: _service([
        _profile("uuid-live", "2026-09-20T00:00:00Z"),
        _profile("100000001", "2026-04-15T00:00:00Z"),
    ])

    resp = TestClient(app).get("/users/search", params={"email": "prof@example.com"})

    assert resp.status_code == 200
    assert [u["userId"] for u in resp.json()["users"]] == ["uuid-live", "100000001"]
