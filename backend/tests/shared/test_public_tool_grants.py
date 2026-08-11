"""A tool flagged ``isPublic`` must pass every gate, not just the picker.

Regression cover for the divergence where ``isPublic`` was honoured only by
``ToolCatalogService._compute_granted_by`` (which builds the tool picker) while
every enforcement path read role ``grantedTools`` alone. A public-but-ungranted
tool therefore listed for everyone and then failed at use: silently dropped from
a scheduled run, and a hard block on any Agent that bound it. Only wildcard
holders — admins — were unaffected, which is what made it look like a
permissions misconfiguration rather than a bug.

Every case here uses a role whose ``grantedTools`` is **empty**, so the public
flag is the only thing that can grant access.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.shared.tools import freshness


PUBLIC_TOOL = "create_word_document"
GRANTED_TOOL = "calculator"
PRIVATE_TOOL = "gmail_employee"


def _catalog_tool(tool_id: str, is_public: bool):
    repo_tool = MagicMock()
    repo_tool.tool_id = tool_id
    repo_tool.is_public = is_public
    return repo_tool


@pytest.fixture(autouse=True)
def _catalog():
    """Serve a three-tool catalog: one public, two not."""
    freshness._reset_for_tests()
    repo = MagicMock()
    repo.list_tools = AsyncMock(
        return_value=[
            _catalog_tool(PUBLIC_TOOL, is_public=True),
            _catalog_tool(GRANTED_TOOL, is_public=False),
            _catalog_tool(PRIVATE_TOOL, is_public=False),
        ]
    )
    with patch(
        "apis.shared.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        yield
    freshness._reset_for_tests()


@pytest.fixture
def svc():
    """An AppRoleService whose only role grants ``calculator`` and nothing else."""
    from apis.shared.rbac.cache import AppRoleCache
    from apis.shared.rbac.models import AppRole, EffectivePermissions
    from apis.shared.rbac.service import AppRoleService

    repo = AsyncMock()
    repo.get_roles_for_jwt_role.return_value = ["student"]
    repo.get_role.return_value = AppRole(
        role_id="student",
        display_name="student",
        description="test",
        jwt_role_mappings=["student"],
        priority=10,
        enabled=True,
        effective_permissions=EffectivePermissions(
            tools=[GRANTED_TOOL], models=["*"],
        ),
    )
    return AppRoleService(repository=repo, cache=AppRoleCache())


def _user():
    user = MagicMock()
    user.user_id = "u1"
    user.email = "student@example.edu"
    user.roles = ["student"]
    return user


class TestCanAccessTool:
    """The gate an Agent's tool binding is re-resolved through (D5)."""

    @pytest.mark.asyncio
    async def test_public_tool_is_accessible_without_a_role_grant(self, svc):
        assert await svc.can_access_tool(_user(), PUBLIC_TOOL) is True

    @pytest.mark.asyncio
    async def test_role_granted_tool_still_accessible(self, svc):
        assert await svc.can_access_tool(_user(), GRANTED_TOOL) is True

    @pytest.mark.asyncio
    async def test_non_public_ungranted_tool_is_still_denied(self, svc):
        """The fix widens access to public tools only — nothing else."""
        assert await svc.can_access_tool(_user(), PRIVATE_TOOL) is False

    @pytest.mark.asyncio
    async def test_tool_absent_from_the_catalog_is_denied(self, svc):
        assert await svc.can_access_tool(_user(), "no_such_tool") is False


class TestFilterRequestedTools:
    """The gate schedules and "Run now" narrow their tool list through."""

    @pytest.mark.asyncio
    async def test_public_tool_survives_the_filter(self, svc):
        allowed = await svc.filter_requested_tools(
            _user(), [PUBLIC_TOOL, GRANTED_TOOL, PRIVATE_TOOL]
        )
        assert allowed == [PUBLIC_TOOL, GRANTED_TOOL]

    @pytest.mark.asyncio
    async def test_scoped_id_of_a_public_server_survives(self, svc):
        """A public MCP server admits its per-tool selections, as a grant does."""
        allowed = await svc.filter_requested_tools(_user(), [f"{PUBLIC_TOOL}::sub"])
        assert allowed == [f"{PUBLIC_TOOL}::sub"]

    @pytest.mark.asyncio
    async def test_requested_order_is_preserved(self, svc):
        """Order is part of this contract — it reaches the model's toolConfig."""
        allowed = await svc.filter_requested_tools(
            _user(), [GRANTED_TOOL, PUBLIC_TOOL]
        )
        assert allowed == [GRANTED_TOOL, PUBLIC_TOOL]


class TestGetAccessibleTools:
    @pytest.mark.asyncio
    async def test_union_of_role_grant_and_public_tools(self, svc):
        assert await svc.get_accessible_tools(_user()) == [GRANTED_TOOL, PUBLIC_TOOL]

    @pytest.mark.asyncio
    async def test_result_is_sorted_for_prompt_cache_stability(self, svc):
        tools = await svc.get_accessible_tools(_user())
        assert tools == sorted(tools)


class TestToolAccessService:
    """The third gate — it must not keep its own narrower copy of the rule."""

    @pytest.fixture
    def tool_access(self, svc):
        from apis.app_api.admin.services.tool_access import ToolAccessService

        return ToolAccessService(app_role_service=svc)

    @pytest.mark.asyncio
    async def test_can_access_public_tool(self, tool_access):
        assert await tool_access.can_access_tool(_user(), PUBLIC_TOOL) is True

    @pytest.mark.asyncio
    async def test_filter_keeps_public_tool(self, tool_access):
        allowed = await tool_access.filter_allowed_tools(
            _user(), [PUBLIC_TOOL, PRIVATE_TOOL]
        )
        assert allowed == [PUBLIC_TOOL]

    @pytest.mark.asyncio
    async def test_denied_list_no_longer_names_public_tools(self, tool_access):
        allowed, denied = await tool_access.check_access_and_filter(
            _user(), [PUBLIC_TOOL, PRIVATE_TOOL]
        )
        assert allowed == [PUBLIC_TOOL]
        assert denied == [PRIVATE_TOOL]


class TestWildcardUnaffected:
    """Admins held ``"*"`` and never saw the bug; that path must not change."""

    @pytest.fixture
    def admin_svc(self):
        from apis.shared.rbac.cache import AppRoleCache
        from apis.shared.rbac.models import AppRole, EffectivePermissions
        from apis.shared.rbac.service import AppRoleService

        repo = AsyncMock()
        repo.get_roles_for_jwt_role.return_value = ["system_admin"]
        repo.get_role.return_value = AppRole(
            role_id="system_admin",
            display_name="system_admin",
            description="test",
            jwt_role_mappings=["system_admin"],
            priority=100,
            enabled=True,
            effective_permissions=EffectivePermissions(tools=["*"], models=["*"]),
        )
        return AppRoleService(repository=repo, cache=AppRoleCache())

    @pytest.mark.asyncio
    async def test_wildcard_still_grants_everything(self, admin_svc):
        assert await admin_svc.can_access_tool(_user(), PRIVATE_TOOL) is True

    @pytest.mark.asyncio
    async def test_wildcard_passes_the_whole_request_through(self, admin_svc):
        requested = [PRIVATE_TOOL, "anything_at_all"]
        assert await admin_svc.filter_requested_tools(_user(), requested) == requested


class TestCatalogUnavailable:
    """A catalog read failure must deny-as-before, never raise mid-turn."""

    @pytest.mark.asyncio
    async def test_public_grant_degrades_to_role_grant_on_error(self, svc):
        freshness._reset_for_tests()
        repo = MagicMock()
        repo.list_tools = AsyncMock(side_effect=RuntimeError("dynamo down"))
        with patch(
            "apis.shared.tools.repository.get_tool_catalog_repository",
            return_value=repo,
        ):
            assert await svc.can_access_tool(_user(), GRANTED_TOOL) is True
            assert await svc.can_access_tool(_user(), PUBLIC_TOOL) is False
