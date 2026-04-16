"""Tests for SkillRegistry — discovery, binding, and three-level access."""

import pytest
from agents.main_agent.skills.skill_registry import SkillRegistry


class TestDiscovery:
    """Req SK-1: Skill discovery from SKILL.md files."""

    def test_discovers_all_skills(self, registry):
        assert registry.get_skill_count() == 3

    def test_discovers_skill_names(self, registry):
        names = sorted(registry.get_skill_names())
        assert names == ["data-analysis", "visualization", "web-search"]

    def test_empty_directory_returns_zero(self, tmp_path):
        reg = SkillRegistry(str(tmp_path))
        assert reg.discover_skills() == 0

    def test_nonexistent_directory_returns_zero(self):
        reg = SkillRegistry("/nonexistent/path")
        assert reg.discover_skills() == 0

    def test_has_skill_true(self, registry):
        assert registry.has_skill("web-search") is True

    def test_has_skill_false(self, registry):
        assert registry.has_skill("nonexistent") is False


class TestFrontmatterParsing:
    """Req SK-2: YAML frontmatter parsing without PyYAML."""

    def test_parses_name(self, registry):
        assert registry.has_skill("web-search")

    def test_parses_composite_compose_list(self, registry):
        skills = registry._skills
        assert "data-analysis" in skills
        assert skills["data-analysis"]["compose"] == ["web-search", "visualization"]

    def test_parses_type(self, registry):
        assert registry._skills["web-search"]["type"] == "tool"
        assert registry._skills["data-analysis"]["type"] == "composite"

    def test_parses_description(self, registry):
        assert registry._skills["web-search"]["description"] == "Search the web for current information"

    def test_no_frontmatter_returns_empty(self):
        result = SkillRegistry._parse_frontmatter("Just plain markdown")
        assert result == {}


class TestToolBinding:
    """Req SK-3: Tool binding via _skill_name metadata."""

    def test_binds_tool_to_skill(self, registry, mock_tool):
        bound = registry.bind_tools([mock_tool])
        assert bound == 1
        assert len(registry.get_tools("web-search")) == 1

    def test_ignores_tool_without_skill_name(self, registry):
        def orphan_tool(): pass
        bound = registry.bind_tools([orphan_tool])
        assert bound == 0

    def test_ignores_tool_with_unknown_skill(self, registry):
        def stray_tool(): pass
        stray_tool._skill_name = "nonexistent-skill"
        bound = registry.bind_tools([stray_tool])
        assert bound == 0

    def test_binds_multiple_tools(self, registry, mock_tool, viz_tool):
        bound = registry.bind_tools([mock_tool, viz_tool])
        assert bound == 2


class TestCatalog:
    """Req SK-4: Level 1 — catalog generation for system prompt."""

    def test_catalog_contains_skill_names(self, registry):
        catalog = registry.get_catalog()
        assert "web-search" in catalog
        assert "visualization" in catalog
        assert "data-analysis" in catalog

    def test_catalog_contains_descriptions(self, registry):
        catalog = registry.get_catalog()
        assert "Search the web for current information" in catalog

    def test_catalog_shows_composite_combines(self, registry):
        catalog = registry.get_catalog()
        assert "combines:" in catalog

    def test_empty_registry_returns_empty_string(self):
        reg = SkillRegistry("/nonexistent")
        assert reg.get_catalog() == ""

    def test_catalog_shows_tool_count(self, registry, mock_tool):
        registry.bind_tools([mock_tool])
        catalog = registry.get_catalog()
        assert "(1 tools)" in catalog


class TestInstructions:
    """Req SK-5: Level 2 — instructions loading."""

    def test_loads_instructions_without_frontmatter(self, registry):
        instructions = registry.load_instructions("web-search")
        assert instructions is not None
        assert "# Web Search" in instructions
        assert "---" not in instructions
        assert "name:" not in instructions

    def test_unknown_skill_returns_none(self, registry):
        assert registry.load_instructions("nonexistent") is None


class TestTools:
    """Req SK-6: Level 3 — tool access."""

    def test_get_tools_returns_bound_tools(self, registry, mock_tool):
        registry.bind_tools([mock_tool])
        tools = registry.get_tools("web-search")
        assert len(tools) == 1
        assert tools[0].tool_name == "web_search"

    def test_composite_skill_aggregates_tools(self, registry, mock_tool, viz_tool):
        registry.bind_tools([mock_tool, viz_tool])
        tools = registry.get_tools("data-analysis")
        assert len(tools) == 2

    def test_unknown_skill_returns_empty(self, registry):
        assert registry.get_tools("nonexistent") == []

    def test_get_tool_schemas(self, registry, mock_tool):
        registry.bind_tools([mock_tool])
        schemas = registry.get_tool_schemas("web-search")
        assert len(schemas) == 1
        assert schemas[0]["name"] == "web_search"
