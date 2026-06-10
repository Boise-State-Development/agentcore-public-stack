"""Tests for skill_dispatcher and skill_executor tools."""

import json
import pytest

from agents.main_agent.skills.skill_registry import SkillRegistry
from agents.main_agent.skills.skill_tools import (
    make_skill_tools,
    skill_dispatcher,
    skill_executor,
    set_dispatcher_registry,
)


class _DbRec:
    """Minimal SkillDefinition stand-in for DB-mode registries."""

    def __init__(self, skill_id, instructions="# body", bound_tool_ids=None,
                 resources=None):
        self.skill_id = skill_id
        self.description = f"desc {skill_id}"
        self.instructions = instructions
        self.compose = []
        self.bound_tool_ids = bound_tool_ids or []
        self.resources = resources or []
        self.status = "active"


class _FakeStore:
    def __init__(self, data):
        self._data = data

    def get(self, s3_key):
        return self._data[s3_key]


@pytest.fixture(autouse=True)
def setup_registry(registry, mock_tool, viz_tool):
    """Wire up registry with tools for all tests in this module."""
    registry.bind_tools([mock_tool, viz_tool])
    set_dispatcher_registry(registry)
    yield
    set_dispatcher_registry(None)


class TestSkillDispatcher:
    """Req SD-1: skill_dispatcher loads instructions and schemas."""

    def test_returns_instructions(self, registry):
        result = json.loads(skill_dispatcher(skill_name="web-search"))
        assert "instructions" in result
        assert "# Web Search" in result["instructions"]

    def test_returns_tool_schemas(self, registry):
        result = json.loads(skill_dispatcher(skill_name="web-search"))
        assert "tool_schemas" in result
        assert len(result["tool_schemas"]) == 1
        assert result["tool_schemas"][0]["name"] == "web_search"

    def test_unknown_skill_returns_error(self):
        result = json.loads(skill_dispatcher(skill_name="nonexistent"))
        assert "error" in result
        assert "Unknown skill" in result["error"]

    def test_unknown_skill_lists_available(self):
        result = json.loads(skill_dispatcher(skill_name="nonexistent"))
        assert "available_skills" in result

    def test_no_registry_returns_error(self):
        set_dispatcher_registry(None)
        result = json.loads(skill_dispatcher(skill_name="web-search"))
        assert "error" in result
        assert "not initialized" in result["error"]


class TestSkillExecutor:
    """Req SE-1: skill_executor runs tools within a skill."""

    def test_executes_tool(self):
        result = skill_executor(
            skill_name="web-search",
            tool_name="web_search",
            tool_input={"query": "test search"}
        )
        assert result == "Results for: test search"

    def test_json_string_input_parsed(self):
        result = skill_executor(
            skill_name="web-search",
            tool_name="web_search",
            tool_input='{"query": "parsed"}'
        )
        assert result == "Results for: parsed"

    def test_unknown_skill_returns_error(self):
        result = json.loads(skill_executor(
            skill_name="nonexistent",
            tool_name="anything",
        ))
        assert "error" in result

    def test_unknown_tool_returns_error_with_available(self):
        result = json.loads(skill_executor(
            skill_name="web-search",
            tool_name="nonexistent_tool",
        ))
        assert "error" in result
        assert "available_tools" in result

    def test_no_registry_returns_error(self):
        set_dispatcher_registry(None)
        result = json.loads(skill_executor(
            skill_name="web-search",
            tool_name="web_search",
        ))
        assert "error" in result


class TestReferenceDisclosure:
    """Req (PR-6b): skill_dispatcher serves reference files on demand."""

    def _registry(self):
        reg = SkillRegistry()
        reg.load_records([
            _DbRec(
                "research",
                instructions="# Research\nSee forms.md",
                resources=[{
                    "filename": "forms.md",
                    "s3_key": "k1",
                    "content_type": "text/markdown",
                    "size": 7,
                }],
            )
        ])
        return reg

    def test_dispatch_lists_available_references(self):
        dispatch, _ = make_skill_tools(self._registry())
        result = json.loads(dispatch(skill_name="research"))
        assert result["available_references"] == ["forms.md"]
        assert "reference_hint" in result

    def test_dispatch_reads_reference(self, monkeypatch):
        import apis.shared.skills.resource_store as rs

        monkeypatch.setattr(
            rs, "get_skill_resource_store",
            lambda: _FakeStore({"k1": b"# Forms\nfill"}),
        )
        dispatch, _ = make_skill_tools(self._registry())
        result = json.loads(dispatch(skill_name="research", reference="forms.md"))
        assert result["content"] == "# Forms\nfill"
        assert result["filename"] == "forms.md"

    def test_dispatch_unknown_reference_lists_available(self):
        dispatch, _ = make_skill_tools(self._registry())
        result = json.loads(dispatch(skill_name="research", reference="absent.md"))
        assert "error" in result
        assert result["available_references"] == ["forms.md"]


class TestFoldedMCPExecutor:
    """Req (PR-6b): skill_executor runs a folded gateway/external MCP tool
    through the MCP client (covers the gateway + external execution path)."""

    def test_executor_routes_through_client(self):
        from agents.main_agent.skills.mcp_binding import FoldedMCPTool

        class _Client:
            def __init__(self):
                self.calls = []

            def call_tool_sync(self, tool_use_id, name, arguments=None, **k):
                self.calls.append((name, arguments))
                return {"content": [{"text": "folded result"}]}

        client = _Client()
        reg = SkillRegistry()
        reg.load_records([_DbRec("g", bound_tool_ids=["gateway_x"])])
        folded = FoldedMCPTool(client, mcp_tool_name="target___tool")
        reg.bind_catalog_tools({"gateway_x": [folded]})

        _, execute = make_skill_tools(reg)
        out = execute(
            skill_name="g", tool_name="target___tool", tool_input={"q": "hi"}
        )
        assert out == "folded result"
        assert client.calls[0] == ("target___tool", {"q": "hi"})

    def test_dispatch_shows_folded_tool_schema(self):
        from agents.main_agent.skills.mcp_binding import FoldedMCPTool

        reg = SkillRegistry()
        reg.load_records([_DbRec("g", bound_tool_ids=["gateway_x"])])
        spec = {"name": "target___tool", "inputSchema": {"json": {}}}
        folded = FoldedMCPTool(None, mcp_tool_name="target___tool", tool_spec=spec)
        reg.bind_catalog_tools({"gateway_x": [folded]})

        dispatch, _ = make_skill_tools(reg)
        result = json.loads(dispatch(skill_name="g"))
        assert result["tool_schemas"][0]["name"] == "target___tool"


class TestDecorators:
    """Req SD-2: Skill decorators apply metadata."""

    def test_skill_decorator_sets_attribute(self):
        from agents.main_agent.skills.decorators import skill

        @skill("my-skill")
        def my_tool():
            pass

        assert my_tool._skill_name == "my-skill"

    def test_register_skill_sets_attributes(self):
        from agents.main_agent.skills.decorators import register_skill

        def tool_a(): pass
        def tool_b(): pass

        register_skill("batch-skill", tools=[tool_a, tool_b])
        assert tool_a._skill_name == "batch-skill"
        assert tool_b._skill_name == "batch-skill"


class TestMakeSkillTools:
    """Req SD-3 (PR-6): per-agent meta-tools bound to their own registry.

    The concurrency fix: two agents serving different users must not share
    skill state through a process-global registry.
    """

    def _registry_with(self, skill_id):
        from agents.main_agent.skills.skill_registry import SkillRegistry

        reg = SkillRegistry()

        class _Rec:
            def __init__(self, sid):
                self.skill_id = sid
                self.description = f"desc {sid}"
                self.instructions = f"# {sid} body"
                self.compose = []
                self.bound_tool_ids = []
                self.resources = []
                self.status = "active"

        reg.load_records([_Rec(skill_id)])
        return reg

    def test_each_pair_resolves_against_its_own_registry(self):
        from agents.main_agent.skills.skill_tools import (
            make_skill_tools,
            set_dispatcher_registry,
        )

        # A stray global registry must NOT leak into the closures.
        set_dispatcher_registry(None)

        reg_a = self._registry_with("skill_a")
        reg_b = self._registry_with("skill_b")
        dispatch_a, _ = make_skill_tools(reg_a)
        dispatch_b, _ = make_skill_tools(reg_b)

        a = json.loads(dispatch_a(skill_name="skill_a"))
        assert "# skill_a body" in a["instructions"]

        # reg_b doesn't know skill_a → its dispatcher reports it unknown,
        # proving the pairs are isolated (no shared global).
        cross = json.loads(dispatch_b(skill_name="skill_a"))
        assert "error" in cross and "Unknown skill" in cross["error"]

        b = json.loads(dispatch_b(skill_name="skill_b"))
        assert "# skill_b body" in b["instructions"]

    def test_factory_pair_ignores_module_global(self):
        from agents.main_agent.skills.skill_tools import (
            make_skill_tools,
            set_dispatcher_registry,
        )

        reg = self._registry_with("only_skill")
        dispatch, _ = make_skill_tools(reg)
        # Even with the global cleared, the closure works.
        set_dispatcher_registry(None)
        result = json.loads(dispatch(skill_name="only_skill"))
        assert "# only_skill body" in result["instructions"]
