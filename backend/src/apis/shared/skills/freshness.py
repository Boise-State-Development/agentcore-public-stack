"""Per-process TTL caches over the skill catalog.

The skills parallel of ``apis/shared/tools/freshness.py``. Two caches live
here, both backed by DynamoDB reads of the skill catalog:

1. **Per-skill freshness tokens** (``get_skill_updated_at``,
   ``get_freshness_hash``). Cheap change-detection signal for the agent
   cache: any admin edit to a skill bumps its ``updated_at``, so including
   the freshness hash in a cache key causes the next build to miss and
   rebuild with the fresh config (the runtime ``skills_hash`` in spec §8.3).

2. **All-known-skill-ids snapshot** (``get_all_skill_ids``). The set of
   skill IDs known to the catalog — the source of truth that RBAC's
   wildcard (``"*"``) skill access needs to enumerate.

Reads are TTL-cached so the per-turn overhead is bounded to at most one
DynamoDB read per cache key per TTL window, per process. Admin routes call
``invalidate(skill_id)`` after a write so same-process visibility is
immediate; other processes see the change within one TTL window.
``invalidate`` clears the all-skill-ids snapshot too, since any
create/delete shifts that set.
"""

import asyncio
import hashlib
import logging
import time
from typing import Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)

# skill_id -> (updated_at_iso_or_none, monotonic_fetched_at)
# None is stored when the skill is missing, so negative lookups are also
# TTL-cached — a deleted skill doesn't trigger a DynamoDB read every turn.
_cache: Dict[str, Tuple[Optional[str], float]] = {}

# Single-slot snapshot of (frozen_set_of_skill_ids, monotonic_fetched_at).
# Held in a list so we mutate index 0 in place rather than rebinding the
# module-level name — same pattern as `_cache` above.
_all_skill_ids_cache: List[Optional[Tuple[FrozenSet[str], float]]] = [None]

_TTL_SECONDS = 10.0


def _reset_for_tests() -> None:
    _cache.clear()
    _all_skill_ids_cache[0] = None


async def _fetch_updated_at(skill_id: str) -> Optional[str]:
    from apis.shared.skills.repository import get_skill_catalog_repository

    repo = get_skill_catalog_repository()
    skill = await repo.get_skill(skill_id)
    if skill is None or skill.updated_at is None:
        return None
    return skill.updated_at.isoformat() + "Z"


async def get_skill_updated_at(skill_id: str) -> Optional[str]:
    """Return the `updated_at` for one skill, TTL-cached per process."""
    now = time.monotonic()
    cached = _cache.get(skill_id)
    if cached is not None and now - cached[1] < _TTL_SECONDS:
        return cached[0]

    try:
        updated_at = await _fetch_updated_at(skill_id)
    except Exception:
        logger.exception("Failed to fetch updated_at for skill %s", skill_id)
        # On failure, return the last-known value if we have one, else
        # None. Never raise — freshness is advisory for cache keying and
        # must not break the chat turn.
        return cached[0] if cached is not None else None

    _cache[skill_id] = (updated_at, now)
    return updated_at


async def get_freshness_hash(skill_ids: List[str]) -> str:
    """Return a stable 16-char hash of (skill_id -> updated_at).

    Changes when any of the given skills' config is edited. Empty list
    returns the empty string so callers can short-circuit.
    """
    if not skill_ids:
        return ""

    sorted_ids = sorted(skill_ids)
    values = await asyncio.gather(
        *(get_skill_updated_at(sid) for sid in sorted_ids)
    )

    payload = "|".join(
        f"{sid}={val or 'none'}" for sid, val in zip(sorted_ids, values)
    )
    return hashlib.md5(payload.encode()).hexdigest()[:16]


async def get_all_skill_ids() -> FrozenSet[str]:
    """Return the set of all known skill IDs, TTL-cached per process.

    Listed once per TTL window via `repository.list_skills()` and reused
    across that window. Used by RBAC skill access to enumerate "every skill
    the system knows about" (e.g. expanding a `"*"` wildcard grant) without
    scanning DynamoDB on every chat turn.

    On a repository error, returns the last-known set if available, else an
    empty frozenset — never raises (auth must not break on a transient DB
    blip).
    """
    now = time.monotonic()
    cached = _all_skill_ids_cache[0]
    if cached is not None and now - cached[1] < _TTL_SECONDS:
        return cached[0]

    from apis.shared.skills.repository import get_skill_catalog_repository

    try:
        repo = get_skill_catalog_repository()
        skills = await repo.list_skills()
        ids = frozenset(s.skill_id for s in skills)
    except Exception:
        logger.exception("Failed to list skill IDs for catalog snapshot")
        return cached[0] if cached is not None else frozenset()

    _all_skill_ids_cache[0] = (ids, now)
    return ids


def invalidate(skill_id: Optional[str] = None) -> None:
    """Drop an entry (or the whole cache) from the TTL store.

    Always clears the all-skill-ids snapshot too, since any create/delete
    shifts that set (and an admin write is the only reason to invalidate
    anyway).

    Call this from admin write paths so changes are visible in the same
    process on the very next turn, without waiting for the TTL to lapse.
    """
    if skill_id is None:
        _cache.clear()
    else:
        _cache.pop(skill_id, None)
    _all_skill_ids_cache[0] = None
