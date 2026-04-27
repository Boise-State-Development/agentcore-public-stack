"""Tests for ToolAccessService.

Verifies that filter_allowed_tools sources its "universe of known
tools" from the DynamoDB-backed catalog (via the freshness snapshot)
rather than the legacy in-memory catalog. This is critical for
admin-managed MCP-external and A2A tools, which only exist in
DynamoDB.
"""

from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pytest

from apis.app_api.tools import freshness
from apis.app_api.admin.services.tool_access import ToolAccessService
from apis.shared.auth.models import User
from apis.shared.rbac.models import UserEffectivePermissions


@pytest.fixture(autouse=True)
def _reset_freshness():
    freshness._reset_for_tests()
    yield
    freshness._reset_for_tests()


def _user(roles=None) -> User:
    return User(
        email="admin@example.com",
        user_id="admin-1",
        name="Admin",
        roles=roles or [],
    )


def _permissions(tools, app_roles=("admin",)) -> UserEffectivePermissions:
    return UserEffectivePermissions(
        user_id="admin-1",
        app_roles=list(app_roles),
        tools=list(tools),
        models=[],
        quota_tier=None,
        resolved_at="2026-04-26T00:00:00Z",
    )


def _service(permissions: UserEffectivePermissions) -> ToolAccessService:
    role_service = AsyncMock()
    role_service.resolve_user_permissions = AsyncMock(return_value=permissions)
    return ToolAccessService(app_role_service=role_service)


def _patch_catalog(tool_ids):
    """Patch the repository.list_tools call that backs freshness snapshot."""
    repo = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=[SimpleNamespace(tool_id=tid) for tid in tool_ids]
        )
    )
    return patch(
        "apis.app_api.tools.repository.get_tool_catalog_repository",
        return_value=repo,
    )


@pytest.mark.asyncio
async def test_wildcard_user_sees_mcp_external_tool_added_via_admin_form():
    """An MCP-external tool added via the admin form (DynamoDB only,
    not in the legacy in-memory catalog) must be returned by
    filter_allowed_tools for a wildcard-access admin.

    This is the core regression: before this fix, the wildcard branch
    only enumerated tools from the legacy catalog and the new tool
    silently disappeared.
    """
    service = _service(_permissions(["*"]))

    with _patch_catalog(
        ["calculator", "fetch_url_content", "linear_create_issue"]
    ):
        # No requested_tools — wildcard should expand to ALL known tools
        result = await service.filter_allowed_tools(_user(), None)

    assert "linear_create_issue" in result, (
        "MCP-external tool added via admin form must be visible to "
        "wildcard users"
    )
    assert set(result) == {
        "calculator",
        "fetch_url_content",
        "linear_create_issue",
    }


@pytest.mark.asyncio
async def test_wildcard_user_filters_out_unknown_tool():
    """A tool that is not in the catalog must be filtered out, even
    for wildcard users — wildcard means 'every known tool', not
    'every tool ID a client claims to want'."""
    service = _service(_permissions(["*"]))

    with _patch_catalog(["calculator", "fetch_url_content"]):
        result = await service.filter_allowed_tools(
            _user(),
            requested_tools=[
                "calculator",
                "made_up_tool",  # not in catalog
                "fetch_url_content",
            ],
        )

    assert "made_up_tool" not in result
    assert set(result) == {"calculator", "fetch_url_content"}


@pytest.mark.asyncio
async def test_wildcard_user_keeps_gateway_tools_even_when_not_in_catalog():
    """Gateway tools (gateway_* prefix) are loaded dynamically from the
    AgentCore Gateway and aren't persisted to the DynamoDB catalog.
    The prefix-based bypass must keep them allowed for wildcard users.
    """
    service = _service(_permissions(["*"]))

    with _patch_catalog(["calculator"]):
        result = await service.filter_allowed_tools(
            _user(),
            requested_tools=["calculator", "gateway_wikipedia"],
        )

    assert set(result) == {"calculator", "gateway_wikipedia"}


@pytest.mark.asyncio
async def test_non_wildcard_user_sees_intersection_of_granted_and_catalog():
    """Non-wildcard users get the intersection of their granted tools
    and the catalog when no specific tools are requested."""
    service = _service(_permissions(["calculator", "linear_create_issue"]))

    with _patch_catalog(["calculator", "linear_create_issue", "weather"]):
        result = await service.filter_allowed_tools(_user(), None)

    assert set(result) == {"calculator", "linear_create_issue"}


@pytest.mark.asyncio
async def test_non_wildcard_user_denies_unauthorized_request():
    """Non-wildcard users must not get tools they aren't granted, even
    if those tools exist in the catalog."""
    service = _service(_permissions(["calculator"]))

    with _patch_catalog(["calculator", "linear_create_issue"]):
        result = await service.filter_allowed_tools(
            _user(),
            requested_tools=["calculator", "linear_create_issue"],
        )

    assert result == ["calculator"]


@pytest.mark.asyncio
async def test_check_access_and_filter_reports_denied():
    """check_access_and_filter must split allowed and denied lists and
    surface the denied set to the caller (so chat routes can log)."""
    service = _service(_permissions(["calculator"]))

    with _patch_catalog(["calculator", "linear_create_issue"]):
        allowed, denied = await service.check_access_and_filter(
            _user(),
            requested_tools=["calculator", "linear_create_issue"],
        )

    assert allowed == ["calculator"]
    assert denied == ["linear_create_issue"]
