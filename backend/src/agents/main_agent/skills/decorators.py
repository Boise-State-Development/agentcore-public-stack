"""
Skill decorators for tagging tools with skill membership.

Usage:
    @skill("web-search")
    @tool
    def ddg_web_search(...): ...

    # Or batch registration:
    register_skill("visualization", tools=[create_chart, create_graph])
"""

from typing import List, Any


def _apply_skill_metadata(tool_obj: Any, name: str) -> None:
    """Attach skill name metadata to a tool object."""
    tool_obj._skill_name = name


def skill(name: str):
    """
    Decorator that tags a tool function with a skill name.

    The SkillRegistry uses the _skill_name attribute to bind tools
    to their parent skill during discovery.

    Args:
        name: Skill identifier (must match a SKILL.md directory name)
    """
    def decorator(func):
        _apply_skill_metadata(func, name)
        return func
    return decorator


def register_skill(name: str, tools: List[Any]) -> None:
    """
    Batch-register multiple tools under a single skill name.

    Args:
        name: Skill identifier
        tools: List of tool objects to tag
    """
    for tool_obj in tools:
        _apply_skill_metadata(tool_obj, name)
