"""Tests for the agentskills.io bundle helpers (Skills v2)."""

import yaml

from apis.shared.skills.bundle import generate_skill_md, slugify_skill_name


def _parse(md: str):
    assert md.startswith("---\n")
    _, fm, body = md.split("---\n", 2)
    return yaml.safe_load(fm), body.strip()


class TestSlugify:
    def test_underscore_to_hyphen_lowercase(self):
        assert slugify_skill_name("PDF_Workflows") == "pdf-workflows"

    def test_collapses_and_trims_hyphens(self):
        assert slugify_skill_name("--weird__multi  space--") == "weird-multi-space"

    def test_empty_falls_back(self):
        assert slugify_skill_name("!!!") == "skill"


class TestGenerateSkillMd:
    def test_minimal(self):
        md = generate_skill_md(
            skill_id="pdf_workflows",
            description="Work with PDFs.",
            instructions="# PDF\nDo the thing.",
        )
        fm, body = _parse(md)
        assert fm == {"name": "pdf-workflows", "description": "Work with PDFs."}
        assert body == "# PDF\nDo the thing."

    def test_allowed_tools_only_when_present(self):
        without = generate_skill_md(
            skill_id="s", description="d", instructions="i", allowed_tools=[]
        )
        assert "allowed-tools" not in _parse(without)[0]
        with_tools = generate_skill_md(
            skill_id="s", description="d", instructions="i", allowed_tools=["web_search"]
        )
        assert _parse(with_tools)[0]["allowed-tools"] == ["web_search"]

    def test_metadata_passthrough_excludes_reserved(self):
        md = generate_skill_md(
            skill_id="s",
            description="d",
            instructions="i",
            skill_metadata={"license": "Apache-2.0", "name": "override-ignored"},
        )
        fm = _parse(md)[0]
        assert fm["license"] == "Apache-2.0"
        # The projection owns `name`; a metadata `name` never overrides the slug.
        assert fm["name"] == "s"

    def test_round_trips_as_valid_frontmatter(self):
        md = generate_skill_md(
            skill_id="my_skill",
            description="Use this when: the user asks. Colons: allowed.",
            instructions="body",
            allowed_tools=["a", "b"],
        )
        fm, _ = _parse(md)
        assert fm["name"] == "my-skill"
        assert fm["description"].startswith("Use this when")
