"""Skill version history: keep every content a skill holds as a version
(docs/specs/skill-version-history.md, PR-1).

A version is the write-once ``SKILL#{id}`` / ``VERSION#{n}`` row described in
``versions.py``. Until this module, one was cut only when something pinned the
skill. With ``SKILL_VERSION_HISTORY_ENABLED`` on, every write that changes what a
skill does is captured too, so the rule becomes: *every content the live row ever
holds is a version* (spec D1). The services call two functions around each write:

* :func:`checkpoint` before it (D2): make sure the content about to be replaced is
  already a version. When the previous write's capture worked this is one Query
  and a hash compare. ``required=True`` (a re-import, a restore) fails closed.
* :func:`capture` after it: keep the new content. Best-effort; a failure is
  logged and counted, and the next write's checkpoint repairs it while that
  content is still live.

Both deduplicate by ``content_hash`` against the latest version, so a save that
changes nothing writes nothing, and a pin after a save reuses the save's version.
:func:`capture_current` is the one place a version is cut; ``pinning.pin_current``
calls it with ``trigger="pin"`` whether or not the flag is on.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Literal, Optional

from apis.shared.feature_flags import skill_version_history_enabled
from apis.shared.observability.emf import emit_emf_metrics
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.timestamps import to_iso, utc_now_iso

from .models import SkillDefinition, SkillResourceRef
from .repository import SkillCatalogRepository, SkillVersionExistsError, get_skill_catalog_repository
from .resource_store import SkillResourceStore, get_skill_resource_store
from .versions import SkillVersion, content_hash, snapshot_of

logger = logging.getLogger(__name__)

MAX_ALLOCATION_ATTEMPTS = 5

_METRICS_NAMESPACE = "AgentCoreStack/Skills"

# What cut a version. Display and diagnosis only; never part of ``content_hash``.
VersionTrigger = Literal[
    "create",  # a new skill
    "save",  # a form save or an update of the skill's fields
    "file",  # one file uploaded, written or deleted
    "import",  # a skill created from a bundle
    "reimport",  # a bundle re-imported into an existing skill
    "restore",  # an earlier version put back (PR-2)
    "pin",  # a project or Agent pinned the skill
    "authoring",  # a ``my_skill_*`` tool wrote it
    "checkpoint",  # content that was live before a write and not yet a version
]


class SkillVersionSourceMissingError(ValueError):
    """The skill to capture does not exist."""


class SkillCheckpointError(RuntimeError):
    """A required checkpoint failed, so the write that needed it must not run."""


async def capture_current(
    skill_id: str,
    *,
    actor: Optional[str],
    trigger: VersionTrigger,
    live: Optional[SkillDefinition] = None,
    restored_from: Optional[int] = None,
    repository: Optional[SkillCatalogRepository] = None,
    resource_store: Optional[SkillResourceStore] = None,
) -> SkillVersion:
    """The version holding ``skill_id``'s content as of now, cut if there is none.

    Reuses the latest version when the live skill's ``content_hash`` matches it, so
    capturing an unchanged skill twice is one version. Otherwise freezes the files
    and allocates the next number; a concurrent capture that takes the number first
    is re-read, and reused when it froze the same content.

    ``live`` skips the re-read when the caller has just written the row.

    Raises ``SkillVersionSourceMissingError`` when the skill does not exist, and
    ``SkillResourceStoreError`` (``SkillResourceChangedError`` for a mid-capture
    re-upload) when its files can't be frozen.
    """
    repo = repository or get_skill_catalog_repository()
    if live is None:
        live = await repo.get_skill(skill_id)
    if live is None:
        raise SkillVersionSourceMissingError(f"Skill '{skill_id}' was not found.")

    digest = content_hash(live)
    latest = await repo.get_latest_skill_version(skill_id)
    if latest is not None and latest.content_hash == digest:
        return latest

    store = resource_store or get_skill_resource_store()
    frozen = await asyncio.to_thread(freeze_resources, live, latest, store)
    snapshot = snapshot_of(
        live,
        frozen_resources=frozen,
        created_at=utc_now_iso(),
        created_by=actor,
        source_updated_at=to_iso(live.updated_at) if live.updated_at else None,
    ).model_copy(
        update={
            "trigger": trigger,
            "restored_from": restored_from,
            "import_source_record": live.import_source,
        }
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


def freeze_resources(
    live: SkillDefinition,
    latest: Optional[SkillVersion],
    store: SkillResourceStore,
) -> List[SkillResourceRef]:
    """``live``'s files pointed at their frozen, content-addressed copies.

    A file whose content the latest version already froze reuses that copy with no
    S3 call (spec §4.2), so a save that changed only the instructions, or one file
    of fifty, touches S3 only for what changed. Everything else goes through
    ``SkillResourceStore.freeze``, which checks the bytes against ``content_hash``.
    """
    if not live.resources:
        return []
    already: Dict[str, str] = {
        r.content_hash: r.s3_key for r in (latest.resources if latest else [])
    }
    return [
        ref.model_copy(
            update={
                "s3_key": already.get(ref.content_hash)
                or store.freeze(
                    skill_id=live.skill_id,
                    s3_key=ref.s3_key,
                    content_hash=ref.content_hash,
                    content_type=ref.content_type,
                )
            }
        )
        for ref in live.resources
    ]


async def checkpoint(
    skill_id: str,
    *,
    actor: Optional[str],
    required: bool = False,
    live: Optional[SkillDefinition] = None,
    repository: Optional[SkillCatalogRepository] = None,
    resource_store: Optional[SkillResourceStore] = None,
) -> None:
    """Before a content write: make sure the content it replaces is a version (D2).

    A no-op while the flag is off. With ``required`` (a re-import, a restore) a
    failure raises ``SkillCheckpointError`` and the caller must not write; without
    it the failure is logged and counted and the write goes ahead.
    """
    if not skill_version_history_enabled():
        return
    try:
        await capture_current(
            skill_id,
            actor=actor,
            trigger="checkpoint",
            live=live,
            repository=repository,
            resource_store=resource_store,
        )
    except SkillVersionSourceMissingError:
        return  # nothing to keep; the write reports the missing skill itself
    except Exception as exc:  # noqa: BLE001 - reported below, raised only when required
        _report_failure(skill_id, "checkpoint", exc)
        if required:
            raise SkillCheckpointError(
                "We couldn't save this skill's current version before changing it, "
                "so nothing was changed. Try again."
            ) from exc


async def capture(
    skill_id: str,
    *,
    actor: Optional[str],
    trigger: VersionTrigger,
    live: Optional[SkillDefinition] = None,
    restored_from: Optional[int] = None,
    repository: Optional[SkillCatalogRepository] = None,
    resource_store: Optional[SkillResourceStore] = None,
) -> Optional[SkillVersion]:
    """After a content write: keep the skill's new content as a version.

    ``live`` is the row as written, when the caller has it; without it the row is
    re-read (a file write returns only the manifest). A no-op returning None while
    the flag is off, with no read. Never raises: the skill write has already
    happened and must not be reported as failed because its history was.
    """
    if not skill_version_history_enabled():
        return None
    try:
        version = await capture_current(
            skill_id,
            actor=actor,
            trigger=trigger,
            live=live,
            restored_from=restored_from,
            repository=repository,
            resource_store=resource_store,
        )
    except SkillVersionSourceMissingError:
        return None  # deleted in between; there is nothing left to keep
    except Exception as exc:  # noqa: BLE001 - history never fails a write
        _report_failure(skill_id, trigger, exc)
        return None
    logger.info(
        "Captured skill version",
        extra={
            "event": "skill_version_captured",
            "skill_id": scrub_log(skill_id),
            "trigger": trigger,
            "version": version.version,
        },
    )
    return version


def _report_failure(skill_id: str, trigger: str, exc: Exception) -> None:
    logger.error(
        "Skill version capture failed",
        exc_info=exc,
        extra={
            "event": "skill_version_capture_failed",
            "skill_id": scrub_log(skill_id),
            "trigger": trigger,
            "error_type": type(exc).__name__,
        },
    )
    emit_emf_metrics(
        _METRICS_NAMESPACE,
        metrics={"SkillVersionCaptureFailed": 1},
        properties={"trigger": trigger, "errorType": type(exc).__name__},
        units={"SkillVersionCaptureFailed": "Count"},
    )
