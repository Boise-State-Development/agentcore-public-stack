"""Resolve the centralized AgentCore Gateway's identity from infra config.

One source of truth for "which gateway." Both the admin-side
``GatewayTargetService`` (which *registers* targets) and the agent-side gateway
MCP client (which *connects* to list/call tools) MUST resolve the same gateway —
otherwise targets an admin registers land on a gateway the agent never connects
to, and the tools silently never appear.

The gateway CDK construct publishes ``/{PROJECT_PREFIX}/gateway/id``; that SSM
parameter is authoritative in a deployed environment. ``AGENTCORE_GATEWAY_ID``
is only a local/CI escape hatch for when SSM isn't reachable (or
``PROJECT_PREFIX`` differs) — not part of the normal cloud path.
"""

import os
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError


def _default_region(region: Optional[str]) -> str:
    return (
        region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
    )


def resolve_gateway_id(
    *,
    gateway_id: Optional[str] = None,
    project_prefix: Optional[str] = None,
    region: Optional[str] = None,
    ssm_client: Any = None,
) -> str:
    """Resolve the AgentCore Gateway identifier from infra config.

    Resolution order: an explicit ``gateway_id`` → the ``AGENTCORE_GATEWAY_ID``
    env override (local/CI escape hatch) → the SSM parameter
    ``/{PROJECT_PREFIX}/gateway/id`` (published by the gateway CDK construct;
    authoritative in a deployed environment).

    Raises:
        RuntimeError: if the SSM parameter is unavailable and no override is set.
    """
    if gateway_id:
        return gateway_id

    env_override = os.environ.get("AGENTCORE_GATEWAY_ID")
    if env_override:
        return env_override

    prefix = project_prefix or os.environ.get("PROJECT_PREFIX", "agentcore")
    region = _default_region(region)
    ssm = ssm_client or boto3.client("ssm", region_name=region)

    param_name = f"/{prefix}/gateway/id"
    try:
        response = ssm.get_parameter(Name=param_name)
    except ClientError as err:
        raise RuntimeError(
            f"Gateway id SSM parameter '{param_name}' is unavailable; is the "
            f"gateway construct deployed (and PROJECT_PREFIX correct)? Set "
            f"AGENTCORE_GATEWAY_ID to override for local/CI. ({err})"
        ) from err

    return response["Parameter"]["Value"]


def gateway_url_from_id(gateway_id: str, region: Optional[str] = None) -> str:
    """Build the gateway's MCP endpoint URL from its identifier.

    AgentCore Gateway URLs are deterministic:
    ``https://{id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp``.
    """
    return (
        f"https://{gateway_id}.gateway.bedrock-agentcore."
        f"{_default_region(region)}.amazonaws.com/mcp"
    )
