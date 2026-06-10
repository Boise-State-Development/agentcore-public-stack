"""Resolve a skill's gateway / external MCP bound tools into foldable adapters.

PR-6a folded only LOCAL callable tools behind ``skill_dispatcher`` /
``skill_executor``. Gateway and external MCP tools materialize as *client
objects* (one ``MCPClient`` exposing many tools), not individual callables, so
they could be made available but not hidden. This module closes that gap.

For each non-local ``bound_tool_id`` a skill carries, ``resolve_mcp_bindings``:

1. classifies it (gateway vs external) — reusing the agent's tool filter;
2. resolves it to the concrete MCP tool name(s) and the owning client —
   gateway ids expand via ``expand_gateway_tool_ids`` (catalog ``gateway_<id>``
   → runtime ``gateway_<target>___<tool>``; the gateway tool name is that with
   the ``gateway_`` prefix stripped); external ids map to the per-server client
   the external integration already built, whose tools are enumerated live
   (its session is active after the build-time ``load_tools`` pre-flight);
3. wraps each as a :class:`FoldedMCPTool` — a lightweight adapter the registry
   stores so ``skill_dispatcher`` can show its schema and ``skill_executor`` can
   run it through the client — and records its agent-facing name to fold off
   the client's model tool list.

The adapter executes via ``MCPClient.call_tool_sync`` rather than the local
``tool_obj(**input)`` path, because an ``MCPAgentTool`` is not a plain callable.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_GATEWAY_PREFIX = "gateway_"


class FoldedMCPTool:
    """Adapter exposing one folded gateway/external MCP tool to the registry.

    Duck-types the slice of the tool interface the skill registry + meta-tools
    use: ``tool_name`` (matched by ``skill_executor`` and used in catalog/schema
    output) and ``tool_spec`` (returned by ``skill_dispatcher`` so the model
    learns the parameters). Execution is routed through the MCP client, marked
    by ``is_mcp_folded`` so ``skill_executor`` calls :meth:`invoke` instead of
    treating it as a plain callable.
    """

    is_mcp_folded = True

    def __init__(
        self,
        client: Any,
        mcp_tool_name: str,
        agent_tool_name: Optional[str] = None,
        tool_spec: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Args:
            client: The ``MCPClient`` (gateway or external) that hosts the tool.
            mcp_tool_name: Server-side tool name, passed to ``call_tool_sync``.
            agent_tool_name: Agent-facing name (what the model calls). Defaults
                to ``mcp_tool_name`` (true for gateway; external tools may
                disambiguate, so it is captured explicitly there).
            tool_spec: Captured tool spec, when known at build time (external).
                When None (gateway, resolved without a live session), the spec
                is fetched lazily from the client on first access.
        """
        self._client = client
        self._mcp_tool_name = mcp_tool_name
        self._agent_tool_name = agent_tool_name or mcp_tool_name
        self._tool_spec = tool_spec

    @property
    def tool_name(self) -> str:
        return self._agent_tool_name

    @property
    def tool_spec(self) -> Dict[str, Any]:
        """The tool's parameter spec, resolved lazily for gateway tools.

        Lazy resolution lists the client (its session is live during the agent
        loop, when ``skill_dispatcher`` runs) and caches the match. Falls back
        to a name-only spec so the dispatcher always returns something usable.
        """
        if self._tool_spec is not None:
            return self._tool_spec

        spec: Dict[str, Any] = {"name": self._agent_tool_name}
        try:
            for t in self._client.list_tools_sync():
                if getattr(t, "tool_name", None) == self._agent_tool_name:
                    resolved = getattr(t, "tool_spec", None)
                    if resolved:
                        spec = resolved
                    break
        except Exception:  # noqa: BLE001 - never break dispatch on a list failure
            logger.debug(
                "Could not resolve tool_spec for folded MCP tool %s",
                self._agent_tool_name,
                exc_info=True,
            )
        self._tool_spec = spec
        return spec

    def invoke(self, tool_input: Optional[dict]) -> Any:
        """Execute the tool through the MCP client and return a string result.

        Synthesizes a ``tool_use_id`` (the executor path has none) and reduces
        the structured ``MCPToolResult`` to text/JSON the model can read.
        """
        try:
            result = self._client.call_tool_sync(
                tool_use_id=f"skill-{uuid.uuid4().hex}",
                name=self._mcp_tool_name,
                arguments=tool_input or {},
            )
        except Exception as e:  # noqa: BLE001 - surface as a tool error, not a crash
            logger.error(
                "Folded MCP tool %s failed: %s", self._agent_tool_name, e,
                exc_info=True,
            )
            return json.dumps({"error": str(e)})
        return _stringify_mcp_result(result)


def _stringify_mcp_result(result: Any) -> str:
    """Reduce an ``MCPToolResult`` to a string for the model.

    Joins text content blocks; falls back to a JSON dump of the content (or the
    whole result) so non-text results still surface something. ``MCPToolResult``
    is a TypedDict (``{"content": [...], "status": ...}``); handle dict-shaped
    and attribute-shaped results defensively.
    """
    content = None
    if isinstance(result, dict):
        content = result.get("content")
    else:
        content = getattr(result, "content", None)

    if isinstance(content, list):
        texts: List[str] = []
        for block in content:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if isinstance(text, str):
                texts.append(text)
        if texts:
            return "\n".join(texts)
        try:
            return json.dumps(content, default=str)
        except (TypeError, ValueError):
            return str(content)

    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError):
        return str(result)


class MCPBindingResult:
    """Outcome of :func:`resolve_mcp_bindings`.

    Attributes:
        catalog_map: ``{catalog_tool_id -> [FoldedMCPTool, ...]}`` to hand to
            ``SkillRegistry.bind_catalog_tools`` (a catalog id can expand to
            several tools — a gateway target or external server with many).
        fold_by_client: ``{client -> {agent_tool_name, ...}}`` to apply with
            ``set_folded_tool_names`` so the bound tools drop off the model
            tool list.
        unresolved: catalog ids that could not be resolved (gateway disabled,
            external client absent, …) — left visible/unfolded and logged.
    """

    def __init__(self) -> None:
        self.catalog_map: Dict[str, List[FoldedMCPTool]] = {}
        self.fold_by_client: Dict[Any, set] = {}
        self.unresolved: List[str] = []

    def _add(self, catalog_id: str, client: Any, tool: FoldedMCPTool) -> None:
        self.catalog_map.setdefault(catalog_id, []).append(tool)
        self.fold_by_client.setdefault(client, set()).add(tool.tool_name)


def resolve_mcp_bindings(
    *,
    gateway_ids: List[str],
    external_ids: List[str],
    gateway_client: Any,
    expand_gateway: Callable[[List[str]], List[str]],
    external_client_lookup: Callable[[str], Any],
) -> MCPBindingResult:
    """Resolve gateway/external bound catalog ids to foldable MCP tools.

    Args:
        gateway_ids: bound catalog ids classified as gateway.
        external_ids: bound catalog ids classified as external MCP.
        gateway_client: the single live gateway ``MCPClient`` (or None when
            gateway is disabled / no gateway tools were materialized).
        expand_gateway: maps catalog ``gateway_<id>`` → runtime
            ``gateway_<target>___<tool>`` ids (``BaseAgent._expand_gateway_tool_ids``).
        external_client_lookup: maps an external catalog ``tool_id`` to its live
            ``MCPClient`` (or None) — typically
            ``lambda tid: integration.get_client(tid, user_id)``.
    """
    result = MCPBindingResult()

    _resolve_gateway(result, gateway_ids, gateway_client, expand_gateway)
    _resolve_external(result, external_ids, external_client_lookup)

    if result.unresolved:
        logger.info(
            "Skill-bound MCP tools could not be folded (kept visible): %s",
            result.unresolved,
        )
    return result


def _resolve_gateway(
    result: MCPBindingResult,
    gateway_ids: List[str],
    gateway_client: Any,
    expand_gateway: Callable[[List[str]], List[str]],
) -> None:
    if not gateway_ids:
        return
    if gateway_client is None:
        result.unresolved.extend(gateway_ids)
        return

    for catalog_id in gateway_ids:
        # A catalog gateway id can expand to several runtime per-tool ids; an
        # already-expanded RBAC-direct id (`gateway_<target>___<tool>`) maps to
        # itself. The gateway tool name is the runtime id minus the prefix.
        try:
            runtime_ids = expand_gateway([catalog_id])
        except Exception:  # noqa: BLE001 - degrade to unresolved on lookup failure
            logger.warning(
                "Could not expand gateway id %s for skill binding",
                catalog_id,
                exc_info=True,
            )
            result.unresolved.append(catalog_id)
            continue

        resolved_any = False
        for rid in runtime_ids:
            tool_name = rid[len(_GATEWAY_PREFIX):] if rid.startswith(_GATEWAY_PREFIX) else rid
            result._add(
                catalog_id,
                gateway_client,
                FoldedMCPTool(gateway_client, mcp_tool_name=tool_name),
            )
            resolved_any = True
        if not resolved_any:
            result.unresolved.append(catalog_id)


def _resolve_external(
    result: MCPBindingResult,
    external_ids: List[str],
    external_client_lookup: Callable[[str], Any],
) -> None:
    for catalog_id in external_ids:
        client = external_client_lookup(catalog_id)
        if client is None:
            result.unresolved.append(catalog_id)
            continue

        # Binding an external catalog id binds its whole MCP server; enumerate
        # the server's tools (session is live post-load_tools) so each folds
        # individually and carries its captured spec.
        try:
            tools = list(client.list_tools_sync())
        except Exception:  # noqa: BLE001 - degrade to unresolved on list failure
            logger.warning(
                "Could not list external MCP tools for skill binding (%s)",
                catalog_id,
                exc_info=True,
            )
            result.unresolved.append(catalog_id)
            continue

        if not tools:
            result.unresolved.append(catalog_id)
            continue

        for t in tools:
            agent_name = getattr(t, "tool_name", None)
            if not agent_name:
                continue
            mcp_name = getattr(getattr(t, "mcp_tool", None), "name", None) or agent_name
            result._add(
                catalog_id,
                client,
                FoldedMCPTool(
                    client,
                    mcp_tool_name=mcp_name,
                    agent_tool_name=agent_name,
                    tool_spec=getattr(t, "tool_spec", None),
                ),
            )
