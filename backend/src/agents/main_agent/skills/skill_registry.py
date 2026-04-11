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
                    "path": skill_dir,
                    "md_path": skill_md,
                }
                count += 1
                logger.info(f"Discovered skill: {name}")

            except Exception as e:
                logger.error(f"Error parsing {skill_md}: {e}")

        logger.info(f"Discovered {count} skills from {self._skills_dir}")
        return count

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

        try:
            with open(skill["md_path"], "r", encoding="utf-8") as f:
                content = f.read()
            return self._strip_frontmatter(content)
        except Exception as e:
            logger.error(f"Error loading instructions for '{skill_name}': {e}")
            return None

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
