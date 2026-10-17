"""Keep one tool per name, so a clash costs a tool rather than the turn.

Strands' ``ToolRegistry.register_tool`` raises on a second tool with a name it
already holds (or one that differs only by ``-`` vs ``_``), and
``process_tools`` turns that into "Failed to load tool <MCPClient ...>: Tool
name 'x' already exists". The agent is never built, so every conversation of
every user who has both sources enabled fails. Two MCP servers fronting the
same upstream (dev, 2026-10-17: two catalog entries pointing at Zoom's hosted
server, both exposing ``get_file_content``) are enough.

``resolve_tool_name_conflicts`` runs over the build's tool list before the
agent sees it. The first source to claim a name keeps it, so the outcome
follows the list order, which is already deterministic (built-in tools, then
gateway, then external servers in enabled-id order, then context-bound tools),
and ``toolConfig`` stays a stable cache prefix. A later MCP server that clashes
loses only the clashing tools: it is wrapped in a ``ToolProvider`` that hides
them for this agent alone. The wrapped client is shared through
``ExternalMCPIntegration``'s cache with other agents that may have no clash, so
it is never changed itself.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from strands.tools import ToolProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkippedToolName:
    """One tool left out because an earlier source already holds its name."""

    name: str
    #: The source whose copy was left out.
    source: str
    #: The source whose copy the model has under that name.
    kept_source: str


class NameExcludingToolProvider(ToolProvider):
    """A per-agent view of a shared ``ToolProvider`` without some tool names.

    Lifecycle calls go to the wrapped provider, so its consumer counting is
    unchanged. Tools keep pointing at the wrapped client (``mcp_client``), which
    is what the OAuth, approval and context-breakdown lookups key on.
    """

    def __init__(self, inner: ToolProvider, excluded_names: FrozenSet[str]):
        self.inner = inner
        self.excluded_names = excluded_names

    async def load_tools(self, **kwargs: Any) -> Sequence[Any]:
        tools = await self.inner.load_tools(**kwargs)
        return [t for t in tools if t.tool_name not in self.excluded_names]

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.inner.add_consumer(consumer_id, **kwargs)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.inner.remove_consumer(consumer_id, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Labels and URLs (`context_label`, `server_url`) read through.
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)


def _normalized(name: str) -> str:
    # Strands rejects names that differ only by '-' vs '_' as duplicates too.
    return name.replace("-", "_")


def _source_label(entry: Any) -> str:
    label = getattr(entry, "context_label", None)
    if isinstance(label, str) and label:
        return label
    if isinstance(entry, ToolProvider):
        return type(entry).__name__
    return "built-in tools"


def _list_provider_tools(providers: List[ToolProvider]) -> Dict[int, Optional[List[Any]]]:
    """Each provider's tools, keyed by ``id``; None where listing failed.

    External MCP clients were pre-flighted, so this is a read of their cached
    list. A provider that has not listed yet (the gateway client) lists here,
    which Strands would do a moment later anyway. A failure is left for the
    registry to report exactly as it did before, so it is not raised here.

    ``load_tools`` is a coroutine and the build may be running on an event
    loop's thread, so it runs on a worker thread with its own loop, the same
    bridge Strands' registry uses.
    """

    async def _list_all() -> Dict[int, Optional[List[Any]]]:
        listed: Dict[int, Optional[List[Any]]] = {}
        for provider in providers:
            try:
                listed[id(provider)] = list(await provider.load_tools())
            except Exception as exc:  # noqa: BLE001 - the registry reports it
                logger.warning(
                    "Could not list %s's tools to check for name clashes: %s",
                    _source_label(provider),
                    exc,
                )
                listed[id(provider)] = None
        return listed

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, _list_all()).result()


def resolve_tool_name_conflicts(
    tools: List[Any],
) -> Tuple[List[Any], List[SkippedToolName]]:
    """Return ``tools`` with every tool name held once, and what was left out.

    Entries that are not tools or providers (none today) pass through.
    ``skipped`` is sorted, so the note rendered from it is byte-stable.
    """
    providers = [t for t in tools if isinstance(t, ToolProvider)]
    provider_tools = _list_provider_tools(providers) if providers else {}

    held: Dict[str, str] = {}  # normalized name -> source holding it
    resolved: List[Any] = []
    skipped: List[SkippedToolName] = []

    for entry in tools:
        source = _source_label(entry)
        if isinstance(entry, ToolProvider):
            listed = provider_tools.get(id(entry))
            if listed is None:
                resolved.append(entry)
                continue
            excluded = set()
            for tool in listed:
                key = _normalized(tool.tool_name)
                if key in held:
                    excluded.add(tool.tool_name)
                    skipped.append(SkippedToolName(tool.tool_name, source, held[key]))
                else:
                    held[key] = source
            resolved.append(
                NameExcludingToolProvider(entry, frozenset(excluded)) if excluded else entry
            )
            continue

        name = getattr(entry, "tool_name", None)
        if not isinstance(name, str):
            resolved.append(entry)
            continue
        key = _normalized(name)
        if key in held:
            skipped.append(SkippedToolName(name, source, held[key]))
            continue
        held[key] = source
        resolved.append(entry)

    for s in skipped:
        logger.warning(
            "Tool name clash: left out %s's '%s'; the agent keeps the one from %s",
            s.source,
            s.name,
            s.kept_source,
        )
    skipped.sort(key=lambda s: (s.name, s.source, s.kept_source))
    return resolved, skipped


def skipped_tools_note(skipped: List[SkippedToolName]) -> str:
    """The system-prompt note naming tools left out for a name clash.

    Changes only when the set of clashes does, which means ``toolConfig``
    changed with it, so it adds no prompt-cache writes of its own.
    """
    lines = [
        f"- `{s.name}` from {s.source} (the `{s.name}` in your tool list is {s.kept_source}'s)"
        for s in skipped
    ]
    return (
        "## Tools left out because of a name clash\n"
        "Two enabled sources offer tools with the same name, so only the first "
        "is available in this conversation:\n"
        + "\n".join(lines)
        + "\nIf a request needs a left-out tool, tell the user it is unavailable "
        "here because of a tool-name clash, and that turning off one of the two "
        "sources fixes it."
    )
