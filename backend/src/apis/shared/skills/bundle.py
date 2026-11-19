"""agentskills.io bundle helpers (Skills v2).

A skill's files are stored under its S3 prefix in the agentskills.io layout:

    skills/{skill_id}/SKILL.md
    skills/{skill_id}/references/{file}
    skills/{skill_id}/scripts/{file}
    skills/{skill_id}/assets/{file}

DynamoDB remains the source of truth for metadata + instructions; the S3
``SKILL.md`` is a write-through projection generated from the row. Its
frontmatter is conformant (:func:`generate_skill_md`), but the prefix itself is
**not** a conformant bundle: the specification requires the directory to be
named after ``name``, and the directory here is the ``skill_id``, which can
differ (``pdf_workflows`` holds ``name: pdf-workflows``). An export is the
conformant artifact; the prefix is storage. The model never reads this file, so
changing its bytes re-writes no prompt.

This module is under ``apis/shared`` because both the admin write path (app-api,
which regenerates ``SKILL.md``) and the runtime mapping (agents, which slugifies
the skill name for the Strands ``Skill``) need the same slug rule — neither may
import the other (import-boundary).
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Any

import yaml

from apis.shared.skills.skill_md import (
    COMPATIBILITY_MAX_LENGTH,
    LICENSE_MAX_LENGTH,
    ROW_OWNED_METADATA_KEYS,
    validate_frontmatter,
)

logger = logging.getLogger(__name__)

_SLUG_STRIP = re.compile(r"[^a-z0-9-]+")
_SLUG_DEDUPE_HYPHENS = re.compile(r"-{2,}")
_MAX_SLUG_LEN = 64

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


def _plain(value: Any) -> Any:
    """``value`` with DynamoDB's ``Decimal`` numbers turned back into ints and floats."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = [_plain(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, set) else items
    return value


def _stringify(value: Any) -> str:
    """A ``metadata`` value as a string: a string as is, anything else as sorted JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(_plain(value), sort_keys=True, ensure_ascii=False, default=str)


def _projected_metadata(skill_metadata: dict[str, Any]) -> dict[str, str]:
    """Everything in ``skill_metadata`` that has no top-level key, as string → string.

    The row's ``metadata`` map, if it has one, plus every non-standard key
    (stringified). An explicit ``metadata`` entry wins a key clash. ``license``
    and ``compatibility`` land here only when they are not valid strings, so
    nothing stored is lost. Keys the row owns (``name``, ``description``,
    ``instructions``, ``allowed-tools``, ``compose``) are left out. Sorted, so
    the output does not depend on the order DynamoDB returns a map in.
    """
    folded: dict[str, str] = {}
    for key, value in skill_metadata.items():
        key = str(key)
        if key in ROW_OWNED_METADATA_KEYS:
            continue
        if key == "metadata" and isinstance(value, dict):
            continue
        if key in ("license", "compatibility") and (
            value in (None, "") or _valid_top_level(key, value)
        ):
            continue
        folded[key] = _stringify(value)
    explicit = skill_metadata.get("metadata")
    if isinstance(explicit, dict):
        for key, value in explicit.items():
            folded[str(key)] = _stringify(value)
    return {key: folded[key] for key in sorted(folded)}


def _valid_top_level(key: str, value: Any) -> bool:
    if key == "license":
        return isinstance(value, str) and bool(value) and len(value) <= LICENSE_MAX_LENGTH
    return isinstance(value, str) and bool(value) and len(value) <= COMPATIBILITY_MAX_LENGTH


def skill_md_frontmatter(
    *,
    skill_id: str,
    description: str,
    allowed_tools: list[str] | None = None,
    skill_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The frontmatter :func:`generate_skill_md` writes, as a dict in output order."""
    stored = dict(skill_metadata or {})
    frontmatter: dict[str, Any] = {
        "name": slugify_skill_name(skill_id),
        "description": description or "",
    }
    for key in ("license", "compatibility"):
        value = stored.get(key)
        if _valid_top_level(key, value):
            frontmatter[key] = value
    metadata = _projected_metadata(stored)
    if metadata:
        frontmatter["metadata"] = metadata
    tools = [name for tool in (allowed_tools or []) for name in str(tool).split()]
    if tools:
        frontmatter["allowed-tools"] = " ".join(tools)
    return frontmatter


def generate_skill_md(
    *,
    skill_id: str,
    description: str,
    instructions: str,
    allowed_tools: list[str] | None = None,
    skill_metadata: dict[str, Any] | None = None,
) -> str:
    """Render a SKILL.md (YAML frontmatter + markdown body) from a catalog row.

    The frontmatter has only the six agentskills.io keys, in the order
    ``name``, ``description``, ``license``, ``compatibility``, ``metadata``,
    ``allowed-tools``; an optional key with no value is left out.

    - ``name`` is the slug of ``skill_id``, so it is always valid.
    - ``license`` and ``compatibility`` come from ``skill_metadata`` when they
      are valid strings.
    - ``metadata`` is the row's ``skill_metadata["metadata"]`` map plus every
      other non-standard ``skill_metadata`` key, all as strings.
    - ``allowed-tools`` is one space-separated string (advisory, D4).

    ``compose`` is never written. The output is deterministic for a given row.

    It never raises. A row that cannot produce a valid file (a legacy empty
    description, metadata over the size limit) is still projected, and a
    warning naming the skill id is logged, because the row is the source of
    truth and a failed projection must never fail a catalog write.
    """
    try:
        frontmatter = skill_md_frontmatter(
            skill_id=skill_id,
            description=description,
            allowed_tools=allowed_tools,
            skill_metadata=skill_metadata,
        )
    except Exception:  # noqa: BLE001 - a projection must never fail a write
        logger.warning(
            "SKILL.md projection for skill=%s could not read its metadata; "
            "writing name and description only",
            skill_id,
            exc_info=True,
        )
        frontmatter = {"name": slugify_skill_name(skill_id), "description": str(description or "")}
    errors = [d for d in validate_frontmatter(frontmatter, strict=True) if d.is_error]
    if errors:
        logger.warning(
            "SKILL.md projection for skill=%s is not agentskills.io-valid (%s); "
            "writing it anyway",
            skill_id,
            ", ".join(sorted({d.code for d in errors})),
        )
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    body = (instructions or "").strip()
    return f"---\n{fm}\n---\n\n{body}\n"
