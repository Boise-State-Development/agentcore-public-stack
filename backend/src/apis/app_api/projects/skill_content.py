"""Reading a project's bound skills: who may, and which content they see.

The project Skills page lists what the project's assistant binds, and a member opens
one to read what it does. The ordinary skill reads (``GET /skills/{id}``,
``GET /skills/mine/{id}``) answer "can *I* reach this skill on my own", which is the
wrong question here: a private skill the editor who added it wrote runs for every
member of the project (clause 4 of ``resolve_invocable_skill_ids``), and is in no
member's own list.

So a project read asks the run-time question instead, with the same predicate and the
same inputs the turn path uses (``agent_binding_resolver._resolve_skills``): the
skill is bound to this project, and it resolves for the caller through this harness.
A member can read exactly the skills their own turns in the project would load, no
more. A catalog skill the caller's roles don't grant is dropped from their turns, so
it is refused here too, rather than widened for display.

The content is the **bound version's** (``config.version``, shared-projects 3.1),
not the live row's: what the project runs is what the page shows. A binding from
before pins reads live, as it runs live.

Read-only by construction, and nothing here reaches a prompt or ``toolConfig``.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Set, Tuple

from apis.shared.assistants.models import AgentBinding, Assistant
from apis.shared.auth.models import User
from apis.shared.feature_flags import skills_enabled
from apis.shared.skills.access import resolve_invocable_skill_ids
from apis.shared.skills.models import SkillDefinition, SkillResourceRef, SkillStatus
from apis.shared.skills.pinning import load_skill_records
from apis.shared.skills.pins import base_skill_id, binding_pin, pinned_ref, project_skill_adders
from apis.shared.skills.repository import get_skill_catalog_repository
from apis.shared.skills.resource_store import get_skill_resource_store

SKILL = "skill"


class SkillContentError(Exception):
    """Base for a refused project skill read."""


class SkillNotBoundError(SkillContentError):
    """The project binds no such skill, or it (or the named file) no longer exists."""


class SkillNotAvailableError(SkillContentError):
    """The skill is bound, but would not run for this caller (their roles don't grant it)."""


def _binding(harness: Assistant, skill_id: str) -> Optional[AgentBinding]:
    return next(
        (b for b in (harness.bindings or []) if b.kind == SKILL and base_skill_id(b.ref) == skill_id),
        None,
    )


async def available_skill_ids(harness: Assistant, user: User, skill_ids: List[str]) -> Set[str]:
    """Which of the harness's bound ``skill_ids`` would run for ``user`` in this project.

    The run-time predicate with the project's adders, exactly as ``_resolve_skills`` calls
    it, so "you can read it" and "it runs for you" can never disagree.
    """
    if not skill_ids or not skills_enabled():
        return set()
    invocable = await resolve_invocable_skill_ids(
        user, skill_ids, harness.owner_id, adders=project_skill_adders(harness)
    )
    return {sid for sid in skill_ids if sid in invocable}


async def bound_skill_labels(harness: Assistant, user: User) -> Dict[str, Tuple[str, str]]:
    """``{skill id: (display name, description)}`` for the bound skills ``user`` can run.

    Lets the settings list name a skill someone else added (the caller's own palette
    doesn't have it) instead of showing its raw id. Skills the caller can't run stay
    unnamed: they already see the id, and the name would say no more than that.
    """
    refs = list(dict.fromkeys(base_skill_id(b.ref) for b in (harness.bindings or []) if b.kind == SKILL))
    available = await available_skill_ids(harness, user, refs)
    if not available:
        return {}
    records = await get_skill_catalog_repository().batch_get_skills([r for r in refs if r in available])
    return {
        r.skill_id: (r.display_name, r.description)
        for r in records
        if _status(r) == SkillStatus.ACTIVE.value
    }


async def read_bound_skill(harness: Assistant, user: User, skill_id: str) -> Tuple[SkillDefinition, Optional[int]]:
    """The content ``user`` sees for a bound skill: its pinned version, or live when unpinned.

    Raises ``SkillNotBoundError`` when the project doesn't bind it or it is gone (deleted,
    disabled, or its pinned version missing — the turn drops it in each case), and
    ``SkillNotAvailableError`` when it is bound but wouldn't run for ``user``.
    """
    binding = _binding(harness, skill_id)
    if binding is None:
        raise SkillNotBoundError(f"This project has no skill '{skill_id}'.")
    if skill_id not in await available_skill_ids(harness, user, [skill_id]):
        raise SkillNotAvailableError("This skill isn't available to you, so its content can't be shown.")

    version = binding_pin(binding.config)
    records = await load_skill_records([pinned_ref(skill_id, version)])
    record = records[0] if records else None
    if record is None or _status(record) != SkillStatus.ACTIVE.value:
        raise SkillNotBoundError(f"The skill '{skill_id}' is no longer available.")
    return record, version


async def read_bound_skill_file(
    harness: Assistant, user: User, skill_id: str, filename: str
) -> Tuple[SkillResourceRef, bytes]:
    """One bundle file of the bound version, by the same rules as ``read_bound_skill``."""
    record, _ = await read_bound_skill(harness, user, skill_id)
    ref = next((r for r in record.resources if r.filename == filename), None)
    if ref is None:
        raise SkillNotBoundError(f"The skill has no file '{filename}'.")
    content = await asyncio.to_thread(get_skill_resource_store().get, ref.s3_key)
    return ref, content


def _status(skill: SkillDefinition) -> str:
    return skill.status.value if hasattr(skill.status, "value") else str(skill.status)
