"""
Skill Registry — single source of truth for skill discovery and access.

Scans a skills definitions directory for SKILL.md files, parses their
frontmatter for metadata, and provides three levels of access:

- Level 1: get_catalog() → lightweight listing for system prompt injection
- Level 2: load_instructions(name) → full SKILL.md body on demand
- Level 3: get_tools(name) → executable tool objects

Based on the progressive disclosure pattern from:
https://github.com/aws-samples/sample-strands-agent-with-agentcore
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Regex for parsing YAML frontmatter (no PyYAML dependency)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_YAML_LINE_RE = re.compile(r"^(\w+):\s*(.*)$")
_YAML_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.+)$")


def _ref_attr(ref: Any, name: str) -> Any:
    """Read a reference-manifest field, tolerating pydantic objects or dicts.

    Records loaded from the repository carry ``SkillResourceRef`` objects;
    duck-typed test stand-ins may pass dicts (snake_case or camelCase).
    """
    value = getattr(ref, name, None)
    if value is None and isinstance(ref, dict):
        value = ref.get(name)
    return value


class SkillRegistry:
    """
    Registry for discovering and managing agent skills.

    Skills are defined by SKILL.md files in a directory structure:
        definitions/
        ├── web-search/
        │   └── SKILL.md
        ├── visualization/
        │   └── SKILL.md
        └── ...

    Each SKILL.md has YAML frontmatter:
        ---
        name: web-search
        description: Search the web using DuckDuckGo
        type: tool
        ---
        <markdown instructions>
    """

    def __init__(self, skills_dir: Optional[str] = None):
        """
        Initialize the registry.

        Args:
            skills_dir: Path to skills definitions directory.
                        Defaults to ./definitions/ relative to this module.
        """
        if skills_dir is None:
            skills_dir = os.path.join(os.path.dirname(__file__), "definitions")
        self._skills_dir = skills_dir
        self._skills: Dict[str, Dict[str, Any]] = {}

    def discover_skills(self) -> int:
        """
        Scan the skills directory for SKILL.md files.

        Returns:
            int: Number of skills discovered
        """
        if not os.path.isdir(self._skills_dir):
            logger.warning(f"Skills directory not found: {self._skills_dir}")
            return 0

        count = 0
        for entry in sorted(os.listdir(self._skills_dir)):
            skill_dir = os.path.join(self._skills_dir, entry)
            skill_md = os.path.join(skill_dir, "SKILL.md")

            if not os.path.isfile(skill_md):
                continue

            try:
                with open(skill_md, "r", encoding="utf-8") as f:
                    content = f.read()

                meta = self._parse_frontmatter(content)
                name = meta.get("name", entry)

                self._skills[name] = {
                    "description": meta.get("description", ""),
                    "type": meta.get("type", "tool"),
                    "compose": meta.get("compose", []),
                    "tools": [],
                    "bound_tool_ids": [],
                    "resources": [],
                    # File mode: instructions live in the SKILL.md body, read
                    # on demand from md_path (kept None-instructions so
                    # load_instructions takes the file path).
                    "instructions": None,
                    "path": skill_dir,
                    "md_path": skill_md,
                }
                count += 1
                logger.info(f"Discovered skill: {name}")

            except Exception as e:
                logger.error(f"Error parsing {skill_md}: {e}")

        logger.info(f"Discovered {count} skills from {self._skills_dir}")
        return count

    def load_records(self, records: List[Any]) -> int:
        """Populate the registry from DynamoDB-backed skill records.

        The admin-managed (DB) source of skills (PR-6). Each record is a
        ``SkillDefinition``-shaped object (duck-typed via ``getattr`` so tests
        can pass simple stand-ins): ``skill_id``, ``description``,
        ``instructions``, ``compose``, ``bound_tool_ids``, ``resources``.

        Unlike ``discover_skills`` (file scan), instructions are carried inline
        on the record (no file), and the skill keys on ``skill_id`` — the
        stable id the model references in the catalog. Returns the number of
        records loaded.
        """
        count = 0
        for rec in records:
            name = getattr(rec, "skill_id", None)
            if not name:
                continue
            self._skills[name] = {
                "description": getattr(rec, "description", "") or "",
                "type": "tool",
                "compose": list(getattr(rec, "compose", []) or []),
                "tools": [],
                "bound_tool_ids": list(getattr(rec, "bound_tool_ids", []) or []),
                # Reference-file manifest — served on demand in PR-6b.
                "resources": list(getattr(rec, "resources", []) or []),
                # DB mode: instructions are inline (no md_path file).
                "instructions": getattr(rec, "instructions", "") or "",
                "path": None,
                "md_path": None,
            }
            count += 1
            logger.info(f"Loaded skill record: {name}")

        logger.info(f"Loaded {count} skill records from repository")
        return count

    def all_bound_tool_ids(self) -> List[str]:
        """Return the de-duplicated union of every skill's bound catalog
        tool ids (admin/DB skills). Used to augment the agent's tool universe
        so a granted skill's bound tools materialize (skill-as-grant)."""
        seen: Dict[str, None] = {}
        for info in self._skills.values():
            for tid in info.get("bound_tool_ids", []) or []:
                seen.setdefault(tid, None)
        return list(seen.keys())

    def bind_catalog_tools(self, catalog_map: Dict[str, Any]) -> int:
        """Bind tools to skills by catalog ``tool_id`` (admin/DB skills).

        ``catalog_map`` maps a catalog ``tool_id`` to the live tool object the
        agent materialized for it — or a *list* of objects, since one catalog
        id can expand to several runtime tools (a gateway target or external
        MCP server with many tools; see ``mcp_binding.resolve_mcp_bindings``).
        For each skill, every ``bound_tool_id`` that resolves in the map is
        attached. This is the cross-source-aware analog of ``bind_tools`` (which
        matches the local-only ``_skill_name`` stamp): local tools and folded
        gateway/external MCP tools both resolve here in PR-6b.

        Returns the number of (skill, tool) bindings made.
        """
        bound = 0
        for info in self._skills.values():
            existing_ids = {id(t) for t in info["tools"]}
            for tid in info.get("bound_tool_ids", []) or []:
                value = catalog_map.get(tid)
                if value is None:
                    continue
                tool_objs = value if isinstance(value, list) else [value]
                for tool_obj in tool_objs:
                    if id(tool_obj) not in existing_ids:
                        info["tools"].append(tool_obj)
                        existing_ids.add(id(tool_obj))
                        bound += 1
        logger.info(f"Bound {bound} catalog tools across {len(self._skills)} skills")
        return bound

    def bind_tools(self, tools: List[Any]) -> int:
        """
        Attach tool objects to their parent skills.

        Tools are matched by the _skill_name attribute set by the @skill decorator
        or register_skill() function.

        Args:
            tools: List of tool objects (functions with _skill_name metadata)

        Returns:
            int: Number of tools bound
        """
        bound = 0
        for tool_obj in tools:
            skill_name = getattr(tool_obj, "_skill_name", None)
            if skill_name and skill_name in self._skills:
                self._skills[skill_name]["tools"].append(tool_obj)
                bound += 1
                logger.debug(f"Bound tool to skill '{skill_name}'")

        logger.info(f"Bound {bound} tools to {len(self._skills)} skills")
        return bound

    def get_catalog(self) -> str:
        """
        Generate Level 1 catalog for system prompt injection.

        Returns a lightweight listing of skill names and descriptions,
        designed to be token-efficient while giving the LLM enough
        information to decide which skill to activate.

        Returns:
            str: Markdown-formatted skill catalog
        """
        if not self._skills:
            return ""

        lines = ["## Available Skills", ""]
        lines.append("Use `skill_dispatcher` to activate a skill and get its instructions.")
        lines.append("Use `skill_executor` to run a skill's tools.")
        lines.append("")

        for name, info in sorted(self._skills.items()):
            desc = info["description"]
            tool_count = len(info["tools"])
            compose = info.get("compose", [])

            if compose:
                lines.append(f"- **{name}**: {desc} _(combines: {', '.join(compose)})_")
            elif tool_count > 0:
                lines.append(f"- **{name}**: {desc} ({tool_count} tools)")
            else:
                lines.append(f"- **{name}**: {desc}")

        return "\n".join(lines)

    def load_instructions(self, skill_name: str) -> Optional[str]:
        """
        Load Level 2 instructions (SKILL.md body without frontmatter).

        Args:
            skill_name: Skill identifier

        Returns:
            str: Markdown instructions, or None if skill not found
        """
        skill = self._skills.get(skill_name)
        if not skill:
            return None

        # DB mode: instructions are carried inline on the record (no file).
        if not skill.get("md_path"):
            return skill.get("instructions") or ""

        try:
            with open(skill["md_path"], "r", encoding="utf-8") as f:
                content = f.read()
            return self._strip_frontmatter(content)
        except Exception as e:
            logger.error(f"Error loading instructions for '{skill_name}': {e}")
            return None

    def get_resource_names(self, skill_name: str) -> List[str]:
        """List a skill's supporting reference filenames (Level-2.5).

        These are the deep progressive-disclosure files (e.g. ``forms.md``)
        that the SKILL.md body refers to. ``skill_dispatcher`` surfaces this
        list so the model knows which files it may read; the bytes live in S3
        (see :meth:`read_resource`). Empty for file/dev skills (no manifest).
        """
        skill = self._skills.get(skill_name)
        if not skill:
            return []
        names: List[str] = []
        for ref in skill.get("resources", []) or []:
            filename = _ref_attr(ref, "filename")
            if filename:
                names.append(filename)
        return names

    def read_resource(
        self, skill_name: str, filename: str, store: Optional[Any] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch one of a skill's reference files on demand (Level-3 disclosure).

        Resolves ``filename`` against the skill's ``resources`` manifest and
        reads the bytes from the S3-backed ``SkillResourceStore`` (the runtime
        was granted read access to the skill-resources bucket in PR-6a). Returns
        a dict the dispatcher serializes:

          - ``{"filename", "content_type", "content"}`` for text;
          - ``{"filename", "content_type", "size", "note"}`` for binary
            (never dumps raw bytes into the model context);
          - ``{"error": ...}`` if the file is missing or storage is unavailable.

        ``None`` only when the skill itself is unknown. ``store`` is injectable
        for tests; defaults to the process-global store.
        """
        skill = self._skills.get(skill_name)
        if not skill:
            return None

        ref = None
        for candidate in skill.get("resources", []) or []:
            if _ref_attr(candidate, "filename") == filename:
                ref = candidate
                break
        if ref is None:
            return {"error": f"Reference file '{filename}' not found in skill '{skill_name}'"}

        s3_key = _ref_attr(ref, "s3_key") or _ref_attr(ref, "s3Key")
        content_type = _ref_attr(ref, "content_type") or _ref_attr(ref, "contentType") or ""
        size = _ref_attr(ref, "size")
        if not s3_key:
            return {"error": f"Reference file '{filename}' has no storage key"}

        from apis.shared.skills.resource_store import (
            SkillResourceStoreError,
            get_skill_resource_store,
        )

        store = store or get_skill_resource_store()
        try:
            data = store.get(s3_key)
        except SkillResourceStoreError as e:
            logger.warning("Could not read reference '%s' for skill '%s': %s", filename, skill_name, e)
            return {"error": f"Could not read reference file '{filename}': {e}"}

        try:
            text = data.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return {
                "filename": filename,
                "content_type": content_type or "application/octet-stream",
                "size": size if size is not None else len(data),
                "note": "Binary reference file — not rendered as text.",
            }
        return {
            "filename": filename,
            "content_type": content_type or "text/markdown",
            "content": text,
        }

    def get_tools(self, skill_name: str) -> List[Any]:
        """
        Get Level 3 tool objects for a skill.

        For composite skills, aggregates tools from all composed skills.

        Args:
            skill_name: Skill identifier

        Returns:
            list: Tool objects, empty if skill not found
        """
        skill = self._skills.get(skill_name)
        if not skill:
            return []

        # For composite skills, aggregate tools from composed skills
        compose = skill.get("compose", [])
        if compose:
            tools = []
            for child_name in compose:
                tools.extend(self.get_tools(child_name))
            return tools

        return list(skill["tools"])

    def get_tool_schemas(self, skill_name: str) -> List[Dict]:
        """
        Get tool parameter schemas for a skill's tools.

        Extracts the tool_spec from each tool for inclusion in
        skill_dispatcher responses, so the LLM knows what parameters
        to pass to skill_executor.

        Args:
            skill_name: Skill identifier

        Returns:
            list: Tool specification dicts
        """
        tools = self.get_tools(skill_name)
        schemas = []
        for tool_obj in tools:
            spec = getattr(tool_obj, "tool_spec", None)
            if spec:
                schemas.append(spec)
            elif hasattr(tool_obj, "tool_name"):
                schemas.append({"name": tool_obj.tool_name})
        return schemas

    def has_skill(self, skill_name: str) -> bool:
        """Check if a skill is registered."""
        return skill_name in self._skills

    def get_skill_names(self) -> List[str]:
        """Get all registered skill names."""
        return list(self._skills.keys())

    def get_skill_count(self) -> int:
        """Get total number of registered skills."""
        return len(self._skills)

    # --- Internal helpers ---

    @staticmethod
    def _parse_frontmatter(content: str) -> Dict[str, Any]:
        """Parse YAML frontmatter from a SKILL.md file (no PyYAML dependency)."""
        match = _FRONTMATTER_RE.match(content)
        if not match:
            return {}

        result = {}
        current_key = None
        current_list = None

        for line in match.group(1).splitlines():
            # Check for key: value pair
            kv_match = _YAML_LINE_RE.match(line)
            if kv_match:
                if current_key and current_list is not None:
                    result[current_key] = current_list

                key = kv_match.group(1)
                value = kv_match.group(2).strip().strip('"').strip("'")
                current_key = key
                current_list = None

                if value:
                    result[key] = value
                else:
                    # Empty value — next lines may be a list
                    result[key] = ""
                continue

            # Check for list item
            list_match = _YAML_LIST_ITEM_RE.match(line)
            if list_match and current_key:
                if current_list is None:
                    current_list = []
                    result[current_key] = current_list
                current_list.append(list_match.group(1).strip())

        if current_key and current_list is not None:
            result[current_key] = current_list

        return result

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """Remove YAML frontmatter from content, returning the markdown body."""
        match = _FRONTMATTER_RE.match(content)
        if match:
            return content[match.end():].strip()
        return content.strip()
