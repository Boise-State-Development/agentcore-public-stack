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


def test_instructions_gain_bundled_files_listing():
    rec = _record(resources=[_ref("forms.md"), _ref("run.py", kind="script")])
    skill = sm.record_to_strands_skill(rec)
    assert skill.instructions.startswith("# PDF\nExtract and fill PDFs.\n\n## Bundled files\n")
    assert "references/forms.md" in skill.instructions
    assert "scripts/run.py" in skill.instructions
    # No listing when there are no resources.
    assert "Bundled files" not in sm.record_to_strands_skill(_record()).instructions


def test_bundled_files_grouped_by_kind_in_fixed_order():
    rec = _record(
        resources=[
            _ref("logo.png", kind="asset", content_type="image/png"),
            _ref("run.py", kind="script"),
            _ref("forms.md"),
            _ref("fill.py", kind="script"),
            _ref("guide.md"),
        ]
    )
    assert sm._reference_files_section(rec) == (
        "\n\n## Bundled files\n"
        "Load any of these with the `read_skill_file` tool "
        "(skill_name `pdf-workflows`, path = one of the entries below):\n\n"
        "### References\n- references/forms.md\n- references/guide.md\n\n"
        "### Scripts\n- scripts/run.py\n- scripts/fill.py\n\n"
        "### Assets\n- assets/logo.png"
    )


def test_bundled_files_only_references():
    rec = _record(resources=[_ref("forms.md")])
    assert sm._reference_files_section(rec) == (
        "\n\n## Bundled files\n"
        "Load any of these with the `read_skill_file` tool "
        "(skill_name `pdf-workflows`, path = one of the entries below):\n\n"
        "### References\n- references/forms.md"
    )


def test_bundled_files_unknown_kind_listed_under_references():
    odd = SimpleNamespace(filename="notes.md", kind="other", content_type="text/markdown", size=1, s3_key="k")
    section = sm._reference_files_section(_record(resources=[odd]))
    assert "### References\n- references/notes.md" in section
    assert "### Scripts" not in section and "### Assets" not in section


def _nested(path, *, kind="script"):
    ref = _ref(path.rsplit("/", 1)[-1], kind=kind, content_type="text/plain")
    ref.path = path
    return ref


def test_xml_schemas_collapse_to_one_line_where_the_first_sits():
    """anthropics/skills' Office bundles: 39 schemas the scripts read from disk."""
    rec = _record(
        resources=[
            _nested("office/validate.py"),
            _nested("office/schemas/ISO-IEC29500-4_2016/sml.xsd"),
            _nested("office/schemas/ecma/fouth-edition/opc-relationships.xsd"),
            _nested("office/schemas/microsoft/wml-2010.xsd"),
            _nested("recalc.py"),
        ]
    )
    assert "### Scripts\n" + "\n".join(
        [
            "- scripts/office/validate.py",
            "- scripts/office/schemas/ holds 3 XML schemas (.xsd) that the scripts read; not listed one by one",
            "- scripts/recalc.py",
        ]
    ) in sm._reference_files_section(rec)


def test_a_lone_schema_keeps_its_own_line():
    rec = _record(resources=[_nested("check.py"), _nested("schema.xsd")])
    assert "### Scripts\n- scripts/check.py\n- scripts/schema.xsd" in sm._reference_files_section(rec)


def test_schemas_still_resolve_by_path_when_not_listed():
    """Collapsing is display only: every file stays in the manifest and readable."""
    rec = _record(resources=[_nested("office/schemas/a.xsd"), _nested("office/schemas/b.xsd")])
    assert sm._manifest_paths(rec) == ["scripts/office/schemas/a.xsd", "scripts/office/schemas/b.xsd"]


def test_bundled_files_nothing_for_a_skill_with_no_files():
    assert sm._reference_files_section(_record()) == ""
    assert sm._reference_files_section(_record(resources=None)) == ""


class TestBuildSkillsRuntime:
    def test_returns_none_none_when_empty(self):
        assert sm.build_skills_runtime(None) == (None, None, [])
        assert sm.build_skills_runtime([]) == (None, None, [])

    def test_returns_plugin_and_tool(self, monkeypatch):
        monkeypatch.setattr(sm, "fetch_active_skill_records", lambda ids: [_record()])
        plugin, read_tool, _records = sm.build_skills_runtime(["pdf_workflows"])
        assert isinstance(plugin, AgentSkills)
        assert read_tool.tool_name == "read_skill_file"

    def test_prompt_order_stable_across_fetch_order(self, monkeypatch):
        # The plugin renders <available_skills> in the order it receives the
        # skills; a between-turn order flip changes the system prompt and
        # invalidates the Bedrock prompt cache. Regardless of the order the
        # fetch returns records, the runtime must produce one canonical order.
        recs = [
            _record(skill_id="zeta_skill"),
            _record(skill_id="alpha_skill"),
            _record(skill_id="midway_skill"),
        ]

        def names_for(fetch_order):
            monkeypatch.setattr(
                sm, "fetch_active_skill_records", lambda ids: list(fetch_order)
            )
            plugin, _, _records = sm.build_skills_runtime(["any"])
            return [s.name for s in plugin.get_available_skills()]

        forward = names_for(recs)
        reversed_order = names_for(list(reversed(recs)))
        assert forward == reversed_order == ["alpha-skill", "midway-skill", "zeta-skill"]


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

    def test_an_imported_bundles_own_link_resolves_by_basename(self, monkeypatch):
        """A bundle's SKILL.md keeps links like ``reference/eval.md`` while the
        manifest is flat (skill-bundle-import.md §4); a unique basename names it."""
        rec = _record(resources=[_ref("eval.md"), _ref("pack.py", kind="script")])
        store = _FakeStore(
            {
                "skills/pdf_workflows/references/eval.md": b"# Eval",
                "skills/pdf_workflows/scripts/pack.py": b"print(1)",
            }
        )
        read = self._tool(monkeypatch, [rec], store)
        assert read(skill_name="pdf-workflows", path="reference/eval.md") == "# Eval"
        assert "print(1)" in read(skill_name="pdf-workflows", path="scripts/office/pack.py")

    def test_a_traversing_path_never_falls_back(self, monkeypatch):
        rec = _record(resources=[_ref("eval.md")])
        read = self._tool(monkeypatch, [rec], _FakeStore({"skills/pdf_workflows/references/eval.md": b"x"}))
        assert "No reference file" in read(skill_name="pdf-workflows", path="../other-skill/eval.md")

    def test_script_execution_keeps_the_strict_lookup(self):
        rec = _record(resources=[_ref("engine.py", kind="script")])
        assert sm._resolve_resource(rec, "scripts/office/engine.py") is None
        assert sm._resolve_readable_resource(rec, "scripts/office/engine.py") is not None

    def test_an_ambiguous_basename_does_not_guess(self, monkeypatch):
        rec = _record(resources=[_ref("a.md"), _ref("a.md", kind="asset")])
        read = self._tool(monkeypatch, [rec], _FakeStore())
        assert "No reference file" in read(skill_name="pdf-workflows", path="other/a.md")

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


def test_frontmatter_metadata_cannot_replace_the_true_id_or_name():
    skill = sm.record_to_strands_skill(
        _record(skill_metadata={"skill_id": "evil", "display_name": "x", "license": "MIT"})
    )
    assert skill.metadata["skill_id"] == "pdf_workflows"
    assert skill.metadata["display_name"] == "PDF Workflows"
    assert skill.metadata["license"] == "MIT"
    assert skill.name == "pdf-workflows"


# =============================================================================
# license / compatibility → Strands Skill, and the activation text
# =============================================================================


def _activate(record):
    import asyncio

    plugin = AgentSkills(skills=[sm.record_to_strands_skill(record)])
    skill = plugin.get_available_skills()[0]
    return asyncio.run(plugin._format_skill_response(skill, None))


def test_license_and_compatibility_reach_the_strands_skill():
    skill = sm.record_to_strands_skill(
        _record(skill_metadata={"license": "Apache-2.0", "compatibility": "Requires uv"})
    )
    assert skill.license == "Apache-2.0"
    assert skill.compatibility == "Requires uv"


@pytest.mark.parametrize("value", [None, "", 3, ["uv"], {"a": "b"}])
def test_a_non_string_license_or_compatibility_is_left_out(value):
    skill = sm.record_to_strands_skill(
        _record(skill_metadata={"license": value, "compatibility": value})
    )
    assert skill.license is None and skill.compatibility is None


def test_activation_shows_a_compatibility_line_for_a_skill_that_declares_one():
    text = _activate(_record(skill_metadata={"compatibility": "Requires uv and network"}))
    assert text == "# PDF\nExtract and fill PDFs.\n\n---\nCompatibility: Requires uv and network"


def test_activation_without_compatibility_is_exactly_todays_text():
    # A license (shown nowhere) and other metadata change nothing either.
    assert _activate(_record()) == "# PDF\nExtract and fill PDFs."
    assert (
        _activate(_record(skill_metadata={"license": "MIT", "version": "1"}))
        == "# PDF\nExtract and fill PDFs."
    )
    assert (
        _activate(_record(allowed_tools=["Read"]))
        == "# PDF\nExtract and fill PDFs.\n\n---\nAllowed tools: Read"
    )


def test_a_skill_with_both_lists_tools_then_compatibility():
    text = _activate(
        _record(allowed_tools=["Read"], skill_metadata={"compatibility": "Needs git"})
    )
    assert text.endswith("\n---\nAllowed tools: Read\nCompatibility: Needs git")
