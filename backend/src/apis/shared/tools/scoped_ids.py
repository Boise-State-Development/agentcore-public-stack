"""Scoped tool identifiers — referencing an individual tool within an MCP server.

A bare catalog tool id (e.g. ``fetch_url_content`` or ``gateway_class_search``)
means "the whole server / all of its tools" — the historical behaviour. A
*scoped* id ``<catalog_tool_id>::<mcp_tool_name>`` references a single tool the
server exposes, so a skill binding or a user preference can carry a subset of a
server's tools instead of all of them.

This module is the one place that knows the on-the-wire scoping format. It is
shared by the app_api validation layer and the agents runtime (import-boundary
safe — depends on nothing else in the package).
"""
from typing import Dict, Iterable, List, Optional, Set, Tuple

# Delimiter between a catalog tool id and an individual MCP tool name. Chosen so
# it cannot collide with catalog ids (which use single underscores) or with the
# gateway runtime convention ``gateway_<target>___<tool>`` (triple underscore).
SCOPE_DELIMITER = "::"


def is_scoped_tool_id(tool_id: str) -> bool:
    """True if ``tool_id`` references a single tool within a server."""
    return SCOPE_DELIMITER in tool_id


def make_scoped_tool_id(catalog_tool_id: str, mcp_tool_name: str) -> str:
    """Build the scoped id for one tool of an MCP-server catalog tool."""
    return f"{catalog_tool_id}{SCOPE_DELIMITER}{mcp_tool_name}"


def parse_scoped_tool_id(tool_id: str) -> Tuple[str, Optional[str]]:
    """Split a possibly-scoped id into ``(catalog_tool_id, mcp_tool_name)``.

    A bare id returns ``(tool_id, None)``. A scoped id returns the base catalog
    id and the individual tool name. Only the first delimiter splits, so an
    (unlikely) tool name containing the delimiter is preserved verbatim.
    """
    if SCOPE_DELIMITER not in tool_id:
        return tool_id, None
    base, _, name = tool_id.partition(SCOPE_DELIMITER)
    name = name.strip()
    return base, (name or None)


def base_tool_id(tool_id: str) -> str:
    """The catalog tool id that a (possibly-scoped) id refers to."""
    return parse_scoped_tool_id(tool_id)[0]


def base_tool_ids(tool_ids: Iterable[str]) -> List[str]:
    """De-duplicated catalog ids referenced by an id list, order-preserving."""
    seen: Dict[str, None] = {}
    for tid in tool_ids:
        seen.setdefault(base_tool_id(tid), None)
    return list(seen.keys())


def collect_tool_name_filters(
    tool_ids: Iterable[str],
) -> Dict[str, Optional[Set[str]]]:
    """Map each referenced catalog id to its selected tool-name filter.

    The value is ``None`` when the whole server is selected — a bare id is
    present for that catalog id, which wins over any scoped ids for the same
    server — or a set of individual tool names when only a subset is selected.
    Catalog ids keep their first-seen order (callers downstream rely on a
    deterministic order, e.g. when one server's client fails to start).

    Example::

        collect_tool_name_filters(["a", "b::x", "b::y", "c", "c::z"])
        # -> {"a": None, "b": {"x", "y"}, "c": None}
        #    'c' is whole-server because the bare 'c' is present alongside 'c::z'.
    """
    order: List[str] = []
    whole: Set[str] = set()
    scoped: Dict[str, Set[str]] = {}
    for tid in tool_ids:
        base, name = parse_scoped_tool_id(tid)
        if base not in whole and base not in scoped:
            order.append(base)
        if name is None:
            whole.add(base)
        else:
            scoped.setdefault(base, set()).add(name)

    return {base: (None if base in whole else scoped[base]) for base in order}
