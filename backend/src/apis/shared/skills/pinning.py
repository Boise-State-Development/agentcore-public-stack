"""Pinning a skill binding to a version, and reading pinned skills back (shared-projects 3.1).

Two callers: the project settings routes pin (``pin_current``) and report
(``pin_status``), and the runtime loads the records a turn runs
(``load_skill_records``). A hard-deleted skill's versions go with it
(``SkillCatalogService.delete_versions``).
See ``versions.py`` for why a version is cut on pin rather than on every save.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from apis.shared.timestamps import to_iso, utc_now_iso

from .models import SkillDefinition, SkillResourceRef
from .pins import split_pinned_ref
from .repository import SkillVersionExistsError, get_skill_catalog_repository
from .resource_store import get_skill_resource_store
from .versions import SkillVersion, apply_version, content_hash, snapshot_of

logger = logging.getLogger(__name__)

MAX_ALLOCATION_ATTEMPTS = 5


class SkillPinError(ValueError):
    """The skill can't be pinned (it does not exist)."""


async def pin_current(skill_id: str, *, pinned_by: Optional[str]) -> SkillVersion:
    """The version a new pin on ``skill_id`` should hold: its content as of now.

    Reuses the latest version when the live skill has not changed since it was cut,
    so pinning is idempotent. Otherwise freezes the reference files and cuts the next
    number; a concurrent pin that takes the number first is re-read, and reused when
    it froze the same content.

    Raises ``SkillPinError`` when the skill does not exist, and
    ``SkillResourceStoreError`` (``SkillResourceChangedError`` for a mid-pin
    re-upload) when its files can't be frozen.
    """
    repo = get_skill_catalog_repository()
    live = await repo.get_skill(skill_id)
    if live is None:
        raise SkillPinError(f"Skill '{skill_id}' was not found.")

    digest = content_hash(live)
    latest = await repo.get_latest_skill_version(skill_id)
    if latest is not None and latest.content_hash == digest:
        return latest

    frozen = await asyncio.to_thread(_freeze_resources, live)
    snapshot = snapshot_of(
        live,
        frozen_resources=frozen,
        created_at=utc_now_iso(),
        created_by=pinned_by,
        source_updated_at=to_iso(live.updated_at) if live.updated_at else None,
    )
    for attempt in range(MAX_ALLOCATION_ATTEMPTS):
        number = (latest.version or 0) + 1 if latest else 1
        try:
            return await repo.put_skill_version(snapshot.model_copy(update={"version": number}))
        except SkillVersionExistsError:
            logger.warning(
                "Version %s of skill %s was taken concurrently (attempt %s/%s)",
                number, skill_id, attempt + 1, MAX_ALLOCATION_ATTEMPTS,
            )
            latest = await repo.get_latest_skill_version(skill_id)
            if latest is not None and latest.content_hash == digest:
                return latest
    raise RuntimeError(f"Could not allocate a version of skill {skill_id}.")


def _freeze_resources(live: SkillDefinition) -> List[SkillResourceRef]:
    if not live.resources:
        return []
    store = get_skill_resource_store()
    return [
        ref.model_copy(
            update={
                "s3_key": store.freeze(
                    skill_id=live.skill_id,
                    s3_key=ref.s3_key,
                    content_hash=ref.content_hash,
                    content_type=ref.content_type,
                )
            }
        )
        for ref in live.resources
    ]


@dataclass
class PinStatus:
    """What a pinned binding runs, for the settings page."""

    version: Optional[int]
    pinned_at: Optional[str]
    # The live skill's content differs from the pinned version (always False unpinned).
    update_available: bool


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

