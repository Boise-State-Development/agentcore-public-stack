"""Hide specific MCP tools from a client's model-facing tool list (PR-6b).

Gateway and external MCP servers are handed to the Strands ``Agent`` as
*client objects*, not as individual callables: Strands enumerates each
client's tools through its ``list_tools_sync`` and adds one ``MCPAgentTool``
per server tool. That is exactly why PR-6a could fold only LOCAL tools — there
was no per-tool object to pull out of the top-level list.

This module supplies the missing seam. A skill that binds a gateway/external
tool registers that tool's *agent-facing name* as **folded** on the owning
client; the client's ``list_tools_sync`` then drops it, so its schema never
rides in the model's context. The tool stays fully executable: the client
object remains in the agent's tool list (Strands keeps its session alive), and
``skill_executor`` invokes the folded tool through the client's
``call_tool_sync`` (see ``agents/main_agent/skills/mcp_binding.py``).

Free functions (rather than a base class) so any ``MCPClient`` subclass opts in
with two lines in its ``list_tools_sync`` and no change to its hierarchy — and
so the mechanism works for clients constructed before a fold set is known.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List

logger = logging.getLogger(__name__)

#: Attribute the fold set is stashed under on a client. Private/namespaced so
#: it never collides with Strands' own attributes.
_FOLD_ATTR = "_skill_folded_tool_names"


def set_folded_tool_names(client: Any, names: Iterable[str]) -> None:
    """Mark ``names`` as folded on ``client`` (hidden from its model tool list).

    Idempotently *adds* to any existing fold set so binding two skills to the
    same client accumulates rather than overwrites. Also invalidates the
    client's cached ``_loaded_tools`` (set by a prior ``load_tools`` pre-flight)
    so Strands re-lists through ``list_tools_sync`` with the fold applied — the
    external pre-flight in ``BaseAgent._build_filtered_tools`` primes that cache
    *before* folds are known, and without this reset the model would still see
    the folded schemas.
    """
    existing = getattr(client, _FOLD_ATTR, None)
    folded = set(existing) if existing else set()
    folded.update(names)
    setattr(client, _FOLD_ATTR, folded)

    # Drop any cached tool list so the next load_tools() re-lists with the fold.
    if getattr(client, "_loaded_tools", None) is not None:
        try:
            client._loaded_tools = None
        except Exception:  # pragma: no cover - defensive; attr is a plain field
            logger.debug("could not reset _loaded_tools on %r", client, exc_info=True)


def folded_tool_names(client: Any) -> set:
    """Return the (copy of the) fold set on ``client``, or an empty set."""
    existing = getattr(client, _FOLD_ATTR, None)
    return set(existing) if existing else set()


def drop_folded_tools(client: Any, tools: List[Any]) -> List[Any]:
    """Filter folded tools out of a ``list_tools_sync`` result.

    Matches on the agent-facing ``tool_name`` (the name the model and
    ``skill_executor`` use). A no-op when nothing is folded, so it is safe to
    call unconditionally from any client's ``list_tools_sync``.
    """
    folded = getattr(client, _FOLD_ATTR, None)
    if not folded:
        return tools
    kept = [t for t in tools if getattr(t, "tool_name", None) not in folded]
    if len(kept) != len(tools):
        logger.info(
            "Folded %d MCP tool(s) out of the model tool list (behind skill "
            "meta-tools): %s",
            len(tools) - len(kept),
            sorted(folded),
        )
    return kept
