"""API surface for delegated admin scopes.

PR-3 of ``docs/specs/granular-admin-permissions.md``: the registry endpoint that
feeds the role form's scope picker, and ``adminScopes`` on the permissions
endpoint the SPA already calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.roles.routes import router as roles_router
from apis.app_api.users.routes import router as users_router
from apis.shared.auth import require_admin
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.rbac.admin_scopes import (
    ADMIN_SCOPES,
    NON_DELEGABLE_SCOPES,
)
from apis.shared.rbac.models import UserEffectivePermissions
from tests.conftest import override_admin_auth


def _user() -> User:
    return User(
        email="admin@example.com",
        user_id="admin-1",
        name="Admin",
        roles=["system_admin"],
    )


# ---------------------------------------------------------------------------
# GET /admin/roles/admin-scopes
# ---------------------------------------------------------------------------


@pytest.fixture
def roles_client() -> TestClient:
    app = FastAPI()
    app.include_router(roles_router, prefix="/admin")
    override_admin_auth(app, _user)
    return TestClient(app)


def test_registry_lists_every_scope(roles_client) -> None:
    resp = roles_client.get("/admin/roles/admin-scopes")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == len(ADMIN_SCOPES)
    assert {s["id"] for s in body["scopes"]} == {s.id for s in ADMIN_SCOPES}


def test_registry_marks_non_delegable_scopes(roles_client) -> None:
    """The picker needs to *show* these as unavailable, not omit them."""
    body = roles_client.get("/admin/roles/admin-scopes").json()

    not_delegable = {s["id"] for s in body["scopes"] if not s["delegable"]}
    assert not_delegable == set(NON_DELEGABLE_SCOPES)


def test_registry_entries_carry_display_metadata(roles_client) -> None:
    body = roles_client.get("/admin/roles/admin-scopes").json()

    for scope in body["scopes"]:
        assert scope["label"].strip(), scope["id"]
        assert scope["group"].strip(), scope["id"]
        assert scope["description"].strip(), scope["id"]


def test_registry_route_is_not_shadowed_by_the_role_id_route() -> None:
    """`/admin-scopes` must be declared before `/{role_id}`.

    Single-segment literal paths lose to a single-segment path parameter
    whenever the parameter is declared first, and the symptom is a confusing
    404 (or a lookup for a role named "admin-scopes") rather than an error at
    import time. Asserted on the router's declaration order so a future edit
    that moves the handler fails here instead of in production.
    """
    paths = [getattr(r, "path", "") for r in roles_router.routes]

    assert "/roles/admin-scopes" in paths
    assert paths.index("/roles/admin-scopes") < paths.index("/roles/{role_id}")


def test_registry_requires_full_admin() -> None:
    """Granting scopes is system_admin-only, so reading the registry is too."""
    app = FastAPI()
    app.include_router(roles_router, prefix="/admin")

    def _deny():
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Access denied.")

    app.dependency_overrides[require_admin] = _deny

    resp = TestClient(app).get("/admin/roles/admin-scopes")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /users/me/permissions
# ---------------------------------------------------------------------------


def _permissions(**kwargs) -> UserEffectivePermissions:
    defaults = dict(
        user_id="u-1",
        app_roles=["content_admin"],
        tools=["tool_a"],
        models=["model_a"],
        quota_tier="standard",
        resolved_at="2026-07-27T00:00:00Z",
        skills=["skill_a"],
        admin_scopes=["admin.skills"],
    )
    defaults.update(kwargs)
    return UserEffectivePermissions(**defaults)


def _users_client(permissions: UserEffectivePermissions) -> TestClient:
    app = FastAPI()
    app.include_router(users_router)
    app.dependency_overrides[get_current_user_from_session] = _user

    service = AsyncMock()
    service.resolve_user_permissions = AsyncMock(return_value=permissions)
    patcher = patch(
        "apis.app_api.users.routes.get_app_role_service", return_value=service
    )
    patcher.start()
    client = TestClient(app)
    client._patcher = patcher  # type: ignore[attr-defined]
    return client


def test_permissions_includes_admin_scopes() -> None:
    client = _users_client(_permissions())
    try:
        body = client.get("/users/me/permissions").json()
    finally:
        client._patcher.stop()  # type: ignore[attr-defined]

    assert body["adminScopes"] == ["admin.skills"]


def test_permissions_includes_skills() -> None:
    """Regression: `skills` was on the model for months but never in the response."""
    client = _users_client(_permissions())
    try:
        body = client.get("/users/me/permissions").json()
    finally:
        client._patcher.stop()  # type: ignore[attr-defined]

    assert body["skills"] == ["skill_a"]


def test_permissions_response_is_camel_cased() -> None:
    """The SPA reads camelCase; a snake_case key would silently read undefined."""
    client = _users_client(_permissions())
    try:
        body = client.get("/users/me/permissions").json()
    finally:
        client._patcher.stop()  # type: ignore[attr-defined]

    assert "adminScopes" in body
    assert "admin_scopes" not in body
    assert "appRoles" in body
    assert "quotaTier" in body


def test_permissions_defaults_to_empty_for_a_user_with_no_scopes() -> None:
    client = _users_client(_permissions(admin_scopes=[], skills=[]))
    try:
        body = client.get("/users/me/permissions").json()
    finally:
        client._patcher.stop()  # type: ignore[attr-defined]

    assert body["adminScopes"] == []
    assert body["skills"] == []
