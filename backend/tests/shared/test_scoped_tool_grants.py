"""A scoped tool id (``base::tool``) is granted by a grant on its **base** server.

Scoping narrows a grant, it never widens one: an Agent binding
``canvas_faculty::list_courses`` asks for strictly less than one binding
``canvas_faculty``, so the same role grant must admit it. Before this, the
binding gate ``AppRoleService.can_access_tool`` exact-matched the id, so a
scoped binding was denied for every user — including one holding the whole
server — while the sibling ``filter_requested_tools`` on the ``enabled_tools``
axis already base-collapsed correctly. The two must stay in agreement, or the
chat picker and the Agent Designer disagree about the same subset.

Real ``AppRole`` records throughout (no predicate stubs), so these fail if the
grant semantics move.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apis.shared.tools import freshness

GRANTED_SERVER = "canvas_faculty"
PUBLIC_SERVER = "fetch_url_content"
PRIVATE_SERVER = "gmail_employee"


def _catalog_tool(tool_id: str, is_public: bool):
    repo_tool = MagicMock()
    repo_tool.tool_id = tool_id
    repo_tool.is_public = is_public
    return repo_tool


@pytest.fixture(autouse=True)
def _catalog():
    freshness._reset_for_tests()
    repo = MagicMock()
    repo.list_tools = AsyncMock(
        return_value=[
            _catalog_tool(GRANTED_SERVER, is_public=False),
            _catalog_tool(PUBLIC_SERVER, is_public=True),
            _catalog_tool(PRIVATE_SERVER, is_public=False),
        ]
    )
    with patch(
        "apis.shared.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    ):
        yield
    freshness._reset_for_tests()


def _service(granted_tools):
    from apis.shared.rbac.cache import AppRoleCache
    from apis.shared.rbac.models import AppRole, EffectivePermissions
    from apis.shared.rbac.service import AppRoleService

    repo = AsyncMock()
    repo.get_roles_for_jwt_role.return_value = ["faculty"]
    repo.get_role.return_value = AppRole(
        role_id="faculty",
        display_name="faculty",
        description="test",
        jwt_role_mappings=["faculty"],
        priority=10,
        enabled=True,
        effective_permissions=EffectivePermissions(tools=granted_tools, models=["*"]),
    )
    return AppRoleService(repository=repo, cache=AppRoleCache())


@pytest.fixture
def svc():
    """A role granting one MCP server and nothing else."""
    return _service([GRANTED_SERVER])


def _user():
    user = MagicMock()
    user.user_id = "u1"
    user.email = "prof@example.edu"
    user.roles = ["faculty"]
    return user


class TestCanAccessTool:
    """The gate an Agent's tool binding is re-resolved through (D5)."""

    @pytest.mark.asyncio
    async def test_scoped_id_admitted_by_a_grant_on_its_base_server(self, svc):
        assert await svc.can_access_tool(_user(), f"{GRANTED_SERVER}::list_courses") is True

    @pytest.mark.asyncio
    async def test_bare_id_still_admitted(self, svc):
        assert await svc.can_access_tool(_user(), GRANTED_SERVER) is True

    @pytest.mark.asyncio
    async def test_scoped_id_denied_when_its_base_is_not_granted(self, svc):
        """Scoping narrows a grant; it cannot manufacture one."""
        assert await svc.can_access_tool(_user(), f"{PRIVATE_SERVER}::send_mail") is False

    @pytest.mark.asyncio
    async def test_scoped_id_admitted_when_its_base_is_public(self, svc):
        """A public tool is a grant, so it admits subsets like any other."""
        assert await svc.can_access_tool(_user(), f"{PUBLIC_SERVER}::fetch") is True

    @pytest.mark.asyncio
    async def test_wildcard_admits_a_scoped_id(self):
        assert await _service(["*"]).can_access_tool(_user(), f"{PRIVATE_SERVER}::send_mail") is True

    @pytest.mark.asyncio
    async def test_agrees_with_filter_requested_tools(self, svc):
        """The bindings axis and the enabled_tools axis must answer alike.

        They diverged before this change — the reason a subset the chat picker
        happily sent was rejected as an Agent binding.
        """
        candidates = [
            GRANTED_SERVER,
            f"{GRANTED_SERVER}::list_courses",
            PUBLIC_SERVER,
            f"{PUBLIC_SERVER}::fetch",
            PRIVATE_SERVER,
            f"{PRIVATE_SERVER}::send_mail",
        ]
        by_filter = set(await svc.filter_requested_tools(_user(), candidates))
        for tool_id in candidates:
            assert await svc.can_access_tool(_user(), tool_id) is (tool_id in by_filter), tool_id
