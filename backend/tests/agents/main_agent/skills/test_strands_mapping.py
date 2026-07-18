"""Skills v2 PR-2 — DB→Strands Skill mapping + ChatAgent plugin wiring."""

from types import SimpleNamespace

import pytest
from strands import AgentSkills, Skill

from agents.main_agent.skills import strands_mapping as sm


def _record(**kw):
    base = dict(
        skill_id="pdf_workflows",
        display_name="PDF Workflows",
        description="Work with PDF files.",
        instructions="# PDF\nExtract and fill PDFs.",
        allowed_tools=[],
        skill_metadata={},
        status="active",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    "skill_id,expected",
    [
        ("pdf_workflows", "pdf-workflows"),
        ("Web Research", "web-research"),
        ("already-slug", "already-slug"),
        ("UPPER_Snake_Case", "upper-snake-case"),
        ("weird__multi___underscore", "weird-multi-underscore"),
        ("-leading-and-trailing-", "leading-and-trailing"),
        ("!!!", "skill"),  # empties out → defensive fallback
    ],
)
def test_slugify_is_agentskills_valid(skill_id, expected):
    import re

    slug = sm.slugify_skill_name(skill_id)
    assert slug == expected
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", slug)


def test_record_to_strands_skill_maps_fields():
    skill = sm.record_to_strands_skill(
        _record(allowed_tools=["web_search"], skill_metadata={"license": "Apache-2.0"})
    )
    assert isinstance(skill, Skill)
    assert skill.name == "pdf-workflows"  # slug, not the human name
    assert skill.description == "Work with PDF files."
    assert skill.instructions.startswith("# PDF")
    assert skill.allowed_tools == ["web_search"]
    # true skill_id + human name recoverable from metadata; frontmatter passthrough kept
    assert skill.metadata["skill_id"] == "pdf_workflows"
    assert skill.metadata["display_name"] == "PDF Workflows"
    assert skill.metadata["license"] == "Apache-2.0"


def test_record_to_strands_skill_empty_allowed_tools_is_none():
    # Strands treats [] and None differently for the advisory field; empty → None.
    skill = sm.record_to_strands_skill(_record(allowed_tools=[]))
    assert skill.allowed_tools is None


def test_build_skills_plugin_none_when_empty():
    assert sm.build_skills_plugin(None) is None
    assert sm.build_skills_plugin([]) is None


def test_build_skills_plugin_builds_agentskills(monkeypatch):
    recs = [_record(skill_id="pdf_workflows"), _record(skill_id="doc_drafting", display_name="Doc")]
    monkeypatch.setattr(sm, "fetch_active_skill_records", lambda ids: recs)
    plugin = sm.build_skills_plugin(["pdf_workflows", "doc_drafting"])
    assert isinstance(plugin, AgentSkills)
    names = {s.name for s in plugin.get_available_skills()}
    assert names == {"pdf-workflows", "doc-drafting"}


def test_build_skills_plugin_none_when_no_active_records(monkeypatch):
    monkeypatch.setattr(sm, "fetch_active_skill_records", lambda ids: [])
    assert sm.build_skills_plugin(["gone"]) is None
