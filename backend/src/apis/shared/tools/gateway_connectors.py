"""Built-in AgentCore Gateway connectors an admin can register as a target.

A *connector* target is an AWS-operated MCP tool hosted behind the Gateway:
there is no endpoint to deploy or credential to manage, the Gateway signs to it
with its own execution role, and its tool schema is fixed by the connector
version. Registering one goes through the same admin lifecycle as an external
MCP server target (`GatewayTargetService`), which is why it lives on
`MCPGatewayConfig` rather than as its own protocol: the catalog row, RBAC,
the user's tool picker and the agent's `gateway_<target>___<tool>` filtering
all work unchanged.

Only connectors listed here can be registered. Each needs its own
`parameterValues` shape and its own IAM action on the Gateway role (see
`AgentCoreGatewayConstruct`), so adding one is a deliberate change, not a
free-text field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class GatewayConnectorSpec:
    """What the platform needs to know to register one connector.

    `version` is pinned rather than left to the connector's moving default:
    the Gateway snapshots the tool schema at registration, and that schema
    reaches every turn's `toolConfig`, which sits ahead of the system prompt
    in the cacheable prefix. A default-version bump AWS makes on its side must
    never reach a live target unannounced; adopting one is an admin update.
    """

    connector_id: str
    version: str
    # `configurations[].name` in CreateGatewayTarget, and the tool name the
    # Gateway lists (`<targetName>___<tool_name>`). The same string for every
    # connector registered here so far.
    tool_name: str
    tool_description: str
    display_name: str
    supports_domain_filter: bool


WEB_SEARCH = GatewayConnectorSpec(
    connector_id="web-search",
    # 1.3.0 (2026-10-19) adds the read-only / open-world MCP annotations; the
    # input schema and results are 1.2.0's, which added request-level domain
    # and published-date filters and the target-level include list.
    version="1.3.0",
    tool_name="WebSearch",
    tool_description=(
        "Search the web (Amazon-operated index). Returns relevant snippets "
        "with source URLs, titles and publication dates."
    ),
    display_name="AgentCore Web Search",
    supports_domain_filter=True,
)

GATEWAY_CONNECTORS: Dict[str, GatewayConnectorSpec] = {
    WEB_SEARCH.connector_id: WEB_SEARCH,
}

#: Per-list cap on a target-level domain filter. AWS documents 100 per list
#: for the request-level filter and states no separate target-level limit, so
#: we hold the target to the same number rather than discover one in prod.
MAX_DOMAIN_FILTER_ENTRIES = 100


def get_connector_spec(connector_id: str) -> GatewayConnectorSpec:
    """Return the spec for `connector_id`, or raise ValueError naming the options."""
    spec = GATEWAY_CONNECTORS.get(connector_id)
    if spec is None:
        supported = ", ".join(sorted(GATEWAY_CONNECTORS))
        raise ValueError(
            f"Unsupported Gateway connector '{connector_id}'. Supported: {supported}"
        )
    return spec
