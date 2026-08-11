"""
Auth-mode validation must hold on the UPDATE path, not just on create.

`_validate_auth_config` enforces two rules: at most one per-user auth mode
(forward_auth_token / requires_oauth_provider / token_exchange_audience — all
three compete for the single Authorization header), and MCP auth type 'none' when
a mode owns that header.

Create ran those checks. Update did not consider token_exchange_audience at all,
so editing a tool to add an audience skipped validation entirely — a rule that
held on one path and not the other. The runtime happens to prefer the exchanged
token, so the result was a broken tool rather than a leaked credential, but the
inconsistency is the bug.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.app_api.tools.service import ToolCatalogService
from apis.shared.tools.models import (
    MCPAuthType,
    MCPServerConfig,
    ToolDefinition,
    ToolProtocol,
)


def _admin():
    return MagicMock(user_id="admin1", email="admin@example.com")


def _tool(**overrides) -> ToolDefinition:
    base = dict(
        tool_id="campus_directory",
        display_name="Directory",
        description="d",
        category="utility",
        protocol=ToolProtocol.MCP_EXTERNAL,
    )
    base.update(overrides)
    return ToolDefinition(**base)


def _service(existing: ToolDefinition) -> ToolCatalogService:
    svc = ToolCatalogService.__new__(ToolCatalogService)
    svc.repository = MagicMock()
    svc.repository.get_tool = AsyncMock(return_value=existing)
    svc.repository.update_tool = AsyncMock(return_value=existing)
    return svc


class TestUpdateEnforcesAuthModeExclusivity:
    @pytest.mark.asyncio
    async def test_adding_audience_to_a_forwarding_tool_is_rejected(self):
        existing = _tool(forward_auth_token=True)
        svc = _service(existing)

        with pytest.raises(ValueError) as exc:
            await svc.update_tool(
                "campus_directory",
                {"token_exchange_audience": "cc5aa8a0-guid"},
                _admin(),
            )
        assert "more than one per-user auth mode" in str(exc.value)

    @pytest.mark.asyncio
    async def test_adding_audience_to_an_oauth_tool_is_rejected(self):
        existing = _tool(requires_oauth_provider="google_workspace")
        svc = _service(existing)

        with pytest.raises(ValueError):
            await svc.update_tool(
                "campus_directory",
                {"token_exchange_audience": "cc5aa8a0-guid"},
                _admin(),
            )

    @pytest.mark.asyncio
    async def test_adding_forwarding_to_an_exchange_tool_is_rejected(self):
        # The mirror case: the conflict must be caught whichever side is edited.
        existing = _tool(token_exchange_audience="cc5aa8a0-guid")
        svc = _service(existing)

        with pytest.raises(ValueError):
            await svc.update_tool(
                "campus_directory", {"forward_auth_token": True}, _admin()
            )

    @pytest.mark.asyncio
    async def test_audience_requires_mcp_auth_type_none(self):
        # SigV4 and Bearer both want the Authorization header; the exchanged
        # token would win and the request would reach an IAM-expecting endpoint
        # unsigned.
        existing = _tool(
            mcp_config=MCPServerConfig(
                server_url="https://x.lambda-url.us-west-2.on.aws/mcp",
                auth_type=MCPAuthType.AWS_IAM,
            )
        )
        svc = _service(existing)

        with pytest.raises(ValueError) as exc:
            await svc.update_tool(
                "campus_directory",
                {"token_exchange_audience": "cc5aa8a0-guid"},
                _admin(),
            )
        assert "must be 'none'" in str(exc.value)

    @pytest.mark.asyncio
    async def test_valid_audience_update_is_allowed(self):
        existing = _tool(
            mcp_config=MCPServerConfig(
                server_url="https://x.lambda-url.us-west-2.on.aws/mcp",
                auth_type=MCPAuthType.NONE,
            )
        )
        svc = _service(existing)

        await svc.update_tool(
            "campus_directory",
            {"token_exchange_audience": "cc5aa8a0-guid"},
            _admin(),
        )
        svc.repository.update_tool.assert_awaited()

    @pytest.mark.asyncio
    async def test_clearing_the_audience_is_allowed(self):
        # Removing a mode must never be blocked — otherwise a misconfigured tool
        # could not be repaired.
        existing = _tool(token_exchange_audience="cc5aa8a0-guid")
        svc = _service(existing)

        await svc.update_tool(
            "campus_directory", {"token_exchange_audience": None}, _admin()
        )
        svc.repository.update_tool.assert_awaited()
