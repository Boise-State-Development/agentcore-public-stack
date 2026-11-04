"""``GET /admin/roles/picker-options`` — the role list a delegated admin's pickers read.

The full role list (``GET /admin/roles/``) is ``system_admin``-only because it carries
every role's grants and admin scopes. Every delegated surface that asks "which roles can
use this?" (models, tools, skills, connectors, quota tiers, announcements, default pins)
reads this narrow list instead, so a scoped admin gets names rather than a 403.

Requests go through the real mounted admin router, not the picker module alone, so the
mount order that keeps ``/roles/{role_id}`` from swallowing the path is exercised too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.app_api.admin.routes import router as admin_router
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.auth.rbac import require_any_admin_scope
from apis.shared.rbac.models import AppRole, UserEffectivePermissions

PATH = "/admin/roles/picker-options"


def _user() -> User:
    return User(
        email="scoped@example.com",
        user_id="scoped-1",
        name="Scoped Admin",
        roles=["SomeGroup"],
    )


def _permissions(app_roles: list[str], admin_scopes: list[str]) -> UserEffectivePermissions:
    return UserEffectivePermissions(
        user_id="scoped-1",
        app_roles=app_roles,
        tools=[],
        models=[],
        quota_tier=None,
        resolved_at="2026-11-04T00:00:00Z",
        admin_scopes=admin_scopes,
    )


def _roles() -> list[AppRole]:
    return [
        AppRole(
            role_id="faculty",
            display_name="Faculty",
            description="Teaching staff",
            granted_tools=["*"],
            granted_models=["us.anthropic.claude-haiku-4-5"],
            priority=10,
        ),
        AppRole(
            role_id="models_admin",
            display_name="Models Admin",
            description="Runs the model catalog",
            granted_admin_scopes=["admin.models"],
            priority=50,
        ),
        AppRole(
            role_id="retired",
            display_name="Retired",
            description="",
            priority=50,
            enabled=False,
        ),
    ]


@pytest.fixture
def client_for():
    """A TestClient whose caller resolves to ``permissions``."""
    patchers = []

    def _make(permissions) -> TestClient:
        app = FastAPI()
        app.include_router(admin_router)
        app.dependency_overrides[get_current_user_from_session] = _user

        role_service = AsyncMock()
        if isinstance(permissions, Exception):
            role_service.resolve_user_permissions = AsyncMock(side_effect=permissions)
        else:
            role_service.resolve_user_permissions = AsyncMock(return_value=permissions)
        admin_service = AsyncMock()
        admin_service.list_roles = AsyncMock(return_value=_roles())
        admin_service.get_role = AsyncMock(return_value=None)

        patchers.extend(
            [
                patch(
                    "apis.shared.rbac.service.get_app_role_service",
                    return_value=role_service,
                ),
                patch(
                    "apis.app_api.admin.roles.picker.get_app_role_admin_service",
                    return_value=admin_service,
                ),
                patch(
                    "apis.app_api.admin.roles.routes.get_app_role_admin_service",
                    return_value=admin_service,
                ),
            ]
        )
        for p in patchers[-3:]:
            p.start()
        return TestClient(app)

    yield _make
    for p in patchers:
        p.stop()


def test_scoped_admin_gets_the_minimal_role_list(client_for) -> None:
    resp = client_for(_permissions(["models_admin"], ["admin.models"])).get(PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    for role in body["roles"]:
        assert set(role) == {"roleId", "displayName", "description", "enabled"}


def test_response_never_carries_grants_or_admin_scopes(client_for) -> None:
    """The whole point of the narrow read: names, never what a role grants."""
    text = client_for(_permissions(["models_admin"], ["admin.tools"])).get(PATH).text

    for leaked in (
        "granted",
        "effectivePermissions",
        "admin.models",
        "jwtRoleMappings",
        "us.anthropic.claude-haiku-4-5",
    ):
        assert leaked not in text


def test_ordered_by_priority_then_id_with_disabled_roles_kept(client_for) -> None:
    body = client_for(_permissions(["models_admin"], ["admin.models"])).get(PATH).json()

    assert [r["roleId"] for r in body["roles"]] == ["models_admin", "retired", "faculty"]
    assert {r["roleId"]: r["enabled"] for r in body["roles"]}["retired"] is False


def test_system_admin_is_allowed(client_for) -> None:
    resp = client_for(_permissions(["system_admin"], [])).get(PATH)

    assert resp.status_code == 200


def test_non_admin_is_refused(client_for) -> None:
    resp = client_for(_permissions(["faculty"], [])).get(PATH)

    assert resp.status_code == 403


def test_a_non_delegable_or_unknown_scope_does_not_count(client_for) -> None:
    """Only a delegable scope evidences delegated admin power."""
    resp = client_for(_permissions(["odd"], ["admin.roles", "admin.nonexistent"])).get(PATH)

    assert resp.status_code == 403


def test_fails_closed_when_resolution_raises(client_for) -> None:
    resp = client_for(RuntimeError("dynamo is down")).get(PATH)

    assert resp.status_code == 403


def test_the_full_role_list_stays_system_admin_only(client_for) -> None:
    """Adding the narrow read must not have widened the full one."""
    resp = client_for(_permissions(["models_admin"], ["admin.models"])).get("/admin/roles/")

    assert resp.status_code == 403


def test_path_is_not_shadowed_by_the_role_id_route() -> None:
    """The picker router must be mounted before the CRUD router's ``/{role_id}``."""
    paths = [getattr(r, "path", "") for r in admin_router.routes]

    assert PATH in paths
    assert paths.index(PATH) < paths.index("/admin/roles/{role_id}")


@pytest.mark.asyncio
async def test_denial_detail_does_not_name_the_permission_model() -> None:
    checker = require_any_admin_scope()
    svc = AsyncMock()
    svc.resolve_user_permissions = AsyncMock(return_value=_permissions(["faculty"], []))

    with patch("apis.shared.rbac.service.get_app_role_service", return_value=svc):
        with pytest.raises(HTTPException) as exc:
            await checker(user=_user())

    assert exc.value.status_code == 403
    assert "scope" not in str(exc.value.detail)
