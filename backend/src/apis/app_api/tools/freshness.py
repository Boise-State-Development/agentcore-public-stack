"""Per-process TTL cache of tool-config freshness tokens.

Cheap change-detection signal for the agent and MCP-client caches: any
admin edit to a tool bumps its `updated_at`, so including the freshness
hash in a cache key causes the next build to miss and rebuild with the
fresh config.

Reads are TTL-cached so the per-turn overhead is bounded to at most one
DynamoDB GetItem per tool per TTL window, per process. Admin routes
call `invalidate(tool_id)` after a write so same-process visibility is
immediate; other processes see the change within one TTL window.
"""

import asyncio
import hashlib
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# tool_id -> (updated_at_iso_or_none, monotonic_fetched_at)
# None is stored when the tool is missing, so negative lookups are also
# TTL-cached — a deleted tool doesn't trigger a DynamoDB read every turn.
_cache: Dict[str, Tuple[Optional[str], float]] = {}
_TTL_SECONDS = 10.0


def _reset_for_tests() -> None:
    _cache.clear()


async def _fetch_updated_at(tool_id: str) -> Optional[str]:
    from apis.app_api.tools.repository import get_tool_catalog_repository

    repo = get_tool_catalog_repository()
    tool = await repo.get_tool(tool_id)
    if tool is None or tool.updated_at is None:
        return None
    return tool.updated_at.isoformat() + "Z"


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
    """
    if not tool_ids:
        return ""

    sorted_ids = sorted(tool_ids)
    values = await asyncio.gather(
        *(get_tool_updated_at(tid) for tid in sorted_ids)
    )

    payload = "|".join(
        f"{tid}={val or 'none'}" for tid, val in zip(sorted_ids, values)
    )
    return hashlib.md5(payload.encode()).hexdigest()[:16]


def invalidate(tool_id: Optional[str] = None) -> None:
    """Drop an entry (or the whole cache) from the TTL store.

    Call this from admin write paths so changes are visible in the same
    process on the very next turn, without waiting for the TTL to lapse.
    """
    if tool_id is None:
        _cache.clear()
    else:
        _cache.pop(tool_id, None)
