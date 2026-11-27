"""The tools a turn's agent would carry, with no agent (quiet-watcher triage spec §4.1).

A watch triage replays a few of a full run's tool calls. It needs the same tool objects that
run's agent had: registry tools, Gateway targets resolved per user, external MCP servers loaded
with the user's token and consent gate, and the context-bound tools the route built. Those are
assembled by ``BaseAgent._register_external_mcp_tools`` and ``BaseAgent._build_filtered_tools``.

:class:`TurnToolSet` runs exactly those two methods, inherited and unchanged, and nothing else
of ``BaseAgent.__init__``: no model config, system prompt, session manager, history or Strands
``Agent``. So the triage's tools are the turn's by construction, never a copy that could drift,
and the turn path is untouched. A test pins the attributes the two methods read
(``tests/agents/test_turn_tool_set.py``).

:func:`load_tools` then hands that list to a Strands ``ToolRegistry`` the way an ``Agent`` does
(``process_tools``: MCP clients load their tools, decorated functions are wrapped), and returns
the tools by name with the registry, whose ``cleanup()`` releases the MCP clients.

Blocking (catalog reads, MCP pre-flight on a worker thread): call it with ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from strands.tools.registry import ToolRegistry

from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.tools.gateway_integration import GatewayIntegration
from agents.main_agent.tools.tool_filter import ToolFilter
from agents.main_agent.tools.tool_registry import create_default_registry

logger = logging.getLogger(__name__)


class TurnToolSet(BaseAgent):
    """``BaseAgent``'s tool assembly alone. Never streams; never builds a Strands ``Agent``."""

    def __init__(  # noqa: D107 - documented on the class; deliberately skips BaseAgent.__init__
        self,
        *,
        user_id: str,
        auth_token: Optional[str],
        enabled_tools: Optional[List[str]],
        extra_tools: Optional[List[Any]] = None,
    ) -> None:
        # Exactly the state BaseAgent.__init__ gives the two methods below, and no more.
        self.session_id = None
        self.user_id = user_id
        self.auth_token = auth_token
        self.enabled_tools = enabled_tools
        self.extra_tools = list(extra_tools or [])
        self.consent_gated_tools: List[Any] = []
        self.skipped_tool_names: List[Any] = []
        self.tool_registry = create_default_registry()
        self.tool_filter = ToolFilter(self.tool_registry)
        self._register_external_mcp_tools()
        self.gateway_integration = GatewayIntegration()

    def _create_agent(self) -> None:
        raise NotImplementedError("TurnToolSet builds tools only")

    async def stream_async(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError("TurnToolSet builds tools only")
        yield  # pragma: no cover - makes this an async generator, as the base declares

    def build(self) -> List[Any]:
        """The list a turn hands to its Strands ``Agent``: local tools, then MCP clients."""
        return self._build_filtered_tools()


def load_tools(
    *,
    user_id: str,
    auth_token: Optional[str],
    enabled_tools: Optional[List[str]],
    extra_tools: Optional[List[Any]] = None,
) -> Tuple[Dict[str, Any], ToolRegistry, List[Any]]:
    """The turn's tools by model-facing name, the registry holding them, and the consent-gated servers.

    The caller must call ``registry.cleanup()`` when done, as an ``Agent`` does on teardown. A
    server the user has not connected is left out (``consent_gated``), as it is on a turn.
    """
    tool_set = TurnToolSet(
        user_id=user_id,
        auth_token=auth_token,
        enabled_tools=enabled_tools,
        extra_tools=extra_tools,
    )
    tools = tool_set.build()
    registry = ToolRegistry()
    try:
        registry.process_tools(tools)
    except Exception:
        registry.cleanup()
        raise
    return dict(registry.registry), registry, list(tool_set.consent_gated_tools)


__all__ = ["TurnToolSet", "load_tools"]
