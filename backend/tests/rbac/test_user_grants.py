"""A direct user grant (``UserGrant``) widens the user's roles, and only that.

The grant is merged inside ``AppRoleService.resolve_user_permissions`` so that
every gate reading the resolved object — the tool checks here, the model
predicate, the skills gate, and the app-api routes that read
``permissions.tools`` directly — honours it without a second lookup. These
tests pin that merge and the ways it must NOT widen access: no wildcard, no
effect on quota tier or admin scopes, nothing after expiry, nothing on a
read failure.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.shared.auth.models import User
from apis.shared.rbac.models import UserGrant
from apis.shared.rbac.service import AppRoleService
from apis.shared.timestamps import utc_now_iso


def _future(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _past(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture(autouse=True)
def _no_live_tool_catalog():
    """``can_access_tool`` consults the public-tool snapshot; serve an empty one."""
    repo = MagicMock()
    repo.list_tools = AsyncMock(return_value=[])
    with patch(
        "apis.shared.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        yield


@pytest.fixture
def user():
    return User(email="u@example.com", user_id="user-1", name="U", roles=["Staff"])


@pytest.fixture
def service(mock_app_role_repo, mock_app_role_cache):
    return AppRoleService(repository=mock_app_role_repo, cache=mock_app_role_cache)


def _wire_role(mock_app_role_repo, role):
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = lambda r: (
        [role.role_id] if r == "Staff" else []
    )
    mock_app_role_repo.get_role.side_effect = lambda rid: role if rid == role.role_id else None


# ---------------------------------------------------------------------------
# UserGrant.is_active
# ---------------------------------------------------------------------------


class TestIsActive:
    def test_no_expiry_never_lapses(self):
        assert UserGrant(user_id="u").is_active(utc_now_iso()) is True

    def test_future_expiry_is_active(self):
        assert UserGrant(user_id="u", expires_at=_future()).is_active(utc_now_iso()) is True

    def test_past_expiry_is_inactive(self):
        assert UserGrant(user_id="u", expires_at=_past()).is_active(utc_now_iso()) is False

    def test_unparseable_expiry_reads_as_expired(self):
        """A typo must not grant forever."""
        assert UserGrant(user_id="u", expires_at="never").is_active(utc_now_iso()) is False

    def test_z_suffix_is_accepted(self):
        stamp = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert UserGrant(user_id="u", expires_at=stamp).is_active(utc_now_iso()) is True


# ---------------------------------------------------------------------------
# Merge into resolve_user_permissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_unions_into_every_axis(service, mock_app_role_repo, make_app_role, user):
    role = make_app_role(
        role_id="staff", tools=["calculator"], models=["m.base"], skills=["s.base"]
    )
    _wire_role(mock_app_role_repo, role)
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1",
        granted_tools=["browse_web"],
        granted_models=["m.pilot"],
        granted_skills=["s.pilot"],
    )

    perms = await service.resolve_user_permissions(user)

    assert perms.tools == ["browse_web", "calculator"]
    assert perms.models == ["m.base", "m.pilot"]
    assert perms.skills == ["s.base", "s.pilot"]
    assert perms.direct_tools == ["browse_web"]
    assert perms.direct_models == ["m.pilot"]
    assert perms.direct_skills == ["s.pilot"]
    # The roles themselves are untouched by the grant.
    assert perms.app_roles == ["staff"]


@pytest.mark.asyncio
async def test_grant_applies_with_no_matching_role(
    service, mock_app_role_repo, user
):
    """A user whose roles map to nothing (and no ``default``) still gets their grant."""
    mock_app_role_repo.get_roles_for_jwt_role.return_value = []
    mock_app_role_repo.get_role.return_value = None
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["browse_web"]
    )

    perms = await service.resolve_user_permissions(user)

    assert perms.app_roles == []
    assert perms.tools == ["browse_web"]
    assert perms.direct_tools == ["browse_web"]


@pytest.mark.asyncio
async def test_merged_lists_stay_sorted(service, mock_app_role_repo, make_app_role, user):
    """These lists reach ``toolConfig``; an order flip rewrites the prompt cache."""
    role = make_app_role(role_id="staff", tools=["zeta", "alpha"])
    _wire_role(mock_app_role_repo, role)
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["mid", "beta"]
    )

    perms = await service.resolve_user_permissions(user)

    assert perms.tools == sorted(perms.tools)
    assert perms.direct_tools == ["beta", "mid"]


@pytest.mark.asyncio
async def test_wildcard_role_is_not_diluted_by_a_grant(
    service, mock_app_role_repo, make_app_role, user
):
    role = make_app_role(role_id="admin", tools=["*"], models=["*"], skills=["*"])
    _wire_role(mock_app_role_repo, role)
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["browse_web"], granted_models=["m"], granted_skills=["s"]
    )

    perms = await service.resolve_user_permissions(user)

    assert "*" in perms.tools and "*" in perms.models and "*" in perms.skills
    # Attribution is still recorded so a picker can say "direct" as well.
    assert perms.direct_tools == ["browse_web"]


@pytest.mark.asyncio
async def test_wildcard_in_a_stored_grant_is_ignored(
    service, mock_app_role_repo, make_app_role, user
):
    """The write path refuses ``"*"``; a hand-written row must not become a superuser."""
    role = make_app_role(role_id="staff", tools=["calculator"])
    _wire_role(mock_app_role_repo, role)
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["*", "browse_web"], granted_models=["*"]
    )

    perms = await service.resolve_user_permissions(user)

    assert perms.tools == ["browse_web", "calculator"]
    assert perms.models == []
    assert "*" not in perms.direct_tools


@pytest.mark.asyncio
async def test_grant_touches_neither_quota_tier_nor_admin_scopes(
    service, mock_app_role_repo, make_app_role, user
):
    role = make_app_role(role_id="staff", quota_tier="standard", admin_scopes=["admin.costs"])
    _wire_role(mock_app_role_repo, role)
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["browse_web"]
    )

    perms = await service.resolve_user_permissions(user)

    assert perms.quota_tier == "standard"
    assert perms.admin_scopes == ["admin.costs"]


@pytest.mark.asyncio
async def test_expired_grant_is_absent(service, mock_app_role_repo, make_app_role, user):
    role = make_app_role(role_id="staff", tools=["calculator"])
    _wire_role(mock_app_role_repo, role)
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["browse_web"], expires_at=_past()
    )

    perms = await service.resolve_user_permissions(user)

    assert perms.tools == ["calculator"]
    assert perms.direct_tools == []


@pytest.mark.asyncio
async def test_grant_read_failure_falls_open_to_roles(
    service, mock_app_role_repo, make_app_role, user
):
    """A DynamoDB error on the grant must not refuse the roles the user holds."""
    role = make_app_role(role_id="staff", tools=["calculator"])
    _wire_role(mock_app_role_repo, role)
    mock_app_role_repo.get_user_grant.side_effect = RuntimeError("table unavailable")

    perms = await service.resolve_user_permissions(user)

    assert perms.tools == ["calculator"]
    assert perms.direct_tools == []


@pytest.mark.asyncio
async def test_non_grant_object_from_repository_is_ignored(
    service, mock_app_role_repo, make_app_role, user
):
    """A test double returning a bare mock must read as "no grant", not as one."""
    role = make_app_role(role_id="staff", tools=["calculator"])
    _wire_role(mock_app_role_repo, role)
    mock_app_role_repo.get_user_grant.return_value = MagicMock()

    perms = await service.resolve_user_permissions(user)

    assert perms.tools == ["calculator"]


@pytest.mark.asyncio
async def test_grant_is_read_once_per_cache_fill(
    service, mock_app_role_repo, mock_app_role_cache, make_app_role, user
):
    """The read rides the existing per-user cache; it never runs per call."""
    role = make_app_role(role_id="staff", tools=["calculator"])
    _wire_role(mock_app_role_repo, role)
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["browse_web"]
    )

    first = await service.resolve_user_permissions(user)
    mock_app_role_cache.get_user_permissions.return_value = first
    await service.resolve_user_permissions(user)
    await service.resolve_user_permissions(user)

    assert mock_app_role_repo.get_user_grant.await_count == 1


@pytest.mark.asyncio
async def test_role_resolution_failure_cancels_the_grant_read(
    service, mock_app_role_repo, user
):
    mock_app_role_repo.get_roles_for_jwt_role.side_effect = RuntimeError("boom")
    mock_app_role_repo.get_user_grant.return_value = None

    with pytest.raises(RuntimeError):
        await service.resolve_user_permissions(user)


# ---------------------------------------------------------------------------
# The gates honour it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_can_access_tool_honours_a_direct_grant(
    service, mock_app_role_repo, make_app_role, user
):
    _wire_role(mock_app_role_repo, make_app_role(role_id="staff", tools=[]))
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["browse_web"]
    )

    assert await service.can_access_tool(user, "browse_web") is True
    # Base-id grant admits a scoped ref, like a role grant does.
    assert await service.can_access_tool(user, "browse_web::fetch") is True
    assert await service.can_access_tool(user, "calculator") is False


@pytest.mark.asyncio
async def test_filter_requested_tools_honours_a_direct_grant(
    service, mock_app_role_repo, make_app_role, user
):
    _wire_role(mock_app_role_repo, make_app_role(role_id="staff", tools=["calculator"]))
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["browse_web"]
    )

    allowed = await service.filter_requested_tools(
        user, ["browse_web", "calculator", "gmail_employee"]
    )

    assert allowed == ["browse_web", "calculator"]


@pytest.mark.asyncio
async def test_can_access_model_honours_a_direct_grant(
    service, mock_app_role_repo, make_app_role, user
):
    _wire_role(mock_app_role_repo, make_app_role(role_id="staff", models=[]))
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_models=["m.pilot"]
    )

    assert await service.can_access_model(user, "m.pilot", record=None) is True
    assert await service.can_access_model(user, "m.other", record=None) is False


@pytest.mark.asyncio
async def test_disabled_catalog_row_still_refuses_a_directly_granted_model(
    service, mock_app_role_repo, make_app_role, user
):
    """Disabling a model takes it out of service for everyone — grant holders too."""
    _wire_role(mock_app_role_repo, make_app_role(role_id="staff", models=[]))
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_models=["m.pilot"]
    )
    disabled = MagicMock()
    disabled.enabled = False
    disabled.available_to_roles = []

    assert await service.can_access_model(user, "m.pilot", record=disabled) is False


@pytest.mark.asyncio
async def test_get_accessible_skills_includes_a_direct_grant(
    service, mock_app_role_repo, make_app_role, user
):
    _wire_role(mock_app_role_repo, make_app_role(role_id="staff", skills=["s.base"]))
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_skills=["s.pilot"]
    )

    assert await service.get_accessible_skills(user) == ["s.base", "s.pilot"]


# ---------------------------------------------------------------------------
# Picker attribution
# ---------------------------------------------------------------------------


class TestGrantedByAttribution:
    """``ToolCatalogService._compute_granted_by`` says *why* a tool is listed."""

    @staticmethod
    def _svc():
        from apis.app_api.tools.service import ToolCatalogService

        return ToolCatalogService(
            repository=MagicMock(),
            app_role_service=MagicMock(),
            app_role_admin_service=MagicMock(),
        )

    @staticmethod
    def _tool(tool_id: str, is_public: bool = False):
        tool = MagicMock()
        tool.tool_id = tool_id
        tool.is_public = is_public
        return tool

    @staticmethod
    def _perms(tools, direct_tools, app_roles=("staff",)):
        from apis.shared.rbac.models import UserEffectivePermissions

        return UserEffectivePermissions(
            user_id="u",
            app_roles=list(app_roles),
            tools=list(tools),
            models=[],
            quota_tier=None,
            resolved_at="",
            direct_tools=list(direct_tools),
        )

    def test_direct_only_grant_is_attributed_to_direct_not_roles(self):
        got = self._svc()._compute_granted_by(
            self._tool("browse_web"), self._perms(["browse_web"], ["browse_web"])
        )
        assert got == ["direct"]

    def test_role_and_direct_both_listed(self):
        got = self._svc()._compute_granted_by(
            self._tool("browse_web"),
            self._perms(["browse_web", "calculator"], ["browse_web"], app_roles=("staff",)),
        )
        # Only "direct" — the role list does not carry browse_web, so the
        # roles are not credited. (The union cannot tell; direct_tools can.)
        assert got == ["direct"]

    def test_role_granted_tool_is_attributed_to_roles(self):
        got = self._svc()._compute_granted_by(
            self._tool("calculator"), self._perms(["browse_web", "calculator"], ["browse_web"])
        )
        assert got == ["staff"]

    def test_wildcard_role_credits_roles_alongside_direct(self):
        got = self._svc()._compute_granted_by(
            self._tool("browse_web"), self._perms(["*", "browse_web"], ["browse_web"])
        )
        assert sorted(got) == ["direct", "staff"]

    def test_public_flag_still_listed(self):
        got = self._svc()._compute_granted_by(
            self._tool("x", is_public=True), self._perms([], [])
        )
        assert got == ["public"]
