"""Tests for skill_dispatcher and skill_executor tools."""

import json
import pytest

from agents.main_agent.skills.skill_registry import SkillRegistry
from agents.main_agent.skills.skill_tools import (
    skill_dispatcher,
    skill_executor,
    set_dispatcher_registry,
)


@pytest.fixture(autouse=True)
def setup_registry(registry, mock_tool, viz_tool):
    """Wire up registry with tools for all tests in this module."""
    registry.bind_tools([mock_tool, viz_tool])
    set_dispatcher_registry(registry)
    yield
    set_dispatcher_registry(None)


class TestSkillDispatcher:
    """Req SD-1: skill_dispatcher loads instructions and schemas."""

    def test_returns_instructions(self, registry):
        result = json.loads(skill_dispatcher(skill_name="web-search"))
        assert "instructions" in result
        assert "# Web Search" in result["instructions"]

    def test_returns_tool_schemas(self, registry):
        result = json.loads(skill_dispatcher(skill_name="web-search"))
        assert "tool_schemas" in result
        assert len(result["tool_schemas"]) == 1
        assert result["tool_schemas"][0]["name"] == "web_search"

    def test_unknown_skill_returns_error(self):
        result = json.loads(skill_dispatcher(skill_name="nonexistent"))
        assert "error" in result
        assert "Unknown skill" in result["error"]

    def test_unknown_skill_lists_available(self):
        result = json.loads(skill_dispatcher(skill_name="nonexistent"))
        assert "available_skills" in result

    def test_no_registry_returns_error(self):
        set_dispatcher_registry(None)
        result = json.loads(skill_dispatcher(skill_name="web-search"))
        assert "error" in result
        assert "not initialized" in result["error"]


class TestSkillExecutor:
    """Req SE-1: skill_executor runs tools within a skill."""

    def test_executes_tool(self):
        result = skill_executor(
            skill_name="web-search",
            tool_name="web_search",
            tool_input={"query": "test search"}
        )
        assert result == "Results for: test search"

    def test_json_string_input_parsed(self):
        result = skill_executor(
            skill_name="web-search",
            tool_name="web_search",
            tool_input='{"query": "parsed"}'
        )
        assert result == "Results for: parsed"

    def test_unknown_skill_returns_error(self):
        result = json.loads(skill_executor(
            skill_name="nonexistent",
            tool_name="anything",
        ))
        assert "error" in result

    def test_unknown_tool_returns_error_with_available(self):
        result = json.loads(skill_executor(
            skill_name="web-search",
            tool_name="nonexistent_tool",
        ))
        assert "error" in result
        assert "available_tools" in result

    def test_no_registry_returns_error(self):
        set_dispatcher_registry(None)
        result = json.loads(skill_executor(
            skill_name="web-search",
            tool_name="web_search",
        ))
        assert "error" in result


class TestDecorators:
    """Req SD-2: Skill decorators apply metadata."""

    def test_skill_decorator_sets_attribute(self):
        from agents.main_agent.skills.decorators import skill

        @skill("my-skill")
        def my_tool():
            pass

        assert my_tool._skill_name == "my-skill"

    def test_register_skill_sets_attributes(self):
        from agents.main_agent.skills.decorators import register_skill

        def tool_a(): pass
        def tool_b(): pass

        register_skill("batch-skill", tools=[tool_a, tool_b])
        assert tool_a._skill_name == "batch-skill"
        assert tool_b._skill_name == "batch-skill"
