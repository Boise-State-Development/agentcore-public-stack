"""Tests for cross-source MCP skill binding + folding (PR-6b).

Covers the genuinely-hard piece: resolving a skill's gateway / external MCP
bound tool ids to foldable adapters that execute through the MCP client, with
no live MCP server (clients are faked).
"""

from types import SimpleNamespace

import pytest

from agents.main_agent.skills.mcp_binding import (
    FoldedMCPTool,
    _stringify_mcp_result,
    make_folded_tool_approval_lookup,
    make_folded_tool_provider_lookup,
    resolve_mcp_bindings,
)


def _mcp_agent_tool(agent_name, server_name=None, spec=None):
    """Fake of a Strands MCPAgentTool (the slice the resolver reads)."""
    return SimpleNamespace(
        tool_name=agent_name,
        tool_spec=spec or {"name": agent_name, "inputSchema": {"json": {}}},
        mcp_tool=SimpleNamespace(name=server_name or agent_name),
    )


class _FakeClient:
    """Records call_tool_sync; serves list_tools_sync from a fixed list."""

    def __init__(self, tools=None, result=None, raise_on_list=False):
        self._tools = tools or []
        self._result = result
        self._raise_on_list = raise_on_list
        self.calls = []

    def list_tools_sync(self, *a, **k):
        if self._raise_on_list:
            raise RuntimeError("session not active")
        return list(self._tools)

    def call_tool_sync(self, tool_use_id, name, arguments=None, **k):
        self.calls.append((tool_use_id, name, arguments))
        return self._result


class TestResolveGateway:
    def test_expands_and_folds(self):
        client = _FakeClient()
        res = resolve_mcp_bindings(
            gateway_ids=["gateway_wiki"],
            external_ids=[],
            gateway_client=client,
            expand_gateway=lambda ids: [
                "gateway_wikipedia___search",
                "gateway_wikipedia___get_article",
            ],
            external_client_lookup=lambda tid: None,
        )
        tools = res.catalog_map["gateway_wiki"]
        assert [t._mcp_tool_name for t in tools] == [
            "wikipedia___search",
            "wikipedia___get_article",
        ]
        # Agent-facing name == gateway tool name (no `gateway_` prefix).
        assert res.fold_by_client[client] == {
            "wikipedia___search",
            "wikipedia___get_article",
        }
        assert res.unresolved == []

    def test_already_expanded_runtime_id_passes_through(self):
        client = _FakeClient()
        res = resolve_mcp_bindings(
            gateway_ids=["gateway_target___tool"],
            external_ids=[],
            gateway_client=client,
            expand_gateway=lambda ids: list(ids),  # already runtime form
            external_client_lookup=lambda tid: None,
        )
        assert res.catalog_map["gateway_target___tool"][0]._mcp_tool_name == "target___tool"

    def test_no_gateway_client_is_unresolved(self):
        res = resolve_mcp_bindings(
            gateway_ids=["gateway_wiki"],
            external_ids=[],
            gateway_client=None,
            expand_gateway=lambda ids: ids,
            external_client_lookup=lambda tid: None,
        )
        assert res.unresolved == ["gateway_wiki"]
        assert res.catalog_map == {}

    def test_expand_failure_is_unresolved(self):
        def boom(ids):
            raise RuntimeError("catalog down")

        res = resolve_mcp_bindings(
            gateway_ids=["gateway_wiki"],
            external_ids=[],
            gateway_client=_FakeClient(),
            expand_gateway=boom,
            external_client_lookup=lambda tid: None,
        )
        assert res.unresolved == ["gateway_wiki"]


class TestResolveExternal:
    def test_lists_server_and_folds_each_tool(self):
        tools = [
            _mcp_agent_tool("search", server_name="search"),
            _mcp_agent_tool("fetch", server_name="fetch_raw"),
        ]
        client = _FakeClient(tools=tools)
        res = resolve_mcp_bindings(
            gateway_ids=[],
            external_ids=["my_server"],
            gateway_client=None,
            expand_gateway=lambda ids: ids,
            external_client_lookup=lambda tid: client,
        )
        bound = res.catalog_map["my_server"]
        # Server-side name preserved for call_tool_sync; agent-facing for fold.
        assert {(t.tool_name, t._mcp_tool_name) for t in bound} == {
            ("search", "search"),
            ("fetch", "fetch_raw"),
        }
        assert res.fold_by_client[client] == {"search", "fetch"}
        # Spec captured eagerly from the listed tool.
        assert bound[0].tool_spec["name"] == "search"

    def test_missing_client_is_unresolved(self):
        res = resolve_mcp_bindings(
            gateway_ids=[],
            external_ids=["gone"],
            gateway_client=None,
            expand_gateway=lambda ids: ids,
            external_client_lookup=lambda tid: None,
        )
        assert res.unresolved == ["gone"]

    def test_list_failure_is_unresolved(self):
        res = resolve_mcp_bindings(
            gateway_ids=[],
            external_ids=["srv"],
            gateway_client=None,
            expand_gateway=lambda ids: ids,
            external_client_lookup=lambda tid: _FakeClient(raise_on_list=True),
        )
        assert res.unresolved == ["srv"]

    def test_empty_server_is_unresolved(self):
        res = resolve_mcp_bindings(
            gateway_ids=[],
            external_ids=["srv"],
            gateway_client=None,
            expand_gateway=lambda ids: ids,
            external_client_lookup=lambda tid: _FakeClient(tools=[]),
        )
        assert res.unresolved == ["srv"]


class TestFoldedMCPToolExecution:
    def test_invoke_routes_through_client(self):
        client = _FakeClient(
            result={"status": "success", "content": [{"text": "the answer"}]}
        )
        tool = FoldedMCPTool(client, mcp_tool_name="wikipedia___search")
        out = tool.invoke({"query": "strands"})
        assert out == "the answer"
        # call_tool_sync got the server-side name + args.
        (_, name, args) = client.calls[0]
        assert name == "wikipedia___search"
        assert args == {"query": "strands"}

    def test_invoke_handles_none_input(self):
        client = _FakeClient(result={"content": [{"text": "ok"}]})
        FoldedMCPTool(client, mcp_tool_name="t").invoke(None)
        assert client.calls[0][2] == {}

    def test_invoke_surfaces_exceptions_as_error_status_result(self):
        """A client exception must keep its error status through the fold —
        Strands' @tool decorator passes status+content dicts through, and the
        OAuth consent hook's 401-retry heuristic only fires on error-status
        results."""

        class _Boom(_FakeClient):
            def call_tool_sync(self, *a, **k):
                raise RuntimeError("mcp down")

        out = FoldedMCPTool(_Boom(), mcp_tool_name="t").invoke({})
        assert out["status"] == "error"
        assert "mcp down" in out["content"][0]["text"]

    def test_invoke_preserves_error_status_from_mcp_result(self):
        client = _FakeClient(
            result={"status": "error", "content": [{"text": "HTTP 401 Unauthorized"}]}
        )
        out = FoldedMCPTool(client, mcp_tool_name="t").invoke({})
        assert out["status"] == "error"
        assert "401" in out["content"][0]["text"]

    def test_is_mcp_folded_marker(self):
        assert FoldedMCPTool(_FakeClient(), mcp_tool_name="t").is_mcp_folded is True


class TestFoldedMCPToolSpec:
    def test_eager_spec_returned(self):
        spec = {"name": "x", "inputSchema": {"json": {"type": "object"}}}
        tool = FoldedMCPTool(_FakeClient(), mcp_tool_name="x", tool_spec=spec)
        assert tool.tool_spec is spec

    def test_lazy_spec_resolves_from_client(self):
        # Gateway path: no spec at build time; resolved by listing at dispatch.
        listed = [_mcp_agent_tool("target___tool", spec={"name": "target___tool", "k": 1})]
        client = _FakeClient(tools=listed)
        tool = FoldedMCPTool(client, mcp_tool_name="target___tool")
        assert tool.tool_spec["k"] == 1

    def test_lazy_spec_falls_back_to_name(self):
        client = _FakeClient(raise_on_list=True)
        tool = FoldedMCPTool(client, mcp_tool_name="t")
        assert tool.tool_spec == {"name": "t"}


class TestFoldedToolProviderLookup:
    """`make_folded_tool_provider_lookup` lets the OAuth consent gate see
    through the skill fold: skill_executor tool_use input → bound
    FoldedMCPTool → owning client → provider_id."""

    def _registry_with_folded_gmail(self, client):
        from agents.main_agent.skills.skill_registry import SkillRegistry

        registry = SkillRegistry()
        registry.load_records(
            [
                SimpleNamespace(
                    skill_id="gmail-for-employees",
                    description="Gmail",
                    instructions="Use Gmail tools.",
                    compose=[],
                    bound_tool_ids=["gmail_mcp"],
                    resources=[],
                )
            ]
        )
        folded = FoldedMCPTool(
            client, mcp_tool_name="gmail_search", agent_tool_name="gmail_search"
        )
        registry.bind_catalog_tools({"gmail_mcp": [folded]})
        return registry

    def _executor_tool_use(self, skill_name="gmail-for-employees", tool_name="gmail_search"):
        return {
            "toolUseId": "tu_1",
            "name": "skill_executor",
            "input": {"skill_name": skill_name, "tool_name": tool_name},
        }

    def test_resolves_provider_for_folded_tool(self):
        client = _FakeClient()
        registry = self._registry_with_folded_gmail(client)
        lookup = make_folded_tool_provider_lookup(
            registry, lambda c: "google" if c is client else None
        )
        assert lookup(self._executor_tool_use()) == "google"

    def test_ignores_non_executor_tool_use(self):
        client = _FakeClient()
        registry = self._registry_with_folded_gmail(client)
        lookup = make_folded_tool_provider_lookup(registry, lambda c: "google")
        assert lookup({"name": "gmail_search", "input": {}}) is None

    def test_unknown_skill_or_tool_resolves_none(self):
        client = _FakeClient()
        registry = self._registry_with_folded_gmail(client)
        lookup = make_folded_tool_provider_lookup(registry, lambda c: "google")
        assert lookup(self._executor_tool_use(skill_name="nope")) is None
        assert lookup(self._executor_tool_use(tool_name="nope")) is None

    def test_local_non_folded_tool_resolves_none(self):
        from agents.main_agent.skills.skill_registry import SkillRegistry

        registry = SkillRegistry()
        registry.load_records(
            [
                SimpleNamespace(
                    skill_id="local-skill",
                    description="",
                    instructions="",
                    compose=[],
                    bound_tool_ids=["local_tool"],
                    resources=[],
                )
            ]
        )
        registry.bind_catalog_tools(
            {"local_tool": SimpleNamespace(tool_name="local_tool")}
        )
        lookup = make_folded_tool_provider_lookup(registry, lambda c: "google")
        assert (
            lookup(self._executor_tool_use("local-skill", "local_tool")) is None
        )

    def test_unmapped_client_resolves_none(self):
        # Gateway clients aren't in the provider map (SigV4, not user OAuth).
        client = _FakeClient()
        registry = self._registry_with_folded_gmail(client)
        lookup = make_folded_tool_provider_lookup(registry, lambda c: None)
        assert lookup(self._executor_tool_use()) is None

    def test_malformed_input_resolves_none(self):
        client = _FakeClient()
        registry = self._registry_with_folded_gmail(client)
        lookup = make_folded_tool_provider_lookup(registry, lambda c: "google")
        assert lookup({}) is None
        assert lookup({"name": "skill_executor"}) is None
        assert lookup({"name": "skill_executor", "input": "not-a-dict"}) is None


class TestFoldedToolApprovalLookup:
    """`make_folded_tool_approval_lookup` lets the per-tool approval gate see
    through the skill fold: skill_executor tool_use input → bound
    FoldedMCPTool → owning client's needs_approval set → the inner tool's
    name + args for the approval dialog."""

    def _registry_with_folded_gmail(self, client):
        from agents.main_agent.skills.skill_registry import SkillRegistry

        registry = SkillRegistry()
        registry.load_records(
            [
                SimpleNamespace(
                    skill_id="gmail-for-employees",
                    description="Gmail",
                    instructions="Use Gmail tools.",
                    compose=[],
                    bound_tool_ids=["gmail_mcp"],
                    resources=[],
                )
            ]
        )
        folded = FoldedMCPTool(
            client, mcp_tool_name="gmail_send", agent_tool_name="gmail_send"
        )
        registry.bind_catalog_tools({"gmail_mcp": [folded]})
        return registry

    def _executor_tool_use(
        self, skill_name="gmail-for-employees", tool_name="gmail_send"
    ):
        return {
            "toolUseId": "tu_1",
            "name": "skill_executor",
            "input": {
                "skill_name": skill_name,
                "tool_name": tool_name,
                "tool_input": {"to": "hr@example.com"},
            },
        }

    def test_resolves_flagged_folded_tool_with_inner_args(self):
        client = _FakeClient()
        registry = self._registry_with_folded_gmail(client)
        lookup = make_folded_tool_approval_lookup(
            registry, lambda c: {"gmail_send"} if c is client else set()
        )
        target = lookup(self._executor_tool_use())
        assert target is not None
        assert target.tool_name == "gmail_send"
        assert target.tool_input == {"to": "hr@example.com"}

    def test_unflagged_folded_tool_resolves_none(self):
        client = _FakeClient()
        registry = self._registry_with_folded_gmail(client)
        lookup = make_folded_tool_approval_lookup(
            registry, lambda c: {"some_other_tool"}
        )
        assert lookup(self._executor_tool_use()) is None

    def test_unflagged_client_resolves_none(self):
        # Gateway clients have no needs_approval snapshot — empty set.
        client = _FakeClient()
        registry = self._registry_with_folded_gmail(client)
        lookup = make_folded_tool_approval_lookup(registry, lambda c: set())
        assert lookup(self._executor_tool_use()) is None

    def test_ignores_non_executor_tool_use(self):
        client = _FakeClient()
        registry = self._registry_with_folded_gmail(client)
        lookup = make_folded_tool_approval_lookup(registry, lambda c: {"gmail_send"})
        assert lookup({"name": "gmail_send", "input": {}}) is None

    def test_unknown_skill_or_tool_resolves_none(self):
        client = _FakeClient()
        registry = self._registry_with_folded_gmail(client)
        lookup = make_folded_tool_approval_lookup(registry, lambda c: {"gmail_send"})
        assert lookup(self._executor_tool_use(skill_name="nope")) is None
        assert lookup(self._executor_tool_use(tool_name="nope")) is None

    def test_local_non_folded_tool_resolves_none(self):
        from agents.main_agent.skills.skill_registry import SkillRegistry

        registry = SkillRegistry()
        registry.load_records(
            [
                SimpleNamespace(
                    skill_id="local-skill",
                    description="",
                    instructions="",
                    compose=[],
                    bound_tool_ids=["local_tool"],
                    resources=[],
                )
            ]
        )
        registry.bind_catalog_tools(
            {"local_tool": SimpleNamespace(tool_name="local_tool")}
        )
        lookup = make_folded_tool_approval_lookup(
            registry, lambda c: {"local_tool"}
        )
        assert (
            lookup(self._executor_tool_use("local-skill", "local_tool")) is None
        )


class TestStringify:
    def test_joins_text_blocks(self):
        result = {"content": [{"text": "a"}, {"text": "b"}]}
        assert _stringify_mcp_result(result) == "a\nb"

    def test_non_text_content_json_dumps(self):
        result = {"content": [{"json": {"k": 1}}]}
        out = _stringify_mcp_result(result)
        assert "json" in out and "k" in out

    def test_object_shaped_result(self):
        result = SimpleNamespace(content=[SimpleNamespace(text="hi")])
        assert _stringify_mcp_result(result) == "hi"
