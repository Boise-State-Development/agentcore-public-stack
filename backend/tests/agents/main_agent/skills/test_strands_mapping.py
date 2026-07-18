"""Skills v2 PR-2 — DB→Strands Skill mapping + ChatAgent plugin wiring."""

from types import SimpleNamespace

import pytest
from strands import AgentSkills, Skill

from agents.main_agent.skills import strands_mapping as sm


def _ref(filename, *, kind="reference", content_type="text/markdown", size=10):
    subdir = {"reference": "references", "script": "scripts", "asset": "assets"}[kind]
    return SimpleNamespace(
        filename=filename,
        kind=kind,
        content_type=content_type,
        size=size,
        s3_key=f"skills/pdf_workflows/{subdir}/{filename}",
    )


def _record(**kw):
    base = dict(
        skill_id="pdf_workflows",
        display_name="PDF Workflows",
        description="Work with PDF files.",
        instructions="# PDF\nExtract and fill PDFs.",
        allowed_tools=[],
        skill_metadata={},
        resources=[],
        status="active",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeStore:
    """Stand-in for SkillResourceStore keyed by s3_key → bytes."""

    def __init__(self, objects=None, raises=False):
        self._objects = objects or {}
        self._raises = raises

    def get(self, s3_key):
        if self._raises:
            raise sm.SkillResourceStoreError("disabled")
        return self._objects[s3_key]


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


def test_instructions_gain_reference_file_listing():
    rec = _record(resources=[_ref("forms.md"), _ref("run.py", kind="script")])
    skill = sm.record_to_strands_skill(rec)
    assert "## Available reference files" in skill.instructions
    assert "references/forms.md" in skill.instructions
    assert "scripts/run.py" in skill.instructions
    # No listing when there are no resources.
    assert "Available reference files" not in sm.record_to_strands_skill(
        _record()
    ).instructions


class TestBuildSkillsRuntime:
    def test_returns_none_none_when_empty(self):
        assert sm.build_skills_runtime(None) == (None, None)
        assert sm.build_skills_runtime([]) == (None, None)

    def test_returns_plugin_and_tool(self, monkeypatch):
        monkeypatch.setattr(sm, "fetch_active_skill_records", lambda ids: [_record()])
        plugin, read_tool = sm.build_skills_runtime(["pdf_workflows"])
        assert isinstance(plugin, AgentSkills)
        assert read_tool.tool_name == "read_skill_file"


class TestReadSkillFile:
    def _tool(self, monkeypatch, records, store):
        monkeypatch.setattr(sm, "get_skill_resource_store", lambda: store)
        return sm.make_read_skill_file_tool(records)

    def test_reads_reference_text(self, monkeypatch):
        rec = _record(resources=[_ref("forms.md")])
        store = _FakeStore({"skills/pdf_workflows/references/forms.md": b"# Forms body"})
        read = self._tool(monkeypatch, [rec], store)
        # Resolvable by bare filename or by standard bundle path.
        assert read(skill_name="pdf-workflows", path="forms.md") == "# Forms body"
        assert (
            read(skill_name="pdf-workflows", path="references/forms.md")
            == "# Forms body"
        )

    def test_script_is_inert_labeled(self, monkeypatch):
        rec = _record(resources=[_ref("run.py", kind="script", content_type="text/x-python")])
        store = _FakeStore({"skills/pdf_workflows/scripts/run.py": b"print('hi')"})
        read = self._tool(monkeypatch, [rec], store)
        out = read(skill_name="pdf-workflows", path="scripts/run.py")
        assert "NOT" in out and "executable" in out
        assert "print('hi')" in out

    def test_binary_asset_is_described_not_dumped(self, monkeypatch):
        rec = _record(resources=[_ref("logo.png", kind="asset", content_type="image/png", size=2048)])
        store = _FakeStore({"skills/pdf_workflows/assets/logo.png": b"\x89PNG..."})
        read = self._tool(monkeypatch, [rec], store)
        out = read(skill_name="pdf-workflows", path="assets/logo.png")
        assert "binary" in out and "image/png" in out
        assert "\x89PNG" not in out

    def test_unknown_skill_rejected(self, monkeypatch):
        read = self._tool(monkeypatch, [_record()], _FakeStore())
        out = read(skill_name="not-a-skill", path="forms.md")
        assert "not available" in out

    def test_missing_file_lists_available(self, monkeypatch):
        rec = _record(resources=[_ref("forms.md")])
        read = self._tool(monkeypatch, [rec], _FakeStore())
        out = read(skill_name="pdf-workflows", path="ghost.md")
        assert "No reference file" in out and "references/forms.md" in out

    def test_store_error_is_handled(self, monkeypatch):
        rec = _record(resources=[_ref("forms.md")])
        read = self._tool(monkeypatch, [rec], _FakeStore(raises=True))
        out = read(skill_name="pdf-workflows", path="forms.md")
        assert "Could not read" in out
