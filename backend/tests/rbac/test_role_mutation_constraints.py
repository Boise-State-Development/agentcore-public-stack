"""Constraints on role mutations.

Verifies that role definitions cannot be mutated in ways that broaden the
``system_admin`` (or any other protected) role's reach to all users via
common JWT group claims, and that ``jwtRoleMappings`` entries follow a
strict format. These checks are enforced at the service layer so they apply
regardless of whether the call originates from the admin REST API, a CLI
script, or future automation.
"""

from __future__ import annotations

import pytest

from apis.shared.auth.models import User
from apis.shared.rbac.admin_service import AppRoleAdminService
from apis.shared.rbac.models import AppRoleCreate, AppRoleUpdate


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
# Protected roles cannot accept ubiquitous JWT group names in their mappings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    ["default", "DEFAULT", "Default", "*", "user", "users", "everyone", "anyone", "authenticated", "all"],
)
@pytest.mark.asyncio
async def test_protected_role_rejects_ubiquitous_jwt_mapping(service, mock_app_role_repo, make_app_role, admin, forbidden: str) -> None:
    system_admin_role = make_app_role(
        role_id="system_admin",
        display_name="System Admin",
        is_system_role=True,
        jwt_role_mappings=["system_admin"],
    )
    mock_app_role_repo.get_role.return_value = system_admin_role
    mock_app_role_repo.update_role.return_value = system_admin_role

    updates = AppRoleUpdate(jwt_role_mappings=["system_admin", forbidden])

    with pytest.raises(ValueError):
        await service.update_role("system_admin", updates, admin)


@pytest.mark.asyncio
async def test_protected_role_accepts_specific_group_mapping(service, mock_app_role_repo, make_app_role, admin) -> None:
    system_admin_role = make_app_role(
        role_id="system_admin",
        display_name="System Admin",
        is_system_role=True,
        jwt_role_mappings=["system_admin"],
    )
    mock_app_role_repo.get_role.return_value = system_admin_role
    mock_app_role_repo.update_role.return_value = system_admin_role

    updates = AppRoleUpdate(jwt_role_mappings=["system_admin", "platform_admin"])

    result = await service.update_role("system_admin", updates, admin)
    assert result is not None
    assert "platform_admin" in result.jwt_role_mappings


# ---------------------------------------------------------------------------
# Non-protected roles are unaffected by the ubiquitous-mapping rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_protected_role_can_have_default_mapping(service, mock_app_role_repo, make_app_role, admin) -> None:
    """The 'default' role is itself the bearer of the 'default' JWT group.

    The constraint applies only to *protected* roles; an everyday role can
    legitimately map the 'default' group name to itself.
    """
    role = make_app_role(
        role_id="standard_user",
        display_name="Standard User",
        is_system_role=False,
        jwt_role_mappings=["standard_user"],
    )
    mock_app_role_repo.get_role.return_value = role
    mock_app_role_repo.update_role.return_value = role

    updates = AppRoleUpdate(jwt_role_mappings=["standard_user", "default"])

    result = await service.update_role("standard_user", updates, admin)
    assert result is not None
    assert "default" in result.jwt_role_mappings


# ---------------------------------------------------------------------------
# Format: every mapping entry must look like a real group identifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "",  # empty
        "x",  # too short
        "a" * 65,  # too long
        "has spaces",
        "has/slash",
        "has.dot",
        "has,comma",
        "<script>",
        "name\nwithnewline",
    ],
)
@pytest.mark.asyncio
async def test_role_mapping_must_match_format(service, mock_app_role_repo, make_app_role, admin, bad_value: str) -> None:
    role = make_app_role(
        role_id="standard_user",
        display_name="Standard User",
        is_system_role=False,
        jwt_role_mappings=["standard_user"],
    )
    mock_app_role_repo.get_role.return_value = role
    mock_app_role_repo.update_role.return_value = role

    updates = AppRoleUpdate(jwt_role_mappings=[bad_value])

    with pytest.raises(ValueError):
        await service.update_role("standard_user", updates, admin)


@pytest.mark.asyncio
async def test_role_mapping_accepts_valid_format(service, mock_app_role_repo, make_app_role, admin) -> None:
    role = make_app_role(
        role_id="standard_user",
        display_name="Standard User",
        is_system_role=False,
        jwt_role_mappings=["standard_user"],
    )
    mock_app_role_repo.get_role.return_value = role
    mock_app_role_repo.update_role.return_value = role

    updates = AppRoleUpdate(jwt_role_mappings=["valid_group", "Group-Name", "abc123", "_under_score"])

    result = await service.update_role("standard_user", updates, admin)
    assert result is not None
    assert set(result.jwt_role_mappings) >= {"valid_group", "Group-Name", "abc123"}


# ---------------------------------------------------------------------------
# Same constraints apply on create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_role_rejects_invalid_mapping_format(service, mock_app_role_repo, admin) -> None:
    role_data = AppRoleCreate(
        role_id="badrole",
        display_name="Bad",
        jwt_role_mappings=["has spaces"],
    )
    mock_app_role_repo.get_role.return_value = None

    with pytest.raises(ValueError):
        await service.create_role(role_data, admin)
