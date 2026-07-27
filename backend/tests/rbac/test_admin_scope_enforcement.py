"""Enforcement of delegated admin scopes.

PR-2 of ``docs/specs/granular-admin-permissions.md``. Covers the authorization
dependency and the write-through guard that stops a delegated admin from
reaching an admin-bearing role through a resource surface.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from apis.shared.auth.models import User
from apis.shared.auth.rbac import require_admin_scope
from apis.shared.rbac.admin_service import AppRoleAdminService
from apis.shared.rbac.models import AppRoleUpdate, UserEffectivePermissions
from apis.shared.rbac.role_constraints import RoleMutationForbidden


def _user(user_id: str = "u-1") -> User:
    return User(
        email=f"{user_id}@example.com",
        user_id=user_id,
        name=user_id,
        roles=["SomeGroup"],
    )


def _permissions(app_roles: list[str], admin_scopes: list[str]) -> UserEffectivePermissions:
    return UserEffectivePermissions(
        user_id="u-1",
        app_roles=app_roles,
        tools=[],
        models=[],
        quota_tier=None,
        resolved_at="2026-07-27T00:00:00Z",
        admin_scopes=admin_scopes,
    )


def _service_returning(permissions) -> AsyncMock:
    svc = AsyncMock()
    if isinstance(permissions, Exception):
        svc.resolve_user_permissions = AsyncMock(side_effect=permissions)
    else:
        svc.resolve_user_permissions = AsyncMock(return_value=permissions)
    return svc


# ---------------------------------------------------------------------------
# require_admin_scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_holder_is_authorized() -> None:
    checker = require_admin_scope("admin.tools")
    svc = _service_returning(_permissions(["content_admin"], ["admin.tools"]))

    with patch("apis.shared.rbac.service.get_app_role_service", return_value=svc):
        assert await checker(user=_user()) is not None


@pytest.mark.asyncio
async def test_system_admin_satisfies_every_scope() -> None:
    """The superuser holds no scopes explicitly and must still pass."""
    svc = _service_returning(_permissions(["system_admin"], []))

    with patch("apis.shared.rbac.service.get_app_role_service", return_value=svc):
        for scope in ("admin.tools", "admin.costs", "admin.marketplace"):
            checker = require_admin_scope(scope)
            assert await checker(user=_user()) is not None


@pytest.mark.asyncio
async def test_holder_of_a_different_scope_is_denied() -> None:
    checker = require_admin_scope("admin.tools")
    svc = _service_returning(_permissions(["content_admin"], ["admin.skills"]))

    with patch("apis.shared.rbac.service.get_app_role_service", return_value=svc):
        with pytest.raises(HTTPException) as exc:
            await checker(user=_user())

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_user_with_no_scopes_is_denied() -> None:
    checker = require_admin_scope("admin.tools")
    svc = _service_returning(_permissions(["faculty"], []))

    with patch("apis.shared.rbac.service.get_app_role_service", return_value=svc):
        with pytest.raises(HTTPException) as exc:
            await checker(user=_user())

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_denial_detail_does_not_name_the_scope() -> None:
    """Don't hand an unauthorized caller the vocabulary of the permission model."""
    checker = require_admin_scope("admin.tools")
    svc = _service_returning(_permissions(["faculty"], []))

    with patch("apis.shared.rbac.service.get_app_role_service", return_value=svc):
        with pytest.raises(HTTPException) as exc:
            await checker(user=_user())

    assert "admin.tools" not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_fails_closed_when_resolution_raises() -> None:
    checker = require_admin_scope("admin.tools")
    svc = _service_returning(RuntimeError("dynamo is down"))

    with patch("apis.shared.rbac.service.get_app_role_service", return_value=svc):
        with pytest.raises(HTTPException) as exc:
            await checker(user=_user())

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_wildcard_scope_grants_nothing() -> None:
    """A stray `"*"` in a role's scopes must not act as a superuser grant."""
    checker = require_admin_scope("admin.tools")
    svc = _service_returning(_permissions(["odd_role"], ["*"]))

    with patch("apis.shared.rbac.service.get_app_role_service", return_value=svc):
        with pytest.raises(HTTPException) as exc:
            await checker(user=_user())

    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Write-through guard (spec I3)
# ---------------------------------------------------------------------------


@pytest.fixture
def service(mock_app_role_repo, mock_app_role_cache) -> AppRoleAdminService:
    return AppRoleAdminService(repository=mock_app_role_repo, cache=mock_app_role_cache)


def _wire_roles(mock_app_role_repo, roles: dict, actor_role_id: str) -> None:
    """Serve `roles` by id, and resolve the actor's JWT group to `actor_role_id`.

    Exercises the real resolution path — the guard shares this service's
    injected repository rather than reaching for the global singleton, so a
    unit test can wire the actor's identity the same way production resolves it.
    """
    mock_app_role_repo.get_role.side_effect = lambda rid: roles.get(rid)
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda jwt: (
        [actor_role_id] if jwt == "SomeGroup" else []
    )


@pytest.mark.asyncio
async def test_delegated_admin_cannot_grant_into_a_scope_bearing_role(
    service, mock_app_role_repo, make_app_role
) -> None:
    """The escalation this guard exists for.

    A delegated `admin.tools` holder uses the tool page's role picker to add a
    tool to a role that carries admin scopes. Granting tools is their job;
    touching an admin-bearing role is not.
    """
    _wire_roles(
        mock_app_role_repo,
        {
            "content_admin": make_app_role(
                role_id="content_admin", granted_admin_scopes=["admin.skills"]
            ),
            "tool_admin": make_app_role(
                role_id="tool_admin", admin_scopes=["admin.tools"]
            ),
        },
        actor_role_id="tool_admin",
    )

    with pytest.raises(RoleMutationForbidden):
        await service.update_role(
            "content_admin",
            AppRoleUpdate(granted_tools=["some_tool"]),
            _user(),
        )


@pytest.mark.asyncio
async def test_delegated_admin_cannot_grant_into_a_protected_role(
    service, mock_app_role_repo, make_app_role
) -> None:
    _wire_roles(
        mock_app_role_repo,
        {
            "system_admin": make_app_role(role_id="system_admin", is_system_role=True),
            "tool_admin": make_app_role(
                role_id="tool_admin", admin_scopes=["admin.tools"]
            ),
        },
        actor_role_id="tool_admin",
    )

    with pytest.raises(RoleMutationForbidden):
        await service.update_role(
            "system_admin",
            AppRoleUpdate(granted_tools=["some_tool"]),
            _user(),
        )


@pytest.mark.asyncio
async def test_delegated_admin_may_still_grant_into_an_ordinary_role(
    service, mock_app_role_repo, make_app_role
) -> None:
    """The legitimate case must keep working — this is a tools admin's job."""
    _wire_roles(
        mock_app_role_repo,
        {
            "faculty": make_app_role(role_id="faculty"),
            "tool_admin": make_app_role(
                role_id="tool_admin", admin_scopes=["admin.tools"]
            ),
        },
        actor_role_id="tool_admin",
    )
    mock_app_role_repo.update_role.side_effect = lambda r: r

    updated = await service.update_role(
        "faculty",
        AppRoleUpdate(granted_tools=["some_tool"]),
        _user(),
    )

    assert updated is not None
    assert "some_tool" in updated.granted_tools


@pytest.mark.asyncio
async def test_system_admin_may_mutate_an_admin_bearing_role(
    service, mock_app_role_repo, make_app_role
) -> None:
    """The guard must not lock the roles admin out of its own job."""
    _wire_roles(
        mock_app_role_repo,
        {
            "content_admin": make_app_role(
                role_id="content_admin", granted_admin_scopes=["admin.skills"]
            ),
            "system_admin": make_app_role(role_id="system_admin", is_system_role=True),
        },
        actor_role_id="system_admin",
    )
    mock_app_role_repo.update_role.side_effect = lambda r: r

    updated = await service.update_role(
        "content_admin",
        AppRoleUpdate(granted_tools=["some_tool"]),
        _user(),
    )

    assert updated is not None


@pytest.mark.asyncio
async def test_write_through_guard_fails_closed(
    service, mock_app_role_repo, make_app_role
) -> None:
    mock_app_role_repo.get_role.side_effect = lambda rid: (
        make_app_role(role_id="content_admin", granted_admin_scopes=["admin.skills"])
        if rid == "content_admin"
        else None
    )
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = RuntimeError("dynamo is down")

    with pytest.raises(RoleMutationForbidden):
        await service.update_role(
            "content_admin",
            AppRoleUpdate(granted_tools=["some_tool"]),
            _user(),
        )


@pytest.mark.asyncio
async def test_ordinary_role_mutation_skips_permission_resolution(
    service, mock_app_role_repo, make_app_role
) -> None:
    """The common path must not pay for an extra permission lookup."""
    mock_app_role_repo.get_role.side_effect = lambda rid: make_app_role(role_id="faculty")
    mock_app_role_repo.update_role.side_effect = lambda r: r

    await service.update_role("faculty", AppRoleUpdate(granted_tools=["t"]), _user())

    mock_app_role_repo.get_roles_for_jwt_role.assert_not_called()
