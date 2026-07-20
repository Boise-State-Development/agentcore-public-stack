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
from typing import Any, List, Optional

from strands import AgentSkills, Skill, tool

# Single source of truth for the slug rule (shared with the app-api SKILL.md
# write-through projection — the import-boundary forbids app_api importing here).
from apis.shared.skills.bundle import KIND_DIRS, slugify_skill_name
from apis.shared.skills.resource_store import (
    SkillResourceStoreError,
    get_skill_resource_store,
)

logger = logging.getLogger(__name__)

__all__ = [
    "slugify_skill_name",
    "record_to_strands_skill",
    "fetch_active_skill_records",
    "build_skills_plugin",
    "build_skills_runtime",
    "make_read_skill_file_tool",
]

# Text MIME types whose bytes ``read_skill_file`` returns inline. Anything else
# (images, PDFs, archives) is described, never dumped as raw bytes.
_TEXT_PREFIXES = ("text/",)
_TEXT_EXACT = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/javascript",
    }
)
_INERT_SCRIPT_NOTICE = (
    "[This script is stored with the skill for reference only. It is NOT "
    "executable on this platform — read it to understand the approach, then "
    "reproduce the steps yourself.]"
)


def _manifest_path(ref: Any) -> str:
    """Standard bundle path for a manifest entry, e.g. ``references/forms.md``."""
    subdir = KIND_DIRS.get(getattr(ref, "kind", "reference"), "references")
    return f"{subdir}/{ref.filename}"


def _manifest_paths(record: Any) -> List[str]:
    """All standard bundle paths a skill's reference files can be read at."""
    return [_manifest_path(r) for r in getattr(record, "resources", None) or []]


def _reference_files_section(record: Any) -> str:
    """A generated 'Available reference files' block for a skill's instructions.

    Programmatic ``Skill`` instances carry no filesystem, so the plugin cannot
    list resources on activation. We append this listing to the instructions so
    the model knows what it can pull with ``read_skill_file`` (spec §5, L3).
    """
    paths = _manifest_paths(record)
    if not paths:
        return ""
    lines = "\n".join(f"- {p}" for p in paths)
    return (
        "\n\n## Available reference files\n"
        "Load any of these with the `read_skill_file` tool "
        f"(skill_name `{slugify_skill_name(getattr(record, 'skill_id', ''))}`, "
        "path = one of the entries below):\n"
        f"{lines}"
    )


def record_to_strands_skill(record: Any) -> Skill:
    """Map one ``SkillDefinition`` record to a Strands ``Skill``.

    ``metadata`` carries the true ``skill_id`` (the slug is lossy and is only an
    activation key) plus the human ``display_name`` and any frontmatter
    passthrough, so downstream code (access checks, ``read_skill_file``) can
    recover the catalog id from an activated skill. The instructions gain a
    generated reference-file listing (L3 disclosure) when the skill has any.
    """
    skill_id = getattr(record, "skill_id", "")
    metadata = {
        "skill_id": skill_id,
        "display_name": getattr(record, "display_name", "") or skill_id,
        **(getattr(record, "skill_metadata", None) or {}),
    }
    allowed_tools = list(getattr(record, "allowed_tools", None) or [])
    instructions = (getattr(record, "instructions", "") or "") + _reference_files_section(
        record
    )
    return Skill(
        name=slugify_skill_name(skill_id),
        description=getattr(record, "description", "") or "",
        instructions=instructions,
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


def _is_text_content(content_type: str) -> bool:
    """True if the MIME type is one we return inline as text."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct.startswith(_TEXT_PREFIXES) or ct in _TEXT_EXACT


def _resolve_resource(record: Any, path: str) -> Optional[Any]:
    """Resolve ``path`` against a skill's manifest (no filesystem, no traversal).

    Accepts either the bare filename (``forms.md``) or the standard bundle path
    (``references/forms.md``). Pure manifest lookup — the returned ref's stored
    ``s3_key`` is the only thing ever read.
    """
    wanted = (path or "").strip().lstrip("/")
    for ref in getattr(record, "resources", None) or []:
        if wanted == ref.filename or wanted == _manifest_path(ref):
            return ref
    return None


def make_read_skill_file_tool(records: List[Any]):
    """Build the per-turn ``read_skill_file`` tool bound to ``records``.

    The tool is the S3-vs-filesystem adapter the spec calls for (§5): the
    ``AgentSkills`` plugin discloses metadata + instructions but never reads
    files for programmatic skills.

    **Access (§6) is enforced structurally, not re-checked per call.** ``records``
    is exactly the turn's effective skill set — already resolved against the
    invoker (catalog ∪ own for plain chat; the invoke-through predicate for an
    Agent's bindings) and narrowed by the opt-in selection. A skill the invoker
    cannot use is simply absent from ``by_slug``, so there is no id the model can
    name to reach one. That is strictly stronger than re-running the predicate on
    an arbitrary caller-supplied id, and it keeps a single resolution point:
    anything that widens access has to widen the effective set, where the tiering
    rules actually live.
    """
    by_slug = {slugify_skill_name(getattr(r, "skill_id", "")): r for r in records}
    store = get_skill_resource_store()

    @tool
    def read_skill_file(skill_name: str, path: str) -> str:
        """Read one of an active skill's reference files.

        Use this to load a reference file listed in a skill's "Available
        reference files" section (after you have activated the skill).

        Args:
            skill_name: The skill's name (as shown in ``<available_skills>``).
            path: A path from the skill's reference-file listing, e.g.
                ``references/forms.md``.
        """
        record = by_slug.get(skill_name)
        if record is None:
            available = ", ".join(sorted(by_slug)) or "(none)"
            return (
                f"Skill '{skill_name}' is not available in this conversation. "
                f"Available skills: {available}"
            )

        ref = _resolve_resource(record, path)
        if ref is None:
            listing = ", ".join(_manifest_paths(record)) or "(no reference files)"
            return (
                f"No reference file '{path}' in skill '{skill_name}'. "
                f"Available paths: {listing}"
            )

        try:
            raw = store.get(ref.s3_key)
        except SkillResourceStoreError as e:
            logger.warning("read_skill_file: %s", e)
            return f"Could not read '{path}' from skill '{skill_name}'."

        kind = getattr(ref, "kind", "reference")
        if kind == "script":
            return f"{_INERT_SCRIPT_NOTICE}\n\n{raw.decode('utf-8', errors='replace')}"
        if kind == "asset" or not _is_text_content(ref.content_type):
            return (
                f"'{path}' is a binary {kind} ({ref.content_type or 'unknown type'}, "
                f"{ref.size} bytes) and cannot be shown as text."
            )
        return raw.decode("utf-8", errors="replace")

    return read_skill_file


def build_skills_runtime(
    accessible_skill_ids: Optional[List[str]],
):
    """Build the skills runtime for a turn: (AgentSkills plugin, read tool).

    Fetches the effective skill records once and returns both the disclosure
    plugin and the ``read_skill_file`` tool bound to those records. Returns
    ``(None, None)`` when there are no accessible/active skills — the caller
    then wires neither and the agent behaves as a plain chat agent.
    """
    if not accessible_skill_ids:
        return None, None

    records = fetch_active_skill_records(list(accessible_skill_ids))
    if not records:
        return None, None

    # Deterministic order regardless of fetch order: these records become the
    # <available_skills> system-prompt block, and any order flip between turns
    # of a session invalidates the Bedrock prompt cache (exact-prefix match).
    records = sorted(records, key=lambda r: getattr(r, "skill_id", "") or "")

    skills = [record_to_strands_skill(r) for r in records]
    plugin = AgentSkills(skills=skills)
    read_tool = make_read_skill_file_tool(records)
    logger.info("Built skills runtime: %d skill(s) + read_skill_file", len(skills))
    return plugin, read_tool


def build_skills_plugin(
    accessible_skill_ids: Optional[List[str]],
) -> Optional[AgentSkills]:
    """Back-compat shim: return only the ``AgentSkills`` plugin.

    Prefer :func:`build_skills_runtime`, which also returns the
    ``read_skill_file`` tool from the same single record fetch.
    """
    plugin, _ = build_skills_runtime(accessible_skill_ids)
    return plugin
