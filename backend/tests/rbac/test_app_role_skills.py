"""Unit tests for skill RBAC (PR-2): granted_skills + EffectivePermissions.skills.

Mirrors the tool/model coverage in test_app_role_service.py and
test_app_role_admin_service.py:
- skills union merge across roles (service)
- wildcard skills (service)
- can_access_skill: wildcard / match / no-match (service)
- only enabled roles contribute skills (service)
- inheritance merge of granted_skills (admin _compute_effective_permissions)
- add_skill_to_role / remove_skill_from_role (admin)
- get_roles_granting_skill: direct + inherited (admin reverse lookup)
"""

from unittest.mock import AsyncMock

import pytest

from apis.shared.auth.models import User
from apis.shared.rbac.admin_service import AppRoleAdminService
from apis.shared.rbac.models import AppRoleCreate
from apis.shared.rbac.service import AppRoleService


@pytest.fixture
def user():
    return User(
        email="test@example.com",
        user_id="user-1",
        name="Test User",
        roles=["Editor", "Viewer"],
    )


@pytest.fixture
def admin():
    return User(
        email="admin@example.com",
        user_id="admin-1",
        name="Admin User",
        roles=["Admin"],
    )


@pytest.fixture
def service(mock_app_role_repo, mock_app_role_cache):
    return AppRoleService(repository=mock_app_role_repo, cache=mock_app_role_cache)


@pytest.fixture
def admin_service(mock_app_role_repo, mock_app_role_cache):
    return AppRoleAdminService(repository=mock_app_role_repo, cache=mock_app_role_cache)


def _wire_two_roles(mock_app_role_repo, role_a, role_b):
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": ["viewer"],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "editor": role_a,
        "viewer": role_b,
    }.get(rid)


# ── service: resolution ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skills_union_merge(service, mock_app_role_repo, make_app_role, user):
    """Merging multiple AppRoles produces the union of all skills."""
    role_a = make_app_role(role_id="editor", skills=["skill_a", "skill_b"], priority=1)
    role_b = make_app_role(role_id="viewer", skills=["skill_b", "skill_c"], priority=0)
    _wire_two_roles(mock_app_role_repo, role_a, role_b)

    perms = await service.resolve_user_permissions(user)

    assert set(perms.skills) == {"skill_a", "skill_b", "skill_c"}


@pytest.mark.asyncio
async def test_wildcard_in_skills(service, mock_app_role_repo, make_app_role, user):
    """When any role has '*' in skills, the merged skills contain '*'."""
    role_a = make_app_role(role_id="editor", skills=["*"], priority=1)
    role_b = make_app_role(role_id="viewer", skills=["skill_c"], priority=0)
    _wire_two_roles(mock_app_role_repo, role_a, role_b)

    perms = await service.resolve_user_permissions(user)

    assert "*" in perms.skills


@pytest.mark.asyncio
async def test_can_access_skill_with_wildcard(service, mock_app_role_repo, make_app_role, user):
    role_a = make_app_role(role_id="editor", skills=["*"], priority=1)
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": [],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {"editor": role_a}.get(rid)

    assert await service.can_access_skill(user, "any_skill") is True


@pytest.mark.asyncio
async def test_can_access_skill_with_match(service, mock_app_role_repo, make_app_role, user):
    role_a = make_app_role(role_id="editor", skills=["pdf_workflows"], priority=1)
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": [],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {"editor": role_a}.get(rid)

    assert await service.can_access_skill(user, "pdf_workflows") is True


@pytest.mark.asyncio
async def test_can_access_skill_with_no_match(service, mock_app_role_repo, make_app_role, user):
    role_a = make_app_role(role_id="editor", skills=["pdf_workflows"], priority=1)
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": [],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {"editor": role_a}.get(rid)

    assert await service.can_access_skill(user, "other_skill") is False


@pytest.mark.asyncio
async def test_only_enabled_roles_contribute_skills(service, mock_app_role_repo, make_app_role, user):
    role_enabled = make_app_role(role_id="editor", skills=["skill_a"], enabled=True)
    role_disabled = make_app_role(role_id="viewer", skills=["skill_secret"], enabled=False)
    _wire_two_roles(mock_app_role_repo, role_enabled, role_disabled)

    perms = await service.resolve_user_permissions(user)

    assert "skill_a" in perms.skills
    assert "skill_secret" not in perms.skills


@pytest.mark.asyncio
async def test_get_accessible_skills(service, mock_app_role_repo, make_app_role, user):
    role_a = make_app_role(role_id="editor", skills=["skill_a", "skill_b"], priority=1)
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: {
        "Editor": ["editor"],
        "Viewer": [],
    }.get(r, [])
    mock_app_role_repo.get_role.side_effect = lambda rid: {"editor": role_a}.get(rid)

    assert set(await service.get_accessible_skills(user)) == {"skill_a", "skill_b"}


# ── admin: effective-permission computation + inheritance ────────────────────


@pytest.mark.asyncio
async def test_create_role_computes_skills(admin_service, mock_app_role_repo, admin):
    role_data = AppRoleCreate(
        role_id="editor",
        display_name="Editor",
        granted_skills=["skill_a", "skill_b"],
    )
    mock_app_role_repo.get_role.return_value = None

    async def capture_create(role):
        return role
    mock_app_role_repo.create_role.side_effect = capture_create

    result = await admin_service.create_role(role_data, admin)

    assert set(result.granted_skills) == {"skill_a", "skill_b"}
    assert set(result.effective_permissions.skills) == {"skill_a", "skill_b"}


@pytest.mark.asyncio
async def test_inheritance_merges_granted_skills(admin_service, mock_app_role_repo, make_app_role, admin):
    parent_role = make_app_role(
        role_id="parent_role",
        granted_skills=["parent_skill_a", "parent_skill_b"],
        enabled=True,
    )
    role_data = AppRoleCreate(
        role_id="child_role",
        display_name="Child",
        inherits_from=["parent_role"],
        granted_skills=["child_skill"],
    )
    mock_app_role_repo.get_role.return_value = parent_role

    async def capture_create(role):
        return role
    mock_app_role_repo.create_role.side_effect = capture_create

    result = await admin_service.create_role(role_data, admin)

    assert set(result.effective_permissions.skills) == {
        "child_skill", "parent_skill_a", "parent_skill_b"
    }


# ── admin: add / remove skill ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_skill_to_role(admin_service, mock_app_role_repo, make_app_role, admin):
    role = make_app_role(role_id="editor", granted_skills=["skill_a"], skills=["skill_a"])
    mock_app_role_repo.get_role.return_value = role

    async def capture_update(r):
        return r
    mock_app_role_repo.update_role.side_effect = capture_update

    result = await admin_service.add_skill_to_role("editor", "skill_b", admin)

    assert "skill_a" in result.granted_skills
    assert "skill_b" in result.granted_skills


@pytest.mark.asyncio
async def test_add_skill_idempotent(admin_service, mock_app_role_repo, make_app_role, admin):
    role = make_app_role(role_id="editor", granted_skills=["skill_a"], skills=["skill_a"])
    mock_app_role_repo.get_role.return_value = role

    result = await admin_service.add_skill_to_role("editor", "skill_a", admin)

    assert result.granted_skills == ["skill_a"]
    mock_app_role_repo.update_role.assert_not_called()


@pytest.mark.asyncio
async def test_remove_skill_from_role(admin_service, mock_app_role_repo, make_app_role, admin):
    role = make_app_role(
        role_id="editor",
        granted_skills=["skill_a", "skill_b"],
        skills=["skill_a", "skill_b"],
    )
    mock_app_role_repo.get_role.return_value = role

    async def capture_update(r):
        return r
    mock_app_role_repo.update_role.side_effect = capture_update

    result = await admin_service.remove_skill_from_role("editor", "skill_a", admin)

    assert "skill_a" not in result.granted_skills
    assert "skill_b" in result.granted_skills


# ── admin: reverse lookup ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_roles_granting_skill_direct(admin_service, mock_app_role_repo, make_app_role):
    role = make_app_role(role_id="editor", granted_skills=["pdf_workflows"])
    mock_app_role_repo.get_roles_for_skill = AsyncMock(
        side_effect=lambda sid: (
            [{"roleId": "editor", "displayName": "Editor", "enabled": True}]
            if sid == "pdf_workflows" else []
        )
    )
    mock_app_role_repo.get_role.side_effect = lambda rid: {"editor": role}.get(rid)

    roles = await admin_service.get_roles_granting_skill("pdf_workflows")

    assert len(roles) == 1
    assert roles[0]["roleId"] == "editor"
    assert roles[0]["grantType"] == "direct"


@pytest.mark.asyncio
async def test_get_roles_granting_skill_inherited(admin_service, mock_app_role_repo, make_app_role):
    parent = make_app_role(
        role_id="parent",
        granted_skills=["pdf_workflows"],
        skills=["pdf_workflows"],
    )
    # Child inherits the skill (it's in effective_permissions but not granted directly).
    child = make_app_role(
        role_id="child",
        inherits_from=["parent"],
        granted_skills=[],
        skills=["pdf_workflows"],
    )
    mock_app_role_repo.get_roles_for_skill = AsyncMock(
        side_effect=lambda sid: (
            [{"roleId": "child", "displayName": "Child", "enabled": True}]
            if sid == "pdf_workflows" else []
        )
    )
    mock_app_role_repo.get_role.side_effect = lambda rid: {
        "child": child,
        "parent": parent,
    }.get(rid)

    roles = await admin_service.get_roles_granting_skill("pdf_workflows")

    assert len(roles) == 1
    assert roles[0]["roleId"] == "child"
    assert roles[0]["grantType"] == "inherited"
    assert roles[0]["inheritedFrom"] == "parent"
