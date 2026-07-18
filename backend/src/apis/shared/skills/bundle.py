"""agentskills.io bundle helpers (Skills v2).

A skill is stored so that its S3 prefix is a valid agentskills.io bundle:

    skills/{skill_id}/SKILL.md
    skills/{skill_id}/references/{file}
    skills/{skill_id}/scripts/{file}      # inert (accept-and-inert)
    skills/{skill_id}/assets/{file}

DynamoDB remains the source of truth for metadata + instructions; the S3
``SKILL.md`` is a write-through projection generated from the row, so the prefix
is always attachable elsewhere (the managed-Harness lane, ``{"s3": {"uri": ...}}``)
and exportable as-is.

This module is under ``apis/shared`` because both the admin write path (app-api,
which regenerates ``SKILL.md``) and the runtime mapping (agents, which slugifies
the skill name for the Strands ``Skill``) need the same slug rule — neither may
import the other (import-boundary).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

_SLUG_STRIP = re.compile(r"[^a-z0-9-]+")
_SLUG_DEDUPE_HYPHENS = re.compile(r"-{2,}")
_MAX_SLUG_LEN = 64

# agentskills.io frontmatter keys we project explicitly; everything else in
# ``skill_metadata`` is passed through verbatim (round-trip-faithful, D2).
_RESERVED_METADATA_KEYS = frozenset({"name", "description", "allowed-tools", "instructions"})

# Resource ``kind`` → bundle subdirectory.
KIND_DIRS: dict[str, str] = {"reference": "references", "script": "scripts", "asset": "assets"}


def slugify_skill_name(skill_id: str) -> str:
    """Convert a catalog ``skill_id`` into an agentskills.io-valid skill name.

    The runtime plugin uses this as both the injected ``Skill.name`` label and
    the ``skills`` tool activation key, and it is the ``name:`` written into the
    projected ``SKILL.md`` frontmatter. Must match
    ``^[a-z0-9]([a-z0-9-]*[a-z0-9])?$``: lowercase, ``_``→``-``, drop other
    invalid characters, collapse and trim hyphens, cap at 64 chars. Falls back
    to ``"skill"`` if normalization empties the string (defensive — ``skill_id``
    is pattern-validated on write).
    """
    slug = skill_id.strip().lower().replace("_", "-")
    slug = _SLUG_STRIP.sub("-", slug)
    slug = _SLUG_DEDUPE_HYPHENS.sub("-", slug).strip("-")
    slug = slug[:_MAX_SLUG_LEN].strip("-")
    return slug or "skill"


def generate_skill_md(
    *,
    skill_id: str,
    description: str,
    instructions: str,
    allowed_tools: list[str] | None = None,
    skill_metadata: dict[str, Any] | None = None,
) -> str:
    """Render a SKILL.md (YAML frontmatter + markdown body) from a catalog row.

    ``name`` is the slug (so a strict agentskills.io loader accepts it);
    ``allowed-tools`` is emitted only when non-empty (advisory, D4); any extra
    ``skill_metadata`` keys (license, compatibility, ...) pass through except the
    reserved ones the projection owns.
    """
    frontmatter: dict[str, Any] = {
        "name": slugify_skill_name(skill_id),
        "description": description or "",
    }
    if allowed_tools:
        frontmatter["allowed-tools"] = list(allowed_tools)
    for key, value in (skill_metadata or {}).items():
        if key not in _RESERVED_METADATA_KEYS and key not in frontmatter:
            frontmatter[key] = value

    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    body = (instructions or "").strip()
    return f"---\n{fm}\n---\n\n{body}\n"
