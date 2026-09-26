"""Per-process TTL caches over the tool catalog.

Three caches live here, all backed by DynamoDB reads of the tool
catalog:

1. **Per-tool freshness tokens** (`get_tool_updated_at`,
   `get_freshness_hash`). Cheap change-detection signal for the agent
   and MCP-client caches: any admin edit to a tool bumps its
   `updated_at`, so including the freshness hash in a cache key causes
   the next build to miss and rebuild with the fresh config.

2. **All-known-tool-ids snapshot** (`get_all_tool_ids`). The set of
   tool IDs known to the catalog — the source of truth for the
   "universe of tools" that authorization (`ToolAccessService`) needs
   to enumerate. Wildcard-access users in particular need this to know
   which tools to expand `*` into, and to validate requested tools
   against.

3. **Public-tool-ids snapshot** (`get_public_tool_ids`). The set of
   tool IDs flagged `isPublic` in the catalog — "available to every
   authenticated user regardless of role". `AppRoleService` unions this
   into a caller's role grant so the access *checks* honour the same
   flag the tool *picker* already does (see the module note there).

4. **Always-on-tool-ids snapshot** (`get_always_on_tool_ids`). The ids
   an admin has pinned — bare catalog ids for a whole tool, and scoped
   `base::name` ids for individual tools of an MCP server. Rides the
   same catalog read as (2) and (3) precisely so that resolving the
   pinned set on the chat path costs no DynamoDB round trip: it is the
   latency budget in docs/specs/admin-always-on-tools.md §4.1, and a
   separate cache here would spend exactly what that section refuses to.

Reads are TTL-cached so the per-turn overhead is bounded to at most
one DynamoDB read per cache key per TTL window, per process. Admin
routes call `invalidate(tool_id)` after a write so same-process
visibility is immediate; other processes see the change within one
TTL window. `invalidate` clears the id snapshots too, since any
create/delete shifts those sets — and toggling `isPublic` on an
existing tool shifts the public one.
"""

import asyncio
import hashlib
import logging
import time
from typing import Dict, FrozenSet, List, Optional, Tuple
from apis.shared.timestamps import to_iso
from apis.shared.tools.scoped_ids import SCOPE_DELIMITER, base_tool_ids

logger = logging.getLogger(__name__)

# tool_id -> (updated_at_iso_or_none, monotonic_fetched_at)
# None is stored when the tool is missing, so negative lookups are also
# TTL-cached — a deleted tool doesn't trigger a DynamoDB read every turn.
_cache: Dict[str, Tuple[Optional[str], float]] = {}

# Single-slot snapshot of (frozen_set_of_tool_ids, monotonic_fetched_at).
# Held in a list so we mutate index 0 in place rather than rebinding the
# module-level name — same pattern as `_cache` above (mutated, never
# reassigned), which keeps the module state easy to reason about.
_all_tool_ids_cache: List[Optional[Tuple[FrozenSet[str], float]]] = [None]

# Single-slot snapshot of (frozen_set_of_public_tool_ids, monotonic_fetched_at),
# same shape and lifecycle as `_all_tool_ids_cache` above.
_public_tool_ids_cache: List[Optional[Tuple[FrozenSet[str], float]]] = [None]

# Single-slot snapshot of (frozen_set_of_always_on_ids, monotonic_fetched_at).
# Unlike the two above this holds a MIX of bare catalog ids and scoped
# `base::name` ids, because always-on is settable on a whole tool and on one
# tool of an MCP server (spec §2.3). Scoped ids must be carried verbatim from
# here to `enabled_tools` — collapsing one to its base restores the whole
# server, which is the bug scoping exists to prevent.
_always_on_tool_ids_cache: List[Optional[Tuple[FrozenSet[str], float]]] = [None]

# Single-slot snapshot of (frozen_set_of_system_tool_ids, monotonic_fetched_at).
# The ids of platform-shipped ('system') tools. Unlike the always-on slot this
# holds only bare catalog ids (a system tool is pinned as a whole tool), and it
# is filled by the SAME single catalog pass as the other three. Kept separate
# from the always-on slot because system inclusion is NOT gated on the
# ADMIN_ALWAYS_ON_TOOLS_ENABLED flag — see resolve_system_tool_ids.
_system_tool_ids_cache: List[Optional[Tuple[FrozenSet[str], float]]] = [None]

_TTL_SECONDS = 10.0


def _reset_for_tests() -> None:
    _cache.clear()
    _all_tool_ids_cache[0] = None
    _public_tool_ids_cache[0] = None
    _always_on_tool_ids_cache[0] = None
    _system_tool_ids_cache[0] = None


async def _fetch_updated_at(tool_id: str) -> Optional[str]:
    from apis.shared.tools.repository import get_tool_catalog_repository

    repo = get_tool_catalog_repository()
    tool = await repo.get_tool(tool_id)
    if tool is None or tool.updated_at is None:
        return None
    return to_iso(tool.updated_at)


async def get_tool_updated_at(tool_id: str) -> Optional[str]:
    """Return the `updated_at` for one tool, TTL-cached per process."""
    now = time.monotonic()
    cached = _cache.get(tool_id)
    if cached is not None and now - cached[1] < _TTL_SECONDS:
        return cached[0]

    try:
        updated_at = await _fetch_updated_at(tool_id)
    except Exception:
        logger.exception("Failed to fetch updated_at for tool %s", tool_id)
        # On failure, return the last-known value if we have one, else
        # None. Never raise — freshness is advisory for cache keying and
        # must not break the chat turn.
        return cached[0] if cached is not None else None

    _cache[tool_id] = (updated_at, now)
    return updated_at


async def get_freshness_hash(tool_ids: List[str]) -> str:
    """Return a stable 16-char hash of (tool_id -> updated_at).

    Changes when any of the given tools' config is edited. Empty list
    returns the empty string so callers can short-circuit.

    Scoped ids (`base::tool`, selecting one tool of an MCP server) are
    collapsed to their base first: freshness is a property of the catalog
    record, and the catalog only holds the base. Hashing a scoped id
    verbatim looks it up, misses, and pins that entry to a constant
    `none` — so an admin edit to a server would never evict an agent that
    had bound a subset of it. Collapsing also de-duplicates, so an agent
    binding seven tools of one server costs one catalog read, not seven.
    """
    if not tool_ids:
        return ""

    sorted_ids = sorted(base_tool_ids(tool_ids))
    values = await asyncio.gather(
        *(get_tool_updated_at(tid) for tid in sorted_ids)
    )

    payload = "|".join(
        f"{tid}={val or 'none'}" for tid, val in zip(sorted_ids, values)
    )
    return hashlib.md5(payload.encode()).hexdigest()[:16]


async def get_all_tool_ids() -> FrozenSet[str]:
    """Return the set of all known tool IDs, TTL-cached per process.

    Listed once per TTL window via `repository.list_tools()` and reused
    across that window. Used by `ToolAccessService` to enumerate "every
    tool the system knows about" without scanning DynamoDB on every
    chat turn.

    On a repository error, returns the last-known set if available, else
    an empty frozenset — never raises (auth must not break on a transient
    DB blip).
    """
    return await _get_snapshot(_all_tool_ids_cache, "all")


async def get_public_tool_ids() -> FrozenSet[str]:
    """Return the set of tool IDs flagged `isPublic`, TTL-cached per process.

    A public tool is "available to all authenticated users regardless of
    role", so `AppRoleService` unions this set into a caller's role grant
    before deciding access. Sharing one snapshot keeps every gate reading
    the same answer within a TTL window.

    Deliberately unfiltered by `status`, mirroring the listing path
    (`ToolCatalogService._get_all_active_tools` passes no status filter):
    enforcement and the tool picker must agree about which tools a public
    flag reaches, and a divergence there is the bug this exists to close.

    Same never-raise contract as `get_all_tool_ids`.
    """
    return await _get_snapshot(_public_tool_ids_cache, "public")


async def get_always_on_tool_ids() -> FrozenSet[str]:
    """Return the admin-pinned tool ids, TTL-cached per process.

    A mix of bare catalog ids (the whole tool is pinned) and scoped
    `base::name` ids (one tool of an MCP server is pinned). **Enables,
    never grants** — the caller must still filter these against its own
    grant set; this function only answers "what did an admin pin?".

    Derived from the same `list_tools()` read that fills the all-ids and
    public-ids snapshots, so resolving the pinned set on a chat turn adds
    no DynamoDB round trip and no extra `from_dynamo_item` parse — one
    generator expression over a list already in memory. That is the whole
    latency argument for this feature (spec §4.1); resolving it with a
    `get_tool` per id instead is the shape that would spend the turn's
    budget.

    Same never-raise contract as `get_all_tool_ids`.
    """
    return await _get_snapshot(_always_on_tool_ids_cache, "always-on")


async def get_system_tool_ids() -> FrozenSet[str]:
    """Return the platform-shipped ('system') tool ids, TTL-cached per process.

    Bare catalog ids only — a system tool is pinned as a whole tool, never as
    one tool of an MCP server. **Enables, never grants**: the caller must still
    filter these against its own grant set (`resolve_system_tool_ids`).

    Derived from the same `list_tools()` read that fills the all-ids,
    public-ids and always-on slots, so it adds no DynamoDB round trip. Distinct
    from the always-on slot because system tools are included regardless of the
    admin always-on feature flag.

    Same never-raise contract as `get_all_tool_ids`.
    """
    return await _get_snapshot(_system_tool_ids_cache, "system")


def _always_on_ids_for(tool) -> List[str]:
    """The pinned ids one catalog row contributes.

    A tool flagged `always_on` contributes its bare id. Independently, any
    curated MCP entry flagged `always_on` contributes a scoped id — so a
    server can pin two of its thirty tools without pinning the server.

    Both can apply at once. That is harmless rather than contradictory:
    `collect_tool_name_filters` lets a bare id win over scoped ids for the
    same base, so the server stays whole and the scoped ids are redundant
    rather than narrowing.
    """
    ids: List[str] = []
    if getattr(tool, "always_on", False):
        ids.append(tool.tool_id)
    # `getattr` rather than attribute access throughout: this runs inside
    # `_get_snapshot`, which fills ALL THREE slots from one pass. An
    # AttributeError on one odd row would abort the whole refresh and leave
    # `get_public_tool_ids` empty too — and that set feeds RBAC's public-tool
    # union, so a malformed catalog row would quietly narrow every access
    # check. The blast radius is the reason for the defensiveness, not tidiness.
    cfg = getattr(tool, "mcp_config", None) or getattr(tool, "mcp_gateway_config", None)
    for entry in (getattr(cfg, "tools", None) or []):
        if getattr(entry, "always_on", False):
            ids.append(f"{tool.tool_id}{SCOPE_DELIMITER}{entry.name}")
    return ids


async def _get_snapshot(
    slot: List[Optional[Tuple[FrozenSet[str], float]]], which: str
) -> FrozenSet[str]:
    """Serve one id snapshot, refreshing all three from a single catalog read.

    `list_tools()` already carries `isPublic` and `alwaysOn`, so one read
    fills the all-ids, public-ids and always-on slots together — a third
    of the DynamoDB traffic of three independent caches, and the sets can
    never disagree about the catalog they were derived from.
    """
    now = time.monotonic()
    cached = slot[0]
    if cached is not None and now - cached[1] < _TTL_SECONDS:
        return cached[0]

    from apis.shared.tools.repository import get_tool_catalog_repository

    try:
        repo = get_tool_catalog_repository()
        tools = await repo.list_tools()
    except Exception:
        logger.exception("Failed to list tools for the %s-ids catalog snapshot", which)
        return cached[0] if cached is not None else frozenset()

    _all_tool_ids_cache[0] = (frozenset(t.tool_id for t in tools), now)
    _public_tool_ids_cache[0] = (
        frozenset(t.tool_id for t in tools if t.is_public),
        now,
    )
    always_on_ids: set = set()
    for t in tools:
        try:
            always_on_ids.update(_always_on_ids_for(t))
        except Exception:  # noqa: BLE001 - one odd row must not empty the other two slots
            logger.warning(
                "Skipping tool %s while collecting always-on ids",
                getattr(t, "tool_id", "<unknown>"),
                exc_info=True,
            )
    _always_on_tool_ids_cache[0] = (frozenset(always_on_ids), now)
    # System snapshot is status-filtered, UNLIKE the public/always-on slots.
    # A system tool is force-injected on every granted turn, so an admin's only
    # runtime kill-switch is the row's status: flipping it to `disabled` (or
    # `deprecated`) in the Tools panel must drop it from the injected set on the
    # next turn, with no redeploy. `_is_active_status` treats a ToolStatus enum
    # and its bare string value the same, and a missing status as active (every
    # row written before the status field, and the seeder's "active").
    _system_tool_ids_cache[0] = (
        frozenset(
            t.tool_id
            for t in tools
            if getattr(t, "system", False) and _is_active_status(t)
        ),
        now,
    )
    return slot[0][0]  # type: ignore[index]


def _is_active_status(tool) -> bool:
    """Whether a catalog row counts as active (enum or bare string, absent = active)."""
    status = getattr(tool, "status", None)
    if status is None:
        return True
    value = getattr(status, "value", status)
    return value == "active"


def invalidate(tool_id: Optional[str] = None) -> None:
    """Drop an entry (or the whole cache) from the TTL store.

    Always clears all three id snapshots too, since any create/delete
    shifts them — and toggling `isPublic` or `alwaysOn` on an existing tool
    shifts one of them without touching the id set (an admin write is the
    only reason to invalidate anyway).

    Call this from admin write paths so changes are visible in the same
    process on the very next turn, without waiting for the TTL to lapse.
    """
    if tool_id is None:
        _cache.clear()
    else:
        _cache.pop(tool_id, None)
    _all_tool_ids_cache[0] = None
    _public_tool_ids_cache[0] = None
    _always_on_tool_ids_cache[0] = None
    _system_tool_ids_cache[0] = None
