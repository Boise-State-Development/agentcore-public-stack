"""Tests for SkillRegistry — discovery, binding, and three-level access."""

from types import SimpleNamespace

import pytest
from agents.main_agent.skills.skill_registry import SkillRegistry


class TestDiscovery:
    """Req SK-1: Skill discovery from SKILL.md files."""

    def test_discovers_all_skills(self, registry):
        assert registry.get_skill_count() == 3

    def test_discovers_skill_names(self, registry):
        names = sorted(registry.get_skill_names())
        assert names == ["data-analysis", "visualization", "web-search"]

    def test_empty_directory_returns_zero(self, tmp_path):
        reg = SkillRegistry(str(tmp_path))
        assert reg.discover_skills() == 0

    def test_nonexistent_directory_returns_zero(self):
        reg = SkillRegistry("/nonexistent/path")
        assert reg.discover_skills() == 0

    def test_has_skill_true(self, registry):
        assert registry.has_skill("web-search") is True

    def test_has_skill_false(self, registry):
        assert registry.has_skill("nonexistent") is False


class TestFrontmatterParsing:
    """Req SK-2: YAML frontmatter parsing without PyYAML."""

    def test_parses_name(self, registry):
        assert registry.has_skill("web-search")

    def test_parses_composite_compose_list(self, registry):
        skills = registry._skills
        assert "data-analysis" in skills
        assert skills["data-analysis"]["compose"] == ["web-search", "visualization"]

    def test_parses_type(self, registry):
        assert registry._skills["web-search"]["type"] == "tool"
        assert registry._skills["data-analysis"]["type"] == "composite"

    def test_parses_description(self, registry):
        assert registry._skills["web-search"]["description"] == "Search the web for current information"

    def test_no_frontmatter_returns_empty(self):
        result = SkillRegistry._parse_frontmatter("Just plain markdown")
        assert result == {}


class TestToolBinding:
    """Req SK-3: Tool binding via _skill_name metadata."""

    def test_binds_tool_to_skill(self, registry, mock_tool):
        bound = registry.bind_tools([mock_tool])
        assert bound == 1
        assert len(registry.get_tools("web-search")) == 1

    def test_ignores_tool_without_skill_name(self, registry):
        def orphan_tool(): pass
        bound = registry.bind_tools([orphan_tool])
        assert bound == 0

    def test_ignores_tool_with_unknown_skill(self, registry):
        def stray_tool(): pass
        stray_tool._skill_name = "nonexistent-skill"
        bound = registry.bind_tools([stray_tool])
        assert bound == 0

    def test_binds_multiple_tools(self, registry, mock_tool, viz_tool):
        bound = registry.bind_tools([mock_tool, viz_tool])
        assert bound == 2


class TestCatalog:
    """Req SK-4: Level 1 — catalog generation for system prompt."""

    def test_catalog_contains_skill_names(self, registry):
        catalog = registry.get_catalog()
        assert "web-search" in catalog
        assert "visualization" in catalog
        assert "data-analysis" in catalog

    def test_catalog_contains_descriptions(self, registry):
        catalog = registry.get_catalog()
        assert "Search the web for current information" in catalog

    def test_catalog_shows_composite_combines(self, registry):
        catalog = registry.get_catalog()
        assert "combines:" in catalog

    def test_empty_registry_returns_empty_string(self):
        reg = SkillRegistry("/nonexistent")
        assert reg.get_catalog() == ""

    def test_catalog_shows_tool_count(self, registry, mock_tool):
        registry.bind_tools([mock_tool])
        catalog = registry.get_catalog()
        assert "(1 tools)" in catalog


class TestInstructions:
    """Req SK-5: Level 2 — instructions loading."""

    def test_loads_instructions_without_frontmatter(self, registry):
        instructions = registry.load_instructions("web-search")
        assert instructions is not None
        assert "# Web Search" in instructions
        assert "---" not in instructions
        assert "name:" not in instructions

    def test_unknown_skill_returns_none(self, registry):
        assert registry.load_instructions("nonexistent") is None


class TestTools:
    """Req SK-6: Level 3 — tool access."""

    def test_get_tools_returns_bound_tools(self, registry, mock_tool):
        registry.bind_tools([mock_tool])
        tools = registry.get_tools("web-search")
        assert len(tools) == 1
        assert tools[0].tool_name == "web_search"

    def test_composite_skill_aggregates_tools(self, registry, mock_tool, viz_tool):
        registry.bind_tools([mock_tool, viz_tool])
        tools = registry.get_tools("data-analysis")
        assert len(tools) == 2

    def test_unknown_skill_returns_empty(self, registry):
        assert registry.get_tools("nonexistent") == []

    def test_get_tool_schemas(self, registry, mock_tool):
        registry.bind_tools([mock_tool])
        schemas = registry.get_tool_schemas("web-search")
        assert len(schemas) == 1
        assert schemas[0]["name"] == "web_search"


class _Rec:
    """Minimal SkillDefinition stand-in (duck-typed by load_records)."""

    def __init__(self, skill_id, description="", instructions="", compose=None,
                 bound_tool_ids=None, resources=None, status="active"):
        self.skill_id = skill_id
        self.description = description
        self.instructions = instructions
        self.compose = compose or []
        self.bound_tool_ids = bound_tool_ids or []
        self.resources = resources or []
        self.status = status


class TestDbSource:
    """Req SK-6 (PR-6): DynamoDB-backed skill records (admin-managed)."""

    def test_load_records_populates_registry(self):
        reg = SkillRegistry()
        n = reg.load_records([
            _Rec("pdf_workflows", description="PDFs", instructions="# PDF body",
                 bound_tool_ids=["fill_pdf_form"]),
            _Rec("doc_basics", description="Docs"),
        ])
        assert n == 2
        assert reg.get_skill_count() == 2
        assert reg.has_skill("pdf_workflows")
        # Catalog lists the DB skills by skill_id + description.
        catalog = reg.get_catalog()
        assert "pdf_workflows" in catalog and "PDFs" in catalog

    def test_load_instructions_returns_inline_body(self):
        reg = SkillRegistry()
        reg.load_records([_Rec("pdf_workflows", instructions="# Inline body")])
        # DB mode: instructions come from the record, not a file.
        assert reg.load_instructions("pdf_workflows") == "# Inline body"

    def test_all_bound_tool_ids_unions_and_dedupes(self):
        reg = SkillRegistry()
        reg.load_records([
            _Rec("a", bound_tool_ids=["t1", "t2"]),
            _Rec("b", bound_tool_ids=["t2", "t3"]),
        ])
        assert sorted(reg.all_bound_tool_ids()) == ["t1", "t2", "t3"]

    def test_bind_catalog_tools_binds_matching_ids_only(self):
        reg = SkillRegistry()
        reg.load_records([_Rec("a", bound_tool_ids=["local_tool", "gateway_x"])])

        def local_tool():
            return "ok"
        local_tool.tool_name = "local_tool"

        # Only the resolvable (local) id is in the map; the gateway id is not.
        reg.bind_catalog_tools({"local_tool": local_tool})
        tools = reg.get_tools("a")
        assert tools == [local_tool]
        # Schema reflects the bound local tool.
        assert reg.get_tool_schemas("a")[0]["name"] == "local_tool"

    def test_bind_catalog_tools_is_idempotent(self):
        reg = SkillRegistry()
        reg.load_records([_Rec("a", bound_tool_ids=["t"])])

        def t():
            return 1
        t.tool_name = "t"

        reg.bind_catalog_tools({"t": t})
        reg.bind_catalog_tools({"t": t})
        assert reg.get_tools("a") == [t]  # not double-bound

    def test_db_composite_aggregates_child_tools(self):
        reg = SkillRegistry()
        reg.load_records([
            _Rec("child", bound_tool_ids=["t"]),
            _Rec("parent", compose=["child"]),
        ])

        def t():
            return 1
        t.tool_name = "t"

        reg.bind_catalog_tools({"t": t})
        assert reg.get_tools("parent") == [t]

    def test_bind_catalog_tools_accepts_list_value(self):
        # One catalog id can expand to several runtime tools (gateway target /
        # external server with many) — bind_catalog_tools accepts a list.
        reg = SkillRegistry()
        reg.load_records([_Rec("a", bound_tool_ids=["server"])])

        t1 = SimpleNamespace(tool_name="search")
        t2 = SimpleNamespace(tool_name="fetch")
        reg.bind_catalog_tools({"server": [t1, t2]})

        tools = reg.get_tools("a")
        assert tools == [t1, t2]

    def test_bind_catalog_tools_list_is_idempotent(self):
        reg = SkillRegistry()
        reg.load_records([_Rec("a", bound_tool_ids=["server"])])
        t1 = SimpleNamespace(tool_name="search")
        reg.bind_catalog_tools({"server": [t1]})
        reg.bind_catalog_tools({"server": [t1]})
        assert reg.get_tools("a") == [t1]


def _ref(filename, s3_key="k", content_type="text/markdown", size=10):
    return {
        "filename": filename,
        "s3_key": s3_key,
        "content_type": content_type,
        "size": size,
    }


class _FakeStore:
    """Stand-in for SkillResourceStore keyed by s3_key."""

    def __init__(self, data):
        self._data = data

    def get(self, s3_key):
        if s3_key not in self._data:
            from apis.shared.skills.resource_store import SkillResourceStoreError

            raise SkillResourceStoreError(f"missing {s3_key}")
        return self._data[s3_key]


class TestReferenceFiles:
    """Req (PR-6b): reference-file progressive disclosure level."""

    def test_get_resource_names(self):
        reg = SkillRegistry()
        reg.load_records([
            _Rec("a", resources=[_ref("forms.md", "k1"), _ref("reference.md", "k2")])
        ])
        assert reg.get_resource_names("a") == ["forms.md", "reference.md"]

    def test_get_resource_names_empty_when_none(self):
        reg = SkillRegistry()
        reg.load_records([_Rec("a")])
        assert reg.get_resource_names("a") == []

    def test_read_resource_returns_text(self):
        reg = SkillRegistry()
        reg.load_records([_Rec("a", resources=[_ref("forms.md", "k1")])])
        store = _FakeStore({"k1": b"# Forms\nfill them in"})
        out = reg.read_resource("a", "forms.md", store=store)
        assert out["content"] == "# Forms\nfill them in"
        assert out["filename"] == "forms.md"

    def test_read_resource_unknown_skill_returns_none(self):
        reg = SkillRegistry()
        assert reg.read_resource("nope", "x.md", store=_FakeStore({})) is None

    def test_read_resource_missing_file_errors(self):
        reg = SkillRegistry()
        reg.load_records([_Rec("a", resources=[_ref("forms.md", "k1")])])
        out = reg.read_resource("a", "absent.md", store=_FakeStore({}))
        assert "error" in out

    def test_read_resource_storage_error_is_error_dict(self):
        reg = SkillRegistry()
        reg.load_records([_Rec("a", resources=[_ref("forms.md", "k1")])])
        # Store has no k1 → raises SkillResourceStoreError → error dict.
        out = reg.read_resource("a", "forms.md", store=_FakeStore({}))
        assert "error" in out

    def test_read_resource_binary_is_noted_not_dumped(self):
        reg = SkillRegistry()
        reg.load_records([_Rec("a", resources=[_ref("logo.png", "k1", "image/png")])])
        store = _FakeStore({"k1": b"\x89PNG\x00\xff"})
        out = reg.read_resource("a", "logo.png", store=store)
        assert "content" not in out
        assert "note" in out
        assert out["content_type"] == "image/png"
