"""Prompt-cache pin for skills: the bytes the model sees before it activates one.

Bedrock prompt caching is an exact-prefix match. A skill reaches the cacheable
prefix in three places, and none of them may change unless a PR means to
re-write every skilled session's prefix once:

- the ``<available_skills>`` block the ``AgentSkills`` plugin appends to the
  system prompt;
- the ``skills`` activation tool's spec, in ``toolConfig``;
- the ``read_skill_file`` tool's spec, in ``toolConfig``.

The golden values in ``golden/skills_prompt_pin.json`` were captured on
``develop`` before the agentskills.io validator and projection change, and every
later skills change must keep them byte-identical. If this fails, the change
re-writes the prompt cache: stop and decide that on purpose, do not regenerate
the golden file to make it pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
from strands import AgentSkills

from agents.main_agent.skills import strands_mapping as sm
from apis.shared.skills.models import SkillDefinition, SkillResourceRef

GOLDEN_PATH = Path(__file__).parent / "golden" / "skills_prompt_pin.json"


def _records() -> list[SkillDefinition]:
    """The fixed record set: plain, with allowed_tools, with files, with a license."""
    return [
        SkillDefinition(
            skill_id="plain_skill",
            display_name="Plain Skill",
            description="A skill with nothing but a description & <markup>.",
            instructions="# Plain\nDo the plain thing.",
        ),
        SkillDefinition(
            skill_id="tooled_skill",
            display_name="Tooled Skill",
            description="Names the tools it expects.",
            instructions="# Tooled\nUse the tools.",
            allowed_tools=["Read", "Bash(git:*)"],
        ),
        SkillDefinition(
            skill_id="files_skill",
            display_name="Files Skill",
            description="Carries reference files and a script.",
            instructions="# Files\nRead the references.",
            resources=[
                SkillResourceRef(
                    filename="forms.md",
                    content_hash="0" * 64,
                    size=12,
                    content_type="text/markdown",
                    s3_key="skills/files_skill/references/forms.md",
                    kind="reference",
                ),
                SkillResourceRef(
                    filename="fill.py",
                    content_hash="1" * 64,
                    size=34,
                    content_type="text/x-python",
                    s3_key="skills/files_skill/scripts/fill.py",
                    kind="script",
                ),
            ],
        ),
        SkillDefinition(
            skill_id="licensed_skill",
            display_name="Licensed Skill",
            description="Declares a license and no compatibility.",
            instructions="# Licensed\nFollow the license.",
            skill_metadata={"license": "Apache-2.0", "author": "example-org"},
        ),
    ]


class _NullStore:
    enabled = False

    def get(self, s3_key: str) -> bytes:  # pragma: no cover - never read here
        raise AssertionError("the pin never reads a file")


def capture() -> Dict[str, Any]:
    """The three cacheable values for the fixed record set."""
    records = sorted(_records(), key=lambda r: r.skill_id)
    plugin = AgentSkills(skills=[sm.record_to_strands_skill(r) for r in records])
    read_tool = sm.make_read_skill_file_tool(records)
    return {
        "available_skills_xml": plugin._generate_skills_xml(None),
        "skills_tool_spec": plugin.skills.tool_spec,
        "read_skill_file_tool_spec": read_tool.tool_spec,
    }


@pytest.fixture(autouse=True)
def _null_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "get_skill_resource_store", lambda: _NullStore())


@pytest.fixture(scope="module")
def golden() -> Dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "key", ["available_skills_xml", "skills_tool_spec", "read_skill_file_tool_spec"]
)
def test_skill_prompt_bytes_are_pinned(golden: Dict[str, Any], key: str) -> None:
    current = capture()[key]
    as_bytes = json.dumps(current, sort_keys=True, ensure_ascii=False).encode("utf-8")
    pinned = json.dumps(golden[key], sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert as_bytes == pinned, (
        f"{key} changed: this re-writes the cached prompt prefix of every session "
        "with a skill. Stop and decide it on purpose."
    )


def test_tool_spec_key_order_is_pinned(golden: Dict[str, Any]) -> None:
    """toolConfig is serialized in insertion order, so key order is bytes too."""
    current = capture()
    for key in ("skills_tool_spec", "read_skill_file_tool_spec"):
        assert json.dumps(current[key]) == json.dumps(golden[key])


def test_xml_lists_every_record_once(golden: Dict[str, Any]) -> None:
    xml = golden["available_skills_xml"]
    for name in ("plain-skill", "tooled-skill", "files-skill", "licensed-skill"):
        assert xml.count(f"<name>{name}</name>") == 1
