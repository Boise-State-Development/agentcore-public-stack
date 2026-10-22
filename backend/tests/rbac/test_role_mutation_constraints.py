"""Constraints on role mutations.

Verifies that role definitions cannot be mutated in ways that broaden the
``system_admin`` (or any other protected) role's reach to all users via
common JWT group claims, and that ``jwtRoleMappings`` entries follow a
strict format. These checks are enforced at the service layer so they apply
regardless of whether the call originates from the admin REST API, a CLI
script, or future automation.

On the format axis: single *internal* spaces are accepted, because real Entra
security groups are named as display names ("PSEmeriti Entra Sync") and the
tenant owner picks those names. Everything that cannot round trip through the
``custom:roles`` claim stays rejected -- commas (the claim delimiter), edge
whitespace (both claim parsers ``.strip()`` every entry, so a padded mapping
could never match), and every non-space whitespace or invisible character.
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
    [
        "default",
        "DEFAULT",
        "Default",
        "*",
        "user",
        "users",
        "everyone",
        "anyone",
        "authenticated",
        "all",
        # Now that spaces are accepted, the ubiquitous groups have a spelling
        # that could not previously be typed at all. "All Users" and
        # "Authenticated Users" are real Entra/AD display names for exactly
        # the populations this rule exists to keep off a protected role.
        "All Users",
        "Authenticated Users",
        "domain users",
    ],
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

    updates = AppRoleUpdate(
        jwt_role_mappings=["system_admin", "platform_admin", "Platform Admins Entra Sync"]
    )

    result = await service.update_role("system_admin", updates, admin)
    assert result is not None
    assert "platform_admin" in result.jwt_role_mappings
    assert "Platform Admins Entra Sync" in result.jwt_role_mappings


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
        "a" * 62 + " bb",  # 65 chars: the length bound still holds with a space
        "has/slash",
        "has.dot",
        # A comma is the delimiter in a comma-separated ``custom:roles`` claim
        # and in the admin form, so a comma-bearing group name is
        # unrepresentable and must stay rejected even now that spaces are not.
        "has,comma",
        "<script>",
        "name\nwithnewline",
        "name\twithtab",
        # Edge whitespace: both claim parsers ``.strip()`` every entry, so a
        # mapping stored like this could never match an incoming claim. It
        # must be rejected, not silently trimmed -- the stored value has to
        # match the claim byte for byte.
        " leading space",
        "trailing space ",
        "\tleading tab",
        "trailing newline\n",
        # Invisible characters. JS ``trim()`` does not strip U+200B, so one of
        # these can be pasted out of Entra or Teams straight into the payload.
        "nbsp\u00a0inside",
        "zero\u200bwidth",
        "\u200bleading zero width",
        "trailing nbsp\u00a0",
        # A doubled internal space is invisible in the comma-separated admin
        # field and is far likelier to be a typo than a real group name.
        "double  space",
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

    updates = AppRoleUpdate(
        jwt_role_mappings=[
            "valid_group",
            "Group-Name",
            "abc123",
            "_under_score",
            # Entra security groups are named as display names, and Boise
            # State does not control those names.
            "has spaces",
            "PSEmeriti Entra Sync",
            "Faculty",
            "a" * 62 + " b",  # exactly 64 chars, space included
        ]
    )

    result = await service.update_role("standard_user", updates, admin)
    assert result is not None
    assert set(result.jwt_role_mappings) >= {
        "valid_group",
        "Group-Name",
        "abc123",
        "has spaces",
        "PSEmeriti Entra Sync",
    }


@pytest.mark.asyncio
async def test_role_mapping_accepts_real_world_entra_group(service, mock_app_role_repo, make_app_role, admin) -> None:
    """The value from the prod incident this constraint was widened for.

    ``PATCH /api/admin/roles/faculty`` with ``["Faculty", "PSEmeriti Entra
    Sync"]`` returned 400, and because one bad entry rejects the whole
    payload, even the untouched ``Faculty`` mapping failed to save.
    """
    role = make_app_role(
        role_id="faculty",
        display_name="Faculty",
        is_system_role=False,
        jwt_role_mappings=["Faculty"],
    )
    mock_app_role_repo.get_role.return_value = role
    mock_app_role_repo.update_role.return_value = role

    updates = AppRoleUpdate(jwt_role_mappings=["Faculty", "PSEmeriti Entra Sync"])

    result = await service.update_role("faculty", updates, admin)
    assert result is not None
    assert "PSEmeriti Entra Sync" in result.jwt_role_mappings


@pytest.mark.parametrize(
    "bad_value,expected_fragment",
    [
        ("trailing space ", "start or end with a space"),
        ("double  space", "consecutive spaces"),
        ("has,comma", "comma"),
        ("x", "between 2 and 64"),
        ("zero\u200bwidth", "<U+200B>"),
        ("nbsp\u00a0inside", "<U+00A0>"),
        ("name\twithtab", "<U+0009>"),
    ],
)
@pytest.mark.asyncio
async def test_role_mapping_error_names_the_offending_entry(
    service, mock_app_role_repo, make_app_role, admin, bad_value: str, expected_fragment: str
) -> None:
    """A rejection has to say *which* entry failed and why.

    A bare "Invalid role configuration." is what turned the prod incident
    into a CloudWatch expedition. Invisible characters are escaped to a
    visible ``<U+XXXX>`` token, because echoing them raw would render
    identically to a correct value.
    """
    role = make_app_role(
        role_id="standard_user",
        display_name="Standard User",
        is_system_role=False,
        jwt_role_mappings=["standard_user"],
    )
    mock_app_role_repo.get_role.return_value = role
    mock_app_role_repo.update_role.return_value = role

    updates = AppRoleUpdate(jwt_role_mappings=[bad_value])

    with pytest.raises(ValueError) as excinfo:
        await service.update_role("standard_user", updates, admin)

    assert expected_fragment in str(excinfo.value)


@pytest.mark.asyncio
async def test_role_mapping_error_does_not_echo_raw_unsafe_characters(
    service, mock_app_role_repo, make_app_role, admin
) -> None:
    """The echoed value is bounded and escaped before it reaches the 400 body.

    Mirrors the reasoning in ``validate_admin_scopes``: detail is safe to
    return here because only a ``system_admin`` can reach the route, but a
    malformed payload still must not ride an arbitrarily long or
    control-character-bearing string into the response or the log line.
    """
    role = make_app_role(
        role_id="standard_user",
        display_name="Standard User",
        is_system_role=False,
        jwt_role_mappings=["standard_user"],
    )
    mock_app_role_repo.get_role.return_value = role
    mock_app_role_repo.update_role.return_value = role

    updates = AppRoleUpdate(jwt_role_mappings=["<script>alert(1)</script>" + "A" * 200])

    with pytest.raises(ValueError) as excinfo:
        await service.update_role("standard_user", updates, admin)

    message = str(excinfo.value)
    assert "<script>" not in message
    assert "<U+003C>" in message
    assert len(message) < 300


# ---------------------------------------------------------------------------
# Same constraints apply on create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_role_accepts_space_bearing_mapping(service, mock_app_role_repo, admin) -> None:
    role_data = AppRoleCreate(
        role_id="emeriti",
        display_name="Emeriti",
        jwt_role_mappings=["PSEmeriti Entra Sync"],
    )
    mock_app_role_repo.get_role.return_value = None

    await service.create_role(role_data, admin)

    # The repository mock returns an AsyncMock, so assert on what the service
    # handed it rather than on the round-tripped result.
    mock_app_role_repo.create_role.assert_awaited_once()
    created = mock_app_role_repo.create_role.await_args.args[0]
    assert "PSEmeriti Entra Sync" in created.jwt_role_mappings


@pytest.mark.asyncio
async def test_create_role_rejects_invalid_mapping_format(service, mock_app_role_repo, admin) -> None:
    role_data = AppRoleCreate(
        role_id="badrole",
        display_name="Bad",
        jwt_role_mappings=["has,comma"],
    )
    mock_app_role_repo.get_role.return_value = None

    with pytest.raises(ValueError):
        await service.create_role(role_data, admin)
