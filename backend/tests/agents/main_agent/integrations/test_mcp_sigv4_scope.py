"""Tests that AWS IAM SigV4 signing is scoped to recognized AWS endpoints.

The ``/admin/tools/discover`` endpoint deliberately accepts arbitrary
URLs from authenticated admins (the same trust extended to saved MCP
tool configurations). What it must *not* do is sign the outbound
discovery request with the task's IAM credentials when the destination
is not actually an AWS service — that would leak the temporary
credential set in the Authorization / X-Amz-Security-Token headers to
whatever host the URL resolves to.

The boundary lives in ``detect_aws_service_from_url``: it returns a
recognized service name for AWS-shaped URLs and ``None`` for everything
else. Non-AWS targets paired with ``auth_type=aws-iam`` are refused at
the client-construction layer; arbitrary URLs paired with
``auth_type=none`` (or other non-signing modes) still go through, since
no credentials are at stake.
"""

from __future__ import annotations

import pytest

from agents.main_agent.integrations.external_mcp_client import (
    create_external_mcp_client,
    detect_aws_service_from_url,
)
from apis.shared.tools.models import MCPAuthType, MCPServerConfig, MCPTransport


# ---------------------------------------------------------------------------
# detect_aws_service_from_url: recognized hosts only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://abc.lambda-url.us-west-2.on.aws/", "lambda"),
        ("https://abc.lambda-url.eu-west-1.on.aws/path", "lambda"),
        ("https://api.execute-api.us-east-1.amazonaws.com/prod", "execute-api"),
        ("https://gw.bedrock-agentcore.us-west-2.amazonaws.com/mcp", "bedrock-agentcore"),
    ],
)
def test_returns_service_for_known_aws_url(url: str, expected: str) -> None:
    assert detect_aws_service_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://attacker.example.com/mcp",
        "http://internal-host.local/mcp",
        "https://my-mcp-server.herokuapp.com/mcp",
        "https://lambda-url-lookalike.example.com/",
        # Looks AWS-ish but not an actual service hostname:
        "https://amazonaws.com.attacker.com/mcp",
        "https://lambda-url.fake.com/",
    ],
)
def test_returns_none_for_unknown_url(url: str) -> None:
    assert detect_aws_service_from_url(url) is None


# ---------------------------------------------------------------------------
# create_external_mcp_client: refuses SigV4 against non-AWS targets
# ---------------------------------------------------------------------------


def _config(server_url: str, auth_type: MCPAuthType = MCPAuthType.AWS_IAM) -> MCPServerConfig:
    return MCPServerConfig(
        server_url=server_url,
        transport=MCPTransport.STREAMABLE_HTTP,
        auth_type=auth_type,
    )


def test_aws_iam_against_non_aws_url_is_refused() -> None:
    """Constructing a SigV4-signed client for a non-AWS URL must fail
    closed. The client must not be returned to the caller, and no
    credentials are wired into a request that would reach the target."""
    client = create_external_mcp_client(
        config=_config("https://attacker.example.com/mcp")
    )
    assert client is None


def test_aws_iam_against_aws_url_is_constructed() -> None:
    client = create_external_mcp_client(
        config=_config("https://abc.lambda-url.us-west-2.on.aws/mcp")
    )
    assert client is not None


def test_non_signing_auth_against_non_aws_url_is_constructed() -> None:
    """auth_type=none paired with an arbitrary URL still constructs a
    client — no credentials are at stake, and the discovery endpoint's
    contract permits arbitrary URLs in the no-auth case."""
    client = create_external_mcp_client(
        config=_config("https://attacker.example.com/mcp", auth_type=MCPAuthType.NONE)
    )
    assert client is not None
