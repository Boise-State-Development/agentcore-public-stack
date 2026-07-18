"""Skills runtime (Skills v2).

Skills are pure knowledge bundles (agentskills.io format) disclosed at runtime
by the vended Strands ``AgentSkills`` plugin — metadata (name + description) is
injected into the system prompt, and full instructions load on demand via the
plugin's ``skills`` activation tool. This package holds the adapter that maps
our DynamoDB ``SkillDefinition`` rows into programmatic ``strands.Skill``
instances and builds the plugin.

The homegrown progressive-disclosure engine (``SkillRegistry`` +
``skill_dispatcher``/``skill_executor`` meta-tools + ``SkillAgent``) and the
tool-binding machinery it served are removed — the plugin implements the same
standard natively and skills no longer bind tools.
"""

from .strands_mapping import (
    build_skills_plugin,
    build_skills_runtime,
    fetch_active_skill_records,
    make_read_skill_file_tool,
    record_to_strands_skill,
    slugify_skill_name,
)

__all__ = [
    "build_skills_plugin",
    "build_skills_runtime",
    "fetch_active_skill_records",
    "make_read_skill_file_tool",
    "record_to_strands_skill",
    "slugify_skill_name",
]
