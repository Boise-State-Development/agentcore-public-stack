"""Tests for shared gateway-identity resolution.

One source of truth for "which gateway" — the admin-side GatewayTargetService
and the agent-side gateway client both resolve through here so they can never
diverge (the bug that left admin-registered targets on a gateway the agent never
connected to).
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from apis.shared.tools.gateway_identity import (
    gateway_url_from_id,
    resolve_gateway_id,
)


def _ssm(value="gw-from-ssm"):
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": value}}
    return ssm


def test_explicit_gateway_id_wins_and_skips_ssm():
    ssm = _ssm()
    assert resolve_gateway_id(gateway_id="gw-explicit", ssm_client=ssm) == "gw-explicit"
    ssm.get_parameter.assert_not_called()


def test_env_override_skips_ssm(monkeypatch):
    monkeypatch.setenv("AGENTCORE_GATEWAY_ID", "gw-from-env")
    ssm = _ssm()
    assert resolve_gateway_id(project_prefix="myproj", ssm_client=ssm) == "gw-from-env"
    ssm.get_parameter.assert_not_called()


def test_resolves_from_ssm_infra_param(monkeypatch):
    monkeypatch.delenv("AGENTCORE_GATEWAY_ID", raising=False)
    ssm = _ssm("dev-boisestateai-v2-mcp-gateway-abc123")
    out = resolve_gateway_id(project_prefix="dev-boisestateai-v2", ssm_client=ssm)
    assert out == "dev-boisestateai-v2-mcp-gateway-abc123"
    ssm.get_parameter.assert_called_once_with(Name="/dev-boisestateai-v2/gateway/id")


def test_missing_ssm_param_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("AGENTCORE_GATEWAY_ID", raising=False)
    ssm = MagicMock()
    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": "x"}}, "GetParameter"
    )
    with pytest.raises(RuntimeError, match="gateway construct deployed"):
        resolve_gateway_id(project_prefix="myproj", ssm_client=ssm)


def test_gateway_url_from_id_is_deterministic():
    assert gateway_url_from_id("gw-xyz", region="us-west-2") == (
        "https://gw-xyz.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
    )
