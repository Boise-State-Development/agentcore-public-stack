"""
Progressive Skill Disclosure System

Provides a three-level disclosure architecture for managing tool complexity:
- Level 1 (Catalog): Skill names + descriptions injected into system prompt
- Level 2 (Instructions): Full SKILL.md loaded on-demand via skill_dispatcher
- Level 3 (Execution): Tool invocation via skill_executor

This approach is dramatically more token-efficient than loading all tool schemas
upfront, especially as the number of tools grows.
"""

from .decorators import skill, register_skill
from .skill_registry import SkillRegistry
from .skill_tools import skill_dispatcher, skill_executor, set_dispatcher_registry

__all__ = [
    "skill",
    "register_skill",
    "SkillRegistry",
    "skill_dispatcher",
    "skill_executor",
    "set_dispatcher_registry",
]
