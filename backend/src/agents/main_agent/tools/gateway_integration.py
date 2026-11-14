"""
Gateway MCP client integration for managed tool execution
"""
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Any, Set
from agents.main_agent.integrations.gateway_mcp_client import (
    GatewayAuthError,
    get_gateway_client_if_enabled,
)
from apis.shared.tools.scoped_ids import parse_scoped_tool_id
from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)


@dataclass
class GatewayToolResolution:
    """What the enabled gateway ids resolve to for one agent build.

    ``runtime_ids`` are the ``gateway_<target>___<tool>`` ids the
    ``FilteredMCPClient`` filters on. ``approval_names`` are the tool names the
    model calls (``<target>___<tool>``, no ``gateway_`` prefix) whose catalog
    entry is flagged ``needs_approval`` (#454). The target is part of the name,
    so a flag on one target never gates a same-named tool on another.
    """

    runtime_ids: List[str] = field(default_factory=list)
    approval_names: Set[str] = field(default_factory=set)


async def resolve_gateway_tools(
    gateway_tool_ids: List[str], repository: Any
) -> GatewayToolResolution:
    """Expand #419 catalog gateway tools and collect their approval flags.

    See `expand_gateway_tool_ids` for the expansion rules. Approval names come
    from the same catalog rows, so a gateway tool is gated exactly when the
    admin ticked "Needs approval" on its row. Runtime ids enabled directly via
    RBAC (no catalog row) and DYNAMIC targets (no curated tool list) carry no
    flags and are not gated, as before.
    """
    expanded: List[str] = []
    approval_names: Set[str] = set()
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

        if is_gateway and target:
            approval_names.update(
                f"{target}___{entry.name}"
                for entry in entries or []
                if getattr(entry, "needs_approval", False)
            )

        if tool_name is not None:
            # Scoped: a single tool of this target. The target name is required
            # to build the runtime id the FilteredMCPClient matches on.
            if is_gateway and target:
                expanded.append(f"gateway_{target}___{tool_name}")
            else:
                logger.warning(
                    "Cannot resolve scoped gateway tool '%s' (no target_name); skipping",
                    scrub_log(tool_id),
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
    runtime_ids = [x for x in expanded if not (x in seen or seen.add(x))]
    return GatewayToolResolution(runtime_ids=runtime_ids, approval_names=approval_names)


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
    return (await resolve_gateway_tools(gateway_tool_ids, repository)).runtime_ids


class GatewayIntegration:
    """Manages Gateway MCP client for Strands Managed Integration"""

    def __init__(self):
        """Initialize gateway integration"""
        self.client: Optional[Any] = None
        # Tool names (`<target>___<tool>`) that pause for user approval,
        # set with the client they belong to (#454).
        self._approval_names: Set[str] = set()

    def approval_names_for_client(self, client: Any) -> Set[str]:
        """Names that pause for approval when ``client`` is this agent's
        Gateway client; empty for any other client. Mirrors
        ``ExternalMCPIntegration.approval_names_for_client``."""
        if client is not None and client is self.client:
            return set(self._approval_names)
        return set()

    def get_client(
        self,
        enabled_gateway_tool_ids: List[str],
        auth_token: Optional[str] = None,
        approval_names: Optional[Set[str]] = None,
    ) -> Optional[Any]:
        """
        Get Gateway MCP client if gateway tools are enabled

        Args:
            enabled_gateway_tool_ids: List of gateway tool IDs (e.g., ["gateway_wikipedia", "gateway_arxiv"])
            auth_token: The current user's Cognito access token, forwarded as a
                Bearer token to the JWT-authorized Gateway. Required unless the
                Gateway is running in legacy ``iam`` inbound-auth mode.
            approval_names: Tool names on this client that pause for user
                approval (see ``GatewayToolResolution.approval_names``).

        Returns:
            MCPClient instance or None if not available

        Note:
            The client is per-user because the bearer token is. This integration
            is instantiated per agent (``BaseAgent.__init__``), so the token
            never outlives the invocation it belongs to.
        """
        if not enabled_gateway_tool_ids:
            logger.info("No gateway tools requested")
            return None

        # Get Gateway MCP client (Strands 1.16+ Managed Integration)
        # Store as instance variable to keep session alive during Agent lifecycle
        try:
            self.client = get_gateway_client_if_enabled(
                enabled_tool_ids=enabled_gateway_tool_ids,
                auth_token=auth_token,
            )
        except GatewayAuthError as e:
            # Degrade gracefully: the turn proceeds without Gateway tools rather
            # than failing outright. Every Gateway call would 401 anyway, and a
            # user with no token cannot be served these tools at all.
            logger.warning(f"⚠️  Gateway tools unavailable: {e}")
            self.client = None
            self._approval_names = set()
            return None

        self._approval_names = set(approval_names or ()) if self.client else set()
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
