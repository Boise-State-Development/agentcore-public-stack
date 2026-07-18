"""DB ``SkillDefinition`` → Strands ``Skill`` mapping (Skills v2 PR-2).

The runtime disclosure engine is the vended Strands ``AgentSkills`` plugin. It
takes programmatic ``Skill`` instances (name/description/instructions +
advisory ``allowed_tools`` + ``metadata``), injects a ~100-token
``<available_skills>`` block per skill into the system prompt, and exposes one
``skills`` activation tool that returns a skill's full instructions on demand.

This module is the adapter between our catalog rows and those ``Skill``
instances. It carries no disclosure logic of its own — that all lives in the
plugin now (the homegrown ``SkillRegistry``/``skill_tools`` dispatcher is
retired).
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

from strands import AgentSkills, Skill

logger = logging.getLogger(__name__)

_SLUG_STRIP = re.compile(r"[^a-z0-9-]+")
_SLUG_DEDUPE_HYPHENS = re.compile(r"-{2,}")
_MAX_SLUG_LEN = 64


def slugify_skill_name(skill_id: str) -> str:
    """Convert a catalog ``skill_id`` into an agentskills.io-valid skill name.

    The plugin uses ``Skill.name`` as both the injected label and the ``skills``
    tool activation key, and the standard bundle format (S3 ``SKILL.md``
    projection, harness portability) requires a slug matching
    ``^[a-z0-9]([a-z0-9-]*[a-z0-9])?$``. Our ``skill_id`` values are
    underscore-form (e.g. ``pdf_workflows``), so normalize: lowercase, ``_``→``-``,
    drop other invalid characters, collapse and trim hyphens, cap at 64 chars.

    Falls back to ``"skill"`` if normalization empties the string (defensive;
    ``skill_id`` is pattern-validated on write so this should not happen).
    """
    slug = skill_id.strip().lower().replace("_", "-")
    slug = _SLUG_STRIP.sub("-", slug)
    slug = _SLUG_DEDUPE_HYPHENS.sub("-", slug).strip("-")
    slug = slug[:_MAX_SLUG_LEN].strip("-")
    return slug or "skill"


def record_to_strands_skill(record: Any) -> Skill:
    """Map one ``SkillDefinition`` record to a Strands ``Skill``.

    ``metadata`` carries the true ``skill_id`` (the slug is lossy and is only an
    activation key) plus the human ``display_name`` and any frontmatter
    passthrough, so downstream code (access checks, ``read_skill_file``) can
    recover the catalog id from an activated skill.
    """
    skill_id = getattr(record, "skill_id", "")
    metadata = {
        "skill_id": skill_id,
        "display_name": getattr(record, "display_name", "") or skill_id,
        **(getattr(record, "skill_metadata", None) or {}),
    }
    allowed_tools = list(getattr(record, "allowed_tools", None) or [])
    return Skill(
        name=slugify_skill_name(skill_id),
        description=getattr(record, "description", "") or "",
        instructions=getattr(record, "instructions", "") or "",
        allowed_tools=allowed_tools or None,
        metadata=metadata,
    )


def _is_active_status(status: Any) -> bool:
    """True if a skill record's status is ACTIVE (handles enum or str)."""
    return str(status).split(".")[-1].lower() == "active"


def fetch_active_skill_records(skill_ids: List[str]) -> List[Any]:
    """Fetch ACTIVE skill records for ``skill_ids`` from the catalog repo.

    Bridges the async repository call into the sync agent-build path the same
    way the other tool-materialization helpers do. Returns an empty list on any
    failure — the agent then simply runs without skills.
    """
    if not skill_ids:
        return []

    import asyncio

    from apis.shared.skills.repository import get_skill_catalog_repository

    repo = get_skill_catalog_repository()

    async def _go() -> List[Any]:
        records = await repo.batch_get_skills(list(skill_ids))
        return [r for r in records if _is_active_status(getattr(r, "status", "active"))]

    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    return executor.submit(asyncio.run, _go()).result()
            return loop.run_until_complete(_go())
        except RuntimeError:
            return asyncio.run(_go())
    except Exception as e:  # noqa: BLE001 - degrade to no-skills on any error
        logger.warning("Could not load skill records: %s", e)
        return []


def build_skills_plugin(
    accessible_skill_ids: Optional[List[str]],
) -> Optional[AgentSkills]:
    """Build an ``AgentSkills`` plugin for the turn's effective skill set.

    Returns ``None`` when there are no accessible skills (the caller then adds
    no plugin and the agent behaves as a plain chat agent). Skills that fail to
    load or resolve are already dropped by ``fetch_active_skill_records``.
    """
    if not accessible_skill_ids:
        return None

    records = fetch_active_skill_records(list(accessible_skill_ids))
    if not records:
        return None

    skills = [record_to_strands_skill(r) for r in records]
    logger.info("Built AgentSkills plugin with %d skill(s)", len(skills))
    return AgentSkills(skills=skills)
