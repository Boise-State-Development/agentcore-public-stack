"""Pinning a skill binding to a version, and reading pinned skills back (shared-projects 3.1).

Two callers: the project settings routes pin (``pin_current``) and report
(``pin_status``), and the runtime loads the records a turn runs
(``load_skill_records``). A hard-deleted skill's versions go with it
(``SkillCatalogService.delete_versions``).
A pin cuts its version through ``history.capture_current``; see ``versions.py`` for
when else one is cut.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import SkillDefinition
from .pins import PIN_CONFIG_KEY, binding_pin, split_pinned_ref
from .history import SkillVersionSourceMissingError, capture_current
from .repository import get_skill_catalog_repository
from .resource_store import get_skill_resource_store
from .versions import SkillVersion, apply_version, content_hash

logger = logging.getLogger(__name__)



class SkillPinError(ValueError):
    """The skill can't be pinned (it does not exist)."""


async def pin_current(skill_id: str, *, pinned_by: Optional[str]) -> SkillVersion:
    """The version a new pin on ``skill_id`` should hold: its content as of now.

    ``history.capture_current`` with ``trigger="pin"``: reuses the latest version
    when the live skill has not changed since it was cut, so pinning is idempotent,
    and with version history on that is usually the version the last save cut.

    Raises ``SkillPinError`` when the skill does not exist, and
    ``SkillResourceStoreError`` (``SkillResourceChangedError`` for a mid-pin
    re-upload) when its files can't be frozen.
    """
    try:
        return await capture_current(
            skill_id,
            actor=pinned_by,
            trigger="pin",
            repository=get_skill_catalog_repository(),
            resource_store=get_skill_resource_store(),
        )
    except SkillVersionSourceMissingError as e:
        raise SkillPinError(str(e)) from e


@dataclass
class PinStatus:
    """What a pinned binding runs, for the settings page."""

    version: Optional[int]
    pinned_at: Optional[str]
    # The live skill's content differs from the pinned version (always False unpinned).
    update_available: bool
    # Where the pinned version came from (``SkillVersion.import_source``); None unpinned,
    # for a skill never imported, or for a version cut before this was recorded.
    import_source: Optional[Dict[str, str]] = None


async def pin_status(bindings: Iterable[Tuple[str, Optional[int]]]) -> Dict[str, PinStatus]:
    """Pin status per skill id for ``(skill_id, pinned version)`` pairs.

    A skill that no longer exists, or a pin whose version is gone, reports no update:
    there is nothing to update to, and the binding is reported unavailable on the turn.
    """
    pairs = list(bindings)
    pinned = [(sid, n) for sid, n in pairs if n]
    if not pinned:
        return {sid: PinStatus(None, None, False) for sid, _ in pairs}

    repo = get_skill_catalog_repository()
    live_rows, versions = await asyncio.gather(
        repo.batch_get_skills(list(dict.fromkeys(sid for sid, _ in pinned))),
        repo.batch_get_skill_versions(pinned),
    )
    live = {s.skill_id: s for s in live_rows}
    out: Dict[str, PinStatus] = {}
    for sid, n in pairs:
        version = versions.get((sid, n)) if n else None
        if version is None:
            out[sid] = PinStatus(n, None, False)
            continue
        current = live.get(sid)
        out[sid] = PinStatus(
            n,
            version.created_at,
            current is not None and content_hash(current) != version.content_hash,
            version.import_source,
        )
    return out


async def load_skill_records(refs: List[str]) -> List[SkillDefinition]:
    """The records a turn runs for run-time skill ids (bare or ``id@n``).

    The live row is always read, because it decides whether the skill runs at all
    (status, existence); a pinned ref then takes its content from the version. A pin
    whose version is missing is left out rather than run live, since running content
    nobody pinned is the one outcome a pin exists to prevent. Order follows ``refs``.
    """
    split = [split_pinned_ref(ref) for ref in refs]
    repo = get_skill_catalog_repository()
    pinned = [(sid, n) for sid, n in split if n]
    # batch_get_item refuses duplicate keys.
    skill_ids = list(dict.fromkeys(sid for sid, _ in split))
    if pinned:
        live_rows, versions = await asyncio.gather(
            repo.batch_get_skills(skill_ids), repo.batch_get_skill_versions(pinned)
        )
    else:
        live_rows, versions = await repo.batch_get_skills(skill_ids), {}
    live = {s.skill_id: s for s in live_rows}

    records: List[SkillDefinition] = []
    for sid, n in split:
        record = live.get(sid)
        if record is None:
            continue
        if n:
            version = versions.get((sid, n))
            if version is None:
                logger.warning("Skill %s is pinned to version %s, which does not exist", sid, n)
                continue
            record = apply_version(record, version)
        records.append(record)
    return records



async def pin_imported_skill_bindings(
    bindings: Optional[List[Any]],
    existing_bindings: Optional[Iterable[Any]] = None,
    *,
    pinned_by: Optional[str],
) -> Optional[List[Any]]:
    """Pin each newly bound skill that was imported from a bundle (agentskills D-7).

    A skill with an ``import_source`` was written by a third party; the binding
    runs what its owner reviewed when they bound it, until someone moves the pin.
    So a skill binding whose ref was not bound before, names no version, and
    whose row has an ``import_source`` gets ``{"version": n}`` from
    :func:`pin_current`. A hand-written skill, a skill already bound, and a
    binding that already names a version are returned unchanged.

    ``bindings`` are ``AgentBinding`` objects (``kind``, ``ref``, ``config``);
    ``None`` (a save that does not touch bindings) is returned as is. Call it
    after ``validate_agent_write``, so nothing is pinned for a skill the saver
    may not bind. Raises what :func:`pin_current` raises.
    """
    if not bindings:
        return bindings
    bound_before = {
        split_pinned_ref(b.ref)[0]
        for b in (existing_bindings or [])
        if getattr(b, "kind", None) == "skill"
    }
    candidates = [
        b
        for b in bindings
        if b.kind == "skill"
        and split_pinned_ref(b.ref)[1] is None
        and binding_pin(b.config) is None
        and b.ref not in bound_before
    ]
    if not candidates:
        return bindings
    rows = await get_skill_catalog_repository().batch_get_skills(
        list(dict.fromkeys(b.ref for b in candidates))
    )
    imported = {s.skill_id for s in rows if s.import_source is not None}
    if not imported:
        return bindings

    versions: Dict[str, int] = {}
    for ref in imported:
        versions[ref] = (await pin_current(ref, pinned_by=pinned_by)).version
    candidate_ids = {id(b) for b in candidates}
    out: List[Any] = []
    for binding in bindings:
        if id(binding) in candidate_ids and binding.ref in versions:
            binding = binding.model_copy(
                update={"config": {**dict(binding.config or {}), PIN_CONFIG_KEY: versions[binding.ref]}}
            )
        out.append(binding)
    return out
