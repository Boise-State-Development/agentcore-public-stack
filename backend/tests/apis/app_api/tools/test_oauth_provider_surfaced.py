"""`requiresOauthProvider` must reach the user-facing /tools payload.

`UserToolAccess` has always declared the field, but the service never passed it,
so every tool reported ``requiresOauthProvider: null``. The SPA therefore had no
way to tell that 13 of the 31 tools in prod need an OAuth connection before they
will run — the user only found out when a turn failed mid-answer.

Dependencies are mocked; the logic under test is pure.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.app_api.tools.service import ToolCatalogService
from apis.shared.auth.models import User
from apis.shared.rbac.models import UserEffectivePermissions
from apis.shared.tools.models import (
    MCPServerConfig,
    ToolDefinition,
    ToolProtocol,
    ToolStatus,
    UserToolPreference,
)


def _user():
    return User(user_id="u1", email="u@x.com", name="U", roles=["User"], raw_token="t")


def _service(tools):
    repo = MagicMock()
    repo.list_tools = AsyncMock(return_value=tools)
    repo.get_user_preferences = AsyncMock(
        return_value=UserToolPreference(user_id="u1", tool_preferences={})
    )
    role_service = MagicMock()
    role_service.resolve_user_permissions = AsyncMock(
        return_value=UserEffectivePermissions(
            user_id="u1",
            app_roles=["User"],
            tools=["*"],
            models=["*"],
            quota_tier=None,
            resolved_at="2024-01-01T00:00:00Z",
        )
    )
    return ToolCatalogService(repository=repo, app_role_service=role_service)


def _tool(tool_id: str, provider: str | None):
    return ToolDefinition(
        tool_id=tool_id,
        display_name=tool_id,
        description="x",
        protocol=ToolProtocol.MCP_EXTERNAL,
        status=ToolStatus.ACTIVE,
        requires_oauth_provider=provider,
        mcp_config=MCPServerConfig(server_url="https://example.com/mcp", tools=[]),
    )


@pytest.mark.asyncio
async def test_oauth_provider_reaches_the_user_payload():
    service = _service([_tool("canvas_faculty", "canvas-faculty")])
    tools = await service.get_user_accessible_tools(_user())
    assert tools[0].requires_oauth_provider == "canvas-faculty"


@pytest.mark.asyncio
async def test_tool_without_a_provider_reports_none():
    service = _service([_tool("calculator", None)])
    tools = await service.get_user_accessible_tools(_user())
    assert tools[0].requires_oauth_provider is None


@pytest.mark.asyncio
async def test_provider_survives_serialization_under_its_alias():
    # The SPA reads `requiresOauthProvider`; a field that only exists under its
    # snake_case name would be just as invisible as not being set at all.
    service = _service([_tool("gmail_employee", "gmail-employee")])
    tools = await service.get_user_accessible_tools(_user())
    dumped = tools[0].model_dump(by_alias=True)
    assert dumped["requiresOauthProvider"] == "gmail-employee"
