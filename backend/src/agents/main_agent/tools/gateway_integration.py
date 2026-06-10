"""
Gateway MCP client integration for managed tool execution
"""
import logging
from typing import List, Optional, Any
from agents.main_agent.integrations.gateway_mcp_client import get_gateway_client_if_enabled
from apis.shared.tools.scoped_ids import parse_scoped_tool_id

logger = logging.getLogger(__name__)


async def expand_gateway_tool_ids(
    gateway_tool_ids: List[str], repository: Any
) -> List[str]:
    """Expand #419 catalog gateway tools into the gateway's per-tool ids.

    A protocol='mcp' catalog tool carries a single id like
    ``gateway_class_search``, but the AgentCore Gateway exposes its target's
    tools as ``<targetName>___<toolName>`` (runtime id
    ``gateway_<targetName>___<toolName>``) — which is what ``FilteredMCPClient``
    matches on. Expand each catalog tool from the ``target_name`` + ``tools``
    list on its ``mcp_gateway_config``.

    A *scoped* catalog id (``gateway_class_search::search``) selects one tool of
    the target and expands to just ``gateway_<target>___search`` — this is how a
    skill or a user enables a subset of a gateway target's tools.

    Ids that already contain ``___`` are runtime gateway ids enabled directly
    via RBAC (no catalog row) and pass through unchanged, as do bare catalog
    tools with no curated tool list (e.g. DYNAMIC listing). Order-preserving and
    de-duplicated.

    Args:
        gateway_tool_ids: enabled ids that the filter classified as gateway
            (bare catalog ids, scoped ``base::tool`` ids, or runtime ids).
        repository: tool-catalog repository exposing ``async get_tool(id)``.
    """
    expanded: List[str] = []
    for tool_id in gateway_tool_ids:
        if "___" in tool_id:
            # Already a runtime gateway id (RBAC-enabled, no catalog row).
            expanded.append(tool_id)
            continue
        base, tool_name = parse_scoped_tool_id(tool_id)
        tool = await repository.get_tool(base)
        cfg = getattr(tool, "mcp_gateway_config", None) if tool else None
        target = getattr(cfg, "target_name", None) if cfg else None
        entries = getattr(cfg, "tools", None) if cfg else None
        is_gateway = bool(tool and tool.protocol == "mcp")

        if tool_name is not None:
            # Scoped: a single tool of this target. The target name is required
            # to build the runtime id the FilteredMCPClient matches on.
            if is_gateway and target:
                expanded.append(f"gateway_{target}___{tool_name}")
            else:
                logger.warning(
                    "Cannot resolve scoped gateway tool '%s' (no target_name); skipping",
                    tool_id,
                )
            continue

        # Bare catalog id = the whole target.
        if is_gateway and target and entries:
            expanded.extend(f"gateway_{target}___{entry.name}" for entry in entries)
        else:
            # No curated list (DYNAMIC listing) or not a gateway tool — pass
            # the id through unchanged.
            expanded.append(tool_id)

    seen: set = set()
    return [x for x in expanded if not (x in seen or seen.add(x))]


class GatewayIntegration:
    """Manages Gateway MCP client for Strands Managed Integration"""

    def __init__(self):
        """Initialize gateway integration"""
        self.client: Optional[Any] = None

    def get_client(self, enabled_gateway_tool_ids: List[str]) -> Optional[Any]:
        """
        Get Gateway MCP client if gateway tools are enabled

        Args:
            enabled_gateway_tool_ids: List of gateway tool IDs (e.g., ["gateway_wikipedia", "gateway_arxiv"])

        Returns:
            MCPClient instance or None if not available
        """
        if not enabled_gateway_tool_ids:
            logger.info("No gateway tools requested")
            return None

        # Get Gateway MCP client (Strands 1.16+ Managed Integration)
        # Store as instance variable to keep session alive during Agent lifecycle
        self.client = get_gateway_client_if_enabled(enabled_tool_ids=enabled_gateway_tool_ids)

        if self.client:
            logger.info(f"✅ Gateway MCP client created (Managed Integration with Strands 1.16+)")
            logger.info(f"   Enabled Gateway tool IDs: {enabled_gateway_tool_ids}")
        else:
            logger.warning("⚠️  Gateway MCP client not available")

        return self.client

    def add_to_tool_list(self, tools: List[Any]) -> List[Any]:
        """
        Add Gateway MCP client to tool list if available

        Args:
            tools: Existing list of tools

        Returns:
            Updated tool list with gateway client (if available)
        """
        if self.client:
            # Using Managed Integration (Strands 1.16+) - pass MCPClient directly to Agent
            # Agent will automatically manage lifecycle and filter tools
            tools.append(self.client)
            logger.info(f"✅ Gateway MCP client added to tool list")

        return tools

    def is_available(self) -> bool:
        """
        Check if gateway client is available

        Returns:
            bool: True if gateway client is initialized
        """
        return self.client is not None
