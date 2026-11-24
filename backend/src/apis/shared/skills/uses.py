"""Where a user's skill is used, split by whether each place runs its current content
(docs/specs/skill-version-history.md D9).

A pin keeps running its version on purpose, so after a change the author has to be
told where it has not landed. This splits every binding ``reach`` finds into:

1. **current** — unpinned (it runs the live skill), or pinned to a version whose
   content is the live content;
2. **behind** — pinned to other content, in a place the author can update;
3. **others behind** — pinned to other content where they can't (a project they
   are now only a viewer of): counted, never named.

Shared by app-api's ``/skills/mine/{id}/uses`` and the Runtime's ``my_skill_*``
tools, which is why it lives in ``apis.shared``. The reach lookup is blocking boto3
and runs through ``run_blocking``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from apis.shared.blocking_io import run_blocking

from .models import SkillDefinition
from .reach import SkillBinding, lookup_skill_reach
from .repository import SkillCatalogRepository, get_skill_catalog_repository
from .versions import content_hash


@dataclass
class SkillUses:
    current: List[SkillBinding] = field(default_factory=list)
    behind: List[SkillBinding] = field(default_factory=list)
    others_behind: int = 0
    # False when the lookup failed or stopped early: say so, never "used nowhere".
    complete: bool = True
    error: Optional[str] = None

    @property
    def behind_count(self) -> int:
        return len(self.behind) + self.others_behind


async def classify_uses(
    live: SkillDefinition,
    owner_id: str,
    *,
    repository: Optional[SkillCatalogRepository] = None,
) -> SkillUses:
    """Every binding of ``live`` in its owner's reach, split by D9's three groups."""
    reach = await run_blocking(lookup_skill_reach, live.skill_id, owner_id)
    uses = SkillUses(
        complete=reach.complete and reach.editor_projects_checked and reach.error is None,
        error=reach.error,
    )
    pinned = sorted({b.pinned_version for b in reach.bindings if b.pinned_version})
    repo = repository or get_skill_catalog_repository()
    versions = (
        await repo.batch_get_skill_versions([(live.skill_id, n) for n in pinned]) if pinned else {}
    )
    digest = content_hash(live)
    for binding in reach.bindings:
        if binding.pinned_version is None:
            uses.current.append(binding)
            continue
        version = versions.get((live.skill_id, binding.pinned_version))
        if version is not None and version.content_hash == digest:
            uses.current.append(binding)
        elif binding.can_update:
            uses.behind.append(binding)
        else:
            uses.others_behind += 1
    return uses


def behind_note(uses: SkillUses) -> Optional[str]:
    """One bounded line for a ``my_skill_*`` tool result: a count, no names.

    None when nothing pinned is behind, so a result only grows when there is
    something the user should hear about.
    """
    count = uses.behind_count
    if count == 0:
        return None
    places = "place" if count == 1 else "places"
    verb = "pins" if count == 1 else "pin"
    return (
        f"{count} agent or project {places} {verb} an earlier version of this skill and "
        "won't get this change until updated. The skill's page in Customize offers the update."
    )
