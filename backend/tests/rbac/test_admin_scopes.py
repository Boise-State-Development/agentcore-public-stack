"""Delegated admin scopes — registry integrity, grant validation, and resolution.

PR-1 of the granular-admin-permissions epic (``docs/specs/granular-admin-permissions.md``).
Nothing *authorizes* on admin scopes yet; these tests cover the data path —
that a scope can be granted, survives a round trip, merges the way the spec
says, and cannot be used to climb back to full admin.
"""

from __future__ import annotations

import pytest

from apis.shared.auth.models import User
from apis.shared.rbac.admin_scopes import (
    ADMIN_SCOPES,
    ADMIN_SCOPES_BY_ID,
    DELEGABLE_SCOPES,
    NON_DELEGABLE_SCOPES,
    is_delegable,
    is_known_scope,
    normalize_scopes,
)
from apis.shared.rbac.admin_service import AppRoleAdminService
from apis.shared.rbac.models import (
    AppRole,
    AppRoleCreate,
    AppRoleResponse,
    AppRoleUpdate,
    EffectivePermissions,
)
from apis.shared.rbac.role_constraints import RoleConstraintError, validate_admin_scopes
from apis.shared.rbac.service import AppRoleService


@pytest.fixture
def admin() -> User:
    return User(
        email="admin@example.com",
        user_id="admin-1",
        name="Admin User",
        roles=["Admin"],
    )


@pytest.fixture
def service(mock_app_role_repo, mock_app_role_cache) -> AppRoleAdminService:
    return AppRoleAdminService(repository=mock_app_role_repo, cache=mock_app_role_cache)


# ---------------------------------------------------------------------------
# Registry integrity
# ---------------------------------------------------------------------------


def test_scope_ids_are_unique() -> None:
    ids = [s.id for s in ADMIN_SCOPES]
    assert len(ids) == len(set(ids))


def test_every_scope_id_is_namespaced_and_lowercase() -> None:
    for scope in ADMIN_SCOPES:
        assert scope.id.startswith("admin."), scope.id
        assert scope.id == scope.id.lower(), scope.id


def test_every_scope_has_label_group_and_description() -> None:
    for scope in ADMIN_SCOPES:
        assert scope.label.strip(), scope.id
        assert scope.group.strip(), scope.id
        assert scope.description.strip(), scope.id


def test_roles_and_auth_providers_are_non_delegable() -> None:
    """I1 — the two escalation paths back to full admin.

    ``admin.roles`` is obvious. ``admin.auth_providers`` is the one that gets
    argued about: role resolution starts from JWT claims, so controlling IdP
    attribute mapping controls which AppRoles resolve at all.
    """
    assert NON_DELEGABLE_SCOPES == {"admin.roles", "admin.auth_providers"}


def test_delegable_and_non_delegable_partition_the_registry() -> None:
    assert DELEGABLE_SCOPES.isdisjoint(NON_DELEGABLE_SCOPES)
    assert DELEGABLE_SCOPES | NON_DELEGABLE_SCOPES == set(ADMIN_SCOPES_BY_ID)


def test_wildcard_is_not_a_scope() -> None:
    """There is deliberately no `"*"` on this axis — full admin is a role."""
    assert not is_known_scope("*")
    assert not is_delegable("*")


def test_normalize_scopes_dedupes_and_sorts() -> None:
    assert normalize_scopes(["admin.tools", "admin.costs", "admin.tools"]) == [
        "admin.costs",
        "admin.tools",
    ]
    assert normalize_scopes(None) == []
    assert normalize_scopes([]) == []


# ---------------------------------------------------------------------------
# Grant validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", sorted(DELEGABLE_SCOPES))
def test_validate_accepts_every_delegable_scope(scope: str) -> None:
    validate_admin_scopes([scope])


@pytest.mark.parametrize("scope", sorted(NON_DELEGABLE_SCOPES))
def test_validate_rejects_non_delegable_scope(scope: str) -> None:
    with pytest.raises(RoleConstraintError, match="cannot be delegated"):
        validate_admin_scopes([scope])


@pytest.mark.parametrize(
    "scope", ["admin.nonexistent", "admin.role", "tools", "admin.tools.write"]
)
def test_validate_rejects_unknown_scope(scope: str) -> None:
    with pytest.raises(RoleConstraintError):
        validate_admin_scopes([scope])


@pytest.mark.parametrize(
    "scope",
    [
        "*",
        "",
        "admin tools",
        "<script>",
        "ADMIN.TOOLS",
        "admin.",
        ".tools",
        "admin." + "x" * 64,  # length-bounded so it can't reach the error message
        "a.b.c.d.e.f",
    ],
)
def test_validate_rejects_malformed_scope(scope: str) -> None:
    with pytest.raises(RoleConstraintError, match="Invalid admin scope"):
        validate_admin_scopes([scope])


def test_malformed_scope_is_not_echoed_into_the_error(caplog) -> None:
    """The generic message is the point — a rejected payload must not travel."""
    payload = "<img src=x onerror=alert(1)>"

    with pytest.raises(RoleConstraintError) as exc:
        validate_admin_scopes([payload])

    assert payload not in str(exc.value)


@pytest.mark.parametrize("value", [None, 42, ["nested"], {"a": 1}])
def test_validate_rejects_non_string_entries(value) -> None:
    with pytest.raises(RoleConstraintError, match="Invalid admin scope"):
        validate_admin_scopes([value])


def test_validate_allows_empty() -> None:
    validate_admin_scopes(None)
    validate_admin_scopes([])


# ---------------------------------------------------------------------------
# Grants through the admin service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_role_persists_normalized_admin_scopes(
    service, mock_app_role_repo, admin
) -> None:
    mock_app_role_repo.create_role.side_effect = lambda role: role

    created = await service.create_role(
        AppRoleCreate(
            role_id="content_admin",
            display_name="Content Admin",
            granted_admin_scopes=["admin.skills", "admin.costs", "admin.skills"],
        ),
        admin,
    )

    assert created.granted_admin_scopes == ["admin.costs", "admin.skills"]
    assert created.effective_permissions.admin_scopes == ["admin.costs", "admin.skills"]


@pytest.mark.asyncio
async def test_create_role_rejects_non_delegable_scope(service, admin) -> None:
    with pytest.raises(RoleConstraintError):
        await service.create_role(
            AppRoleCreate(
                role_id="sneaky",
                display_name="Sneaky",
                granted_admin_scopes=["admin.roles"],
            ),
            admin,
        )


@pytest.mark.asyncio
async def test_update_role_rejects_non_delegable_scope(
    service, mock_app_role_repo, make_app_role, admin
) -> None:
    role = make_app_role(role_id="content_admin", granted_admin_scopes=["admin.skills"])
    mock_app_role_repo.get_role.return_value = role

    with pytest.raises(RoleConstraintError):
        await service.update_role(
            "content_admin",
            AppRoleUpdate(granted_admin_scopes=["admin.skills", "admin.auth_providers"]),
            admin,
        )


@pytest.mark.asyncio
async def test_system_admin_update_strips_admin_scopes(
    service, mock_app_role_repo, make_app_role, admin
) -> None:
    """system_admin holds every scope implicitly; the field is not writable on it.

    The existing protected-field stripping in ``update_role`` already covers
    this — granted_admin_scopes is simply not in ``allowed_fields``. Asserted
    here so a future widening of that set has to consciously break this test.
    """
    role = make_app_role(
        role_id="system_admin",
        is_system_role=True,
        jwt_role_mappings=["system_admin"],
    )
    mock_app_role_repo.get_role.return_value = role
    mock_app_role_repo.update_role.side_effect = lambda r: r

    updated = await service.update_role(
        "system_admin",
        AppRoleUpdate(display_name="Sys Admin", granted_admin_scopes=["admin.tools"]),
        admin,
    )

    assert updated.granted_admin_scopes == []


# ---------------------------------------------------------------------------
# I4 — admin scopes do not inherit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_scopes_are_not_inherited_from_parent(
    service, mock_app_role_repo, make_app_role, admin
) -> None:
    """Tools inherit; admin power does not.

    A child role that inherits from a scope-bearing parent picks up the
    parent's *tools* but none of its admin scopes.
    """
    parent = make_app_role(
        role_id="content_admin",
        granted_tools=["tool_a"],
        granted_admin_scopes=["admin.skills"],
    )
    mock_app_role_repo.get_role.return_value = parent
    mock_app_role_repo.create_role.side_effect = lambda role: role

    child = await service.create_role(
        AppRoleCreate(
            role_id="child_role",
            display_name="Child",
            inherits_from=["content_admin"],
            granted_tools=["tool_b"],
        ),
        admin,
    )

    assert set(child.effective_permissions.tools) == {"tool_a", "tool_b"}
    assert child.effective_permissions.admin_scopes == []


# ---------------------------------------------------------------------------
# Runtime resolution
# ---------------------------------------------------------------------------


@pytest.fixture
def user() -> User:
    return User(
        email="test@example.com",
        user_id="user-1",
        name="Test User",
        roles=["Editor", "Viewer"],
    )


@pytest.fixture
def runtime_service(mock_app_role_repo, mock_app_role_cache) -> AppRoleService:
    return AppRoleService(repository=mock_app_role_repo, cache=mock_app_role_cache)


def _wire(mock_app_role_repo, roles: dict[str, AppRole]) -> None:
    """Map the `user` fixture's two JWT roles onto the given AppRoles."""
    jwt_to_role = dict(zip(["Editor", "Viewer"], roles))
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: (
        [jwt_to_role[r]] if r in jwt_to_role else []
    )
    mock_app_role_repo.get_role.side_effect = lambda rid: roles.get(rid)


@pytest.mark.asyncio
async def test_merge_unions_admin_scopes_across_roles(
    runtime_service, mock_app_role_repo, make_app_role, user
) -> None:
    _wire(
        mock_app_role_repo,
        {
            "editor": make_app_role(role_id="editor", admin_scopes=["admin.tools"]),
            "viewer": make_app_role(
                role_id="viewer", admin_scopes=["admin.skills", "admin.tools"]
            ),
        },
    )

    perms = await runtime_service.resolve_user_permissions(user)

    assert perms.admin_scopes == ["admin.skills", "admin.tools"]


@pytest.mark.asyncio
async def test_merge_is_sorted_and_deterministic(
    runtime_service, mock_app_role_repo, make_app_role, user
) -> None:
    _wire(
        mock_app_role_repo,
        {
            "editor": make_app_role(
                role_id="editor",
                admin_scopes=["admin.tools", "admin.costs", "admin.skills"],
            )
        },
    )

    perms = await runtime_service.resolve_user_permissions(user)

    assert perms.admin_scopes == ["admin.costs", "admin.skills", "admin.tools"]


@pytest.mark.asyncio
async def test_merge_does_not_expand_a_stray_wildcard(
    runtime_service, mock_app_role_repo, make_app_role, user
) -> None:
    """A `"*"` that somehow reached the stored list grants nothing.

    Unlike tools/models/skills, this axis has no wildcard collapse, so a stray
    value is carried through as an unknown scope that matches no route rather
    than silently granting every admin surface.
    """
    _wire(
        mock_app_role_repo,
        {"editor": make_app_role(role_id="editor", admin_scopes=["*"])},
    )

    perms = await runtime_service.resolve_user_permissions(user)

    assert perms.admin_scopes == ["*"]
    assert not any(is_known_scope(s) for s in perms.admin_scopes)


@pytest.mark.asyncio
async def test_user_matching_no_roles_gets_no_scopes(
    runtime_service, mock_app_role_repo, make_app_role, user
) -> None:
    """The `default` fallback role carries no admin scopes."""
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: []
    mock_app_role_repo.get_role.side_effect = lambda rid: (
        make_app_role(role_id="default") if rid == "default" else None
    )

    perms = await runtime_service.resolve_user_permissions(user)

    assert perms.admin_scopes == []


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


def test_app_role_dict_round_trip_preserves_admin_scopes() -> None:
    role = AppRole(
        role_id="content_admin",
        display_name="Content Admin",
        description="",
        granted_admin_scopes=["admin.skills"],
        effective_permissions=EffectivePermissions(admin_scopes=["admin.skills"]),
    )

    restored = AppRole.from_dict(role.to_dict())

    assert restored.granted_admin_scopes == ["admin.skills"]
    assert restored.effective_permissions.admin_scopes == ["admin.skills"]


def test_role_persisted_before_this_axis_defaults_to_empty() -> None:
    """A DEFINITION item written before admin scopes existed must still load."""
    legacy = {
        "roleId": "faculty",
        "displayName": "Faculty",
        "description": "",
        "grantedTools": ["tool_a"],
        "effectivePermissions": {"tools": ["tool_a"], "models": [], "skills": []},
    }

    role = AppRole.from_dict(legacy)

    assert role.granted_admin_scopes == []
    assert role.effective_permissions.admin_scopes == []


def test_response_model_carries_admin_scopes(make_app_role) -> None:
    """Guards the `skills`-omission bug: written on create, dropped on read."""
    role = make_app_role(role_id="content_admin", granted_admin_scopes=["admin.skills"])

    response = AppRoleResponse.from_app_role(role)

    assert response.granted_admin_scopes == ["admin.skills"]
    assert response.effective_permissions.admin_scopes == ["admin.skills"]
    assert response.model_dump(by_alias=True)["grantedAdminScopes"] == ["admin.skills"]
