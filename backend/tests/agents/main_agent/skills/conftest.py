"""Shared fixtures for skill tests."""

import os
import pytest
import tempfile

from agents.main_agent.skills.skill_registry import SkillRegistry


@pytest.fixture
def temp_skills_dir(tmp_path):
    """Create a temporary skills directory with sample SKILL.md files."""
    # Create web-search skill
    web_dir = tmp_path / "web-search"
    web_dir.mkdir()
    (web_dir / "SKILL.md").write_text(
        '---\n'
        'name: web-search\n'
        'description: Search the web for current information\n'
        'type: tool\n'
        '---\n'
        '\n'
        '# Web Search\n'
        '\n'
        'Use this skill to search the web.\n'
    )

    # Create visualization skill
    viz_dir = tmp_path / "visualization"
    viz_dir.mkdir()
    (viz_dir / "SKILL.md").write_text(
        '---\n'
        'name: visualization\n'
        'description: Create charts and graphs\n'
        'type: tool\n'
        '---\n'
        '\n'
        '# Visualization\n'
        '\n'
        'Create data visualizations.\n'
    )

    # Create composite skill
    comp_dir = tmp_path / "data-analysis"
    comp_dir.mkdir()
    (comp_dir / "SKILL.md").write_text(
        '---\n'
        'name: data-analysis\n'
        'description: Analyze data with search and visualization\n'
        'type: composite\n'
        'compose:\n'
        '  - web-search\n'
        '  - visualization\n'
        '---\n'
        '\n'
        '# Data Analysis\n'
        '\n'
        'Combined data analysis skill.\n'
    )

    return str(tmp_path)


@pytest.fixture
def registry(temp_skills_dir):
    """Create a SkillRegistry with discovered skills."""
    reg = SkillRegistry(temp_skills_dir)
    reg.discover_skills()
    return reg


@pytest.fixture
def mock_tool():
    """Create a mock tool with _skill_name metadata."""
    def web_search_tool(query: str) -> str:
        return f"Results for: {query}"
    web_search_tool.tool_name = "web_search"
    web_search_tool._skill_name = "web-search"
    web_search_tool.tool_spec = {"name": "web_search", "description": "Search the web", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}
    return web_search_tool


@pytest.fixture
def viz_tool():
    """Create a mock visualization tool."""
    def create_chart(data: dict) -> str:
        return "chart created"
    create_chart.tool_name = "create_chart"
    create_chart._skill_name = "visualization"
    create_chart.tool_spec = {"name": "create_chart", "description": "Create a chart"}
    return create_chart
