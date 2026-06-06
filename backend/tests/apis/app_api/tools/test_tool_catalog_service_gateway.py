"""Phase 4 (issue #419): ToolCatalogService Gateway-target lifecycle orchestration
and the admin route's HTTP error mapping.

The GatewayTargetService is stubbed (no AWS) — these verify the catalog<->AWS
coupling: create the live target first, persist the row only on success, reconcile
on update, remove the target before the row on hard delete, and log loudly on a
partial failure.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from apis.app_api.admin.tools.routes import _raise_gateway_http
from apis.app_api.tools.service import ToolCatalogService
from apis.shared.tools.gateway_target_service import (
    GatewayTargetConflictError,
    GatewayTargetInfo,
    GatewayTargetNotFoundError,
)
from apis.shared.tools.models import (
    MCPGatewayConfig,
    ToolDefinition,
    ToolProtocol,
)


def _admin():
    return MagicMock(user_id="admin1", email="admin@example.com")


def _gw_info(target_id="TGT1", gateway_arn="arn:aws:bedrock-agentcore:us-west-2:1:gateway/gw1"):
    return GatewayTargetInfo(
        target_id=target_id, gateway_arn=gateway_arn, status="CREATING", name="weather-target"
    )


def _gateway_config(**overrides) -> MCPGatewayConfig:
    base = dict(target_name="weather-target", endpoint_url="https://example.com/mcp")
    base.update(overrides)
    return MCPGatewayConfig(**base)


def _gateway_tool(config: MCPGatewayConfig | None = None) -> ToolDefinition:
    return ToolDefinition(
        tool_id="gw_weather",
        display_name="Weather (Gateway)",
        description="Weather via the AgentCore Gateway",
        protocol=ToolProtocol.MCP_GATEWAY,
        mcp_gateway_config=config or _gateway_config(),
    )


def _service(repo, gw):
    return ToolCatalogService(
        repository=repo,
        app_role_service=MagicMock(),
        app_role_admin_service=MagicMock(),
        gateway_target_service=gw,
    )


# --------------------------------------------------------------------- create


class TestCreate:
    @pytest.mark.asyncio
    async def test_creates_aws_target_first_then_stamps_ids_and_persists(self):
        repo = MagicMock()
        repo.create_tool = AsyncMock(side_effect=lambda t: t)
        gw = MagicMock()
        gw.create_target = MagicMock(return_value=_gw_info())

        created = await _service(repo, gw).create_tool(_gateway_tool(), _admin())

        gw.create_target.assert_called_once()
        # AWS-assigned identifiers are stamped onto the persisted config.
        assert created.mcp_gateway_config.target_id == "TGT1"
        assert created.mcp_gateway_config.gateway_arn.endswith("gateway/gw1")
        # The row persisted carries the stamped target_id (stamp happened first).
        persisted = repo.create_tool.call_args.args[0]
        assert persisted.mcp_gateway_config.target_id == "TGT1"

    @pytest.mark.asyncio
    async def test_aws_failure_aborts_persist(self):
        repo = MagicMock()
        repo.create_tool = AsyncMock()
        gw = MagicMock()
        gw.create_target = MagicMock(side_effect=ClientError({"Error": {"Code": "ValidationException"}}, "op"))

        with pytest.raises(ClientError):
            await _service(repo, gw).create_tool(_gateway_tool(), _admin())
        repo.create_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_conflict_propagates(self):
        repo = MagicMock()
        repo.create_tool = AsyncMock()
        gw = MagicMock()
        gw.create_target = MagicMock(side_effect=GatewayTargetConflictError("exists"))

        with pytest.raises(GatewayTargetConflictError):
            await _service(repo, gw).create_tool(_gateway_tool(), _admin())
        repo.create_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_orphan_logged_when_persist_fails(self, caplog):
        repo = MagicMock()
        repo.create_tool = AsyncMock(side_effect=RuntimeError("dynamo down"))
        gw = MagicMock()
        gw.create_target = MagicMock(return_value=_gw_info(target_id="TGT-ORPHAN"))

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await _service(repo, gw).create_tool(_gateway_tool(), _admin())

        assert any(
            "ORPHANED GATEWAY TARGET" in r.message and "TGT-ORPHAN" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_protocol_mcp_without_config_rejected(self):
        repo = MagicMock()
        repo.create_tool = AsyncMock()
        gw = MagicMock()
        tool = ToolDefinition(
            tool_id="gw_bad",
            display_name="x",
            description="d",
            protocol=ToolProtocol.MCP_GATEWAY,
            mcp_gateway_config=None,
        )
        with pytest.raises(ValueError):
            await _service(repo, gw).create_tool(tool, _admin())
        gw.create_target.assert_not_called()
        repo.create_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_gateway_config_on_non_mcp_protocol_rejected(self):
        repo = MagicMock()
        repo.create_tool = AsyncMock()
        gw = MagicMock()
        tool = ToolDefinition(
            tool_id="local_bad",
            display_name="x",
            description="d",
            protocol=ToolProtocol.LOCAL,
            mcp_gateway_config=_gateway_config(),
        )
        with pytest.raises(ValueError):
            await _service(repo, gw).create_tool(tool, _admin())
        gw.create_target.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_gateway_tool_does_not_touch_gateway(self):
        repo = MagicMock()
        repo.create_tool = AsyncMock(side_effect=lambda t: t)
        gw = MagicMock()
        tool = ToolDefinition(
            tool_id="local_thing", display_name="x", description="d", protocol=ToolProtocol.LOCAL
        )
        await _service(repo, gw).create_tool(tool, _admin())
        gw.create_target.assert_not_called()
        repo.create_tool.assert_called_once()


# --------------------------------------------------------------------- update


class TestUpdate:
    @pytest.mark.asyncio
    async def test_config_change_reconciles_target_and_preserves_target_id(self):
        existing = _gateway_tool(_gateway_config(target_id="TGT1", gateway_arn="arn:old"))
        repo = MagicMock()
        repo.get_tool = AsyncMock(return_value=existing)
        repo.update_tool = AsyncMock(return_value=existing)
        gw = MagicMock()
        gw.update_target = MagicMock(return_value=_gw_info(gateway_arn="arn:new"))

        new_cfg = _gateway_config(endpoint_url="https://new.example.com/mcp")
        updates = {"mcp_gateway_config": new_cfg}
        await _service(repo, gw).update_tool("gw_weather", updates, _admin())

        gw.update_target.assert_called_once()
        assert gw.update_target.call_args.kwargs["target_id"] == "TGT1"
        # target_id preserved, gateway_arn refreshed from the update response.
        assert updates["mcp_gateway_config"].target_id == "TGT1"
        assert updates["mcp_gateway_config"].gateway_arn == "arn:new"

    @pytest.mark.asyncio
    async def test_protocol_transition_to_mcp_rejected(self):
        existing = ToolDefinition(
            tool_id="local_thing", display_name="x", description="d", protocol=ToolProtocol.LOCAL
        )
        repo = MagicMock()
        repo.get_tool = AsyncMock(return_value=existing)
        repo.update_tool = AsyncMock()
        gw = MagicMock()

        with pytest.raises(ValueError):
            await _service(repo, gw).update_tool(
                "local_thing", {"protocol": ToolProtocol.MCP_GATEWAY}, _admin()
            )
        gw.update_target.assert_not_called()
        repo.update_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_config_update_does_not_touch_gateway(self):
        repo = MagicMock()
        repo.update_tool = AsyncMock(return_value=_gateway_tool())
        repo.get_tool = AsyncMock()
        gw = MagicMock()

        await _service(repo, gw).update_tool("gw_weather", {"display_name": "New"}, _admin())

        gw.update_target.assert_not_called()
        # display_name isn't a config/protocol field, so we don't even fetch existing.
        repo.get_tool.assert_not_called()
        repo.update_tool.assert_called_once()


# --------------------------------------------------------------------- delete


class TestDelete:
    @pytest.mark.asyncio
    async def test_hard_delete_removes_target_before_row(self):
        existing = _gateway_tool(_gateway_config(target_id="TGT1"))
        repo = MagicMock()
        repo.get_tool = AsyncMock(return_value=existing)
        repo.delete_tool = AsyncMock(return_value=True)
        gw = MagicMock()
        gw.delete_target = MagicMock()

        ok = await _service(repo, gw).delete_tool("gw_weather", _admin(), soft=False)

        assert ok is True
        # Passes the stored config so the service can revoke the target's
        # gateway-role Lambda grant alongside deleting the target.
        gw.delete_target.assert_called_once_with(
            target_id="TGT1", config=existing.mcp_gateway_config
        )
        repo.delete_tool.assert_called_once_with("gw_weather")

    @pytest.mark.asyncio
    async def test_soft_delete_leaves_target_in_place(self):
        existing = _gateway_tool(_gateway_config(target_id="TGT1"))
        repo = MagicMock()
        repo.get_tool = AsyncMock(return_value=existing)
        repo.soft_delete_tool = AsyncMock(return_value=existing)
        gw = MagicMock()

        ok = await _service(repo, gw).delete_tool("gw_weather", _admin(), soft=True)

        assert ok is True
        gw.delete_target.assert_not_called()
        repo.soft_delete_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_not_found_returns_false_without_touching_gateway(self):
        repo = MagicMock()
        repo.get_tool = AsyncMock(return_value=None)
        gw = MagicMock()

        ok = await _service(repo, gw).delete_tool("nope", _admin(), soft=False)

        assert ok is False
        gw.delete_target.assert_not_called()

    @pytest.mark.asyncio
    async def test_divergence_logged_when_row_delete_fails_after_target_delete(self, caplog):
        existing = _gateway_tool(_gateway_config(target_id="TGT1"))
        repo = MagicMock()
        repo.get_tool = AsyncMock(return_value=existing)
        repo.delete_tool = AsyncMock(side_effect=RuntimeError("dynamo down"))
        gw = MagicMock()
        gw.delete_target = MagicMock()

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await _service(repo, gw).delete_tool("gw_weather", _admin(), soft=False)

        assert any("state diverged" in r.message for r in caplog.records)


# ----------------------------------------------------- route HTTP error mapping


class TestRouteErrorMapping:
    def test_conflict_maps_to_409(self):
        exc = _raise_gateway_http(GatewayTargetConflictError("exists"))
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 409

    def test_not_found_maps_to_409(self):
        exc = _raise_gateway_http(GatewayTargetNotFoundError("gone"))
        assert exc.status_code == 409

    def test_aws_client_error_maps_to_502(self):
        exc = _raise_gateway_http(
            ClientError({"Error": {"Code": "ThrottlingException"}}, "op")
        )
        assert exc.status_code == 502

    def test_runtime_error_maps_to_502(self):
        # e.g. the gateway-id SSM parameter is unavailable.
        exc = _raise_gateway_http(RuntimeError("Gateway id SSM parameter unavailable"))
        assert exc.status_code == 502
