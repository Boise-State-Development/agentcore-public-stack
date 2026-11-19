"""Audit records for skill mutations (agentskills-conformance §6.5, S5).

One place decides what a skill audit record carries, so every write path (the
admin catalog, My Skills, bundle import, export, the Runtime's ``my_skill_*``
tools and skill script runs) writes the same shape:

* the tier (``catalog`` or ``user``) and the owner id;
* the version content hash (``versions.content_hash``), the digest a pin compares;
* the bundle's file names with their content hashes;
* for an imported skill, the source type, owner, repo, ref and commit SHA.

**Ids and hashes only, never instruction or description text.** A skill's text
is someone's work product and can be long; the audit table is read by every admin
with the audit scope and kept for a year. The hash answers "did it change, and
into what?" because a pinned version with the same hash holds the text itself.

``AuditService.record`` makes a blocking DynamoDB call and never raises. Every
caller here is ``async``, so the write runs in a worker thread and a failure is
logged by the service and swallowed: an audit gap never fails the mutation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from apis.shared.audit.models import TARGET_SKILL
from apis.shared.audit.service import get_audit_service
from apis.shared.auth.models import User

from .models import SkillDefinition
from .versions import content_hash

logger = logging.getLogger(__name__)

TIER_CATALOG = "catalog"
TIER_USER = "user"
_SYSTEM_OWNER = "system"


def skill_tier(skill: SkillDefinition) -> str:
    return TIER_CATALOG if (skill.owner_id or _SYSTEM_OWNER) == _SYSTEM_OWNER else TIER_USER


def import_source_fields(skill: SkillDefinition) -> Dict[str, Any]:
    """The provenance of an imported skill, without any URL query or text."""
    return skill.import_source.provenance() if skill.import_source is not None else {}


def skill_state(skill: SkillDefinition) -> Dict[str, Any]:
    """What an audit record says about one state of a skill. Ids and hashes only."""
    state: Dict[str, Any] = {
        "tier": skill_tier(skill),
        "ownerId": skill.owner_id,
        "contentHash": content_hash(skill),
        "files": {r.filename: r.content_hash for r in skill.resources},
        "status": str(getattr(skill.status, "value", skill.status)),
    }
    source = import_source_fields(skill)
    if source:
        state["importSource"] = source
    return state


def changed_keys(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def is_no_op(before: SkillDefinition, after: SkillDefinition) -> bool:
    """True when a save changed nothing an audit record would show.

    Compares the audited state plus the fields outside the content hash that a
    user can still change (display category); a save that only re-sent the same
    values writes no record.
    """
    return skill_state(before) == skill_state(after) and before.category == after.category


async def record_skill_event(
    action: str,
    actor: User,
    skill_id: str,
    *,
    before: Optional[SkillDefinition] = None,
    after: Optional[SkillDefinition] = None,
    extra_after: Optional[Dict[str, Any]] = None,
) -> None:
    """Write one skill audit record off the event loop. Never raises."""
    try:
        before_state = skill_state(before) if before is not None else {}
        after_state = skill_state(after) if after is not None else {}
        if extra_after:
            after_state.update(extra_after)
        changes = changed_keys(before_state, after_state) if before is not None and after is not None else sorted(after_state or before_state)
        await asyncio.to_thread(
            get_audit_service().record,
            action=action,
            actor=actor,
            target_id=skill_id,
            target_type=TARGET_SKILL,
            changes=changes,
            before=before_state,
            after=after_state,
        )
    except Exception:  # noqa: BLE001 - an audit gap never fails the mutation
        logger.exception(
            "Skill audit record could not be written",
            extra={"event": "skill_audit_failed", "action": action},
        )
