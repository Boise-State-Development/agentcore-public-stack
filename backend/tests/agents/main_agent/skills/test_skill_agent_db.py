"""Tests for SkillAgent's DB-source helpers (PR-6).

Covers the repository fetch + ACTIVE filtering that feeds the registry, plus
the status check — without constructing a full agent (which needs a live model
+ session manager).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agents.main_agent import skill_agent


def _rec(skill_id, status="active"):
    return SimpleNamespace(
        skill_id=skill_id,
        description="",
        instructions="body",
        compose=[],
        bound_tool_ids=[],
        resources=[],
        status=status,
    )


class TestIsActiveStatus:
    def test_accepts_string_active(self):
        assert skill_agent._is_active_status("active") is True

    def test_accepts_enum_repr_active(self):
        # use_enum_values is on in the model, but be robust to "SkillStatus.ACTIVE".
        assert skill_agent._is_active_status("SkillStatus.ACTIVE") is True

    def test_rejects_non_active(self):
        assert skill_agent._is_active_status("draft") is False
        assert skill_agent._is_active_status("disabled") is False


class TestFetchSkillRecords:
    def test_empty_ids_short_circuit(self):
        assert skill_agent._fetch_skill_records([]) == []

    def test_filters_to_active_records(self):
        repo = MagicMock()
        repo.batch_get_skills = AsyncMock(
            return_value=[
                _rec("active_one", status="active"),
                _rec("draft_one", status="draft"),
                _rec("disabled_one", status="disabled"),
            ]
        )
        # get_skill_catalog_repository is imported inside the function, so
        # patch it at the source module.
        with patch(
            "apis.shared.skills.repository.get_skill_catalog_repository",
            return_value=repo,
        ):
            records = skill_agent._fetch_skill_records(
                ["active_one", "draft_one", "disabled_one"]
            )

        assert [r.skill_id for r in records] == ["active_one"]

    def test_degrades_to_empty_on_repo_error(self):
        repo = MagicMock()
        repo.batch_get_skills = AsyncMock(side_effect=RuntimeError("boom"))
        with patch(
            "apis.shared.skills.repository.get_skill_catalog_repository",
            return_value=repo,
        ):
            # Never raises into agent construction — returns [] so the agent
            # degrades to chat.
            assert skill_agent._fetch_skill_records(["x"]) == []


class TestToolUseProviderLookupWiring:
    """SkillAgent must hand the OAuth consent hook a tool_use-based provider
    resolver over ITS registry — that's what lets the consent gate see a
    skill-bound external MCP tool behind skill_executor (the skills-mode
    `oauth_required` regression). Built without a full agent: the override
    only reads `self._registry` and the global external MCP integration.
    """

    def test_lookup_resolves_folded_tool_via_integration(self):
        from agents.main_agent.skills.mcp_binding import FoldedMCPTool
        from agents.main_agent.skills.skill_registry import SkillRegistry

        client = object()
        registry = SkillRegistry()
        registry.load_records(
            [
                SimpleNamespace(
                    skill_id="gmail-for-employees",
                    description="",
                    instructions="",
                    compose=[],
                    bound_tool_ids=["gmail_mcp"],
                    resources=[],
                )
            ]
        )
        registry.bind_catalog_tools(
            {"gmail_mcp": [FoldedMCPTool(client, mcp_tool_name="gmail_search")]}
        )

        agent = object.__new__(skill_agent.SkillAgent)
        agent._registry = registry

        integration = MagicMock()
        integration.provider_for_client = (
            lambda c: "google" if c is client else None
        )
        with patch(
            "agents.main_agent.integrations.external_mcp_client.get_external_mcp_integration",
            return_value=integration,
        ):
            lookup = agent._build_tool_use_provider_lookup()

        assert lookup(
            {
                "name": "skill_executor",
                "input": {
                    "skill_name": "gmail-for-employees",
                    "tool_name": "gmail_search",
                },
            }
        ) == "google"
        assert lookup({"name": "some_other_tool", "input": {}}) is None


class TestToolUseApprovalLookupWiring:
    """SkillAgent must hand the per-tool approval hook a tool_use-based
    resolver over ITS registry — that's what lets the hook see an admin's
    needs_approval flag on a skill-bound external MCP tool behind
    skill_executor (the skills-mode approval-bypass regression). Built
    without a full agent: the override only reads `self._registry` and the
    global external MCP integration.
    """

    def test_lookup_resolves_flagged_folded_tool_via_integration(self):
        from agents.main_agent.skills.mcp_binding import FoldedMCPTool
        from agents.main_agent.skills.skill_registry import SkillRegistry

        client = object()
        registry = SkillRegistry()
        registry.load_records(
            [
                SimpleNamespace(
                    skill_id="gmail-for-employees",
                    description="",
                    instructions="",
                    compose=[],
                    bound_tool_ids=["gmail_mcp"],
                    resources=[],
                )
            ]
        )
        registry.bind_catalog_tools(
            {"gmail_mcp": [FoldedMCPTool(client, mcp_tool_name="gmail_send")]}
        )

        agent = object.__new__(skill_agent.SkillAgent)
        agent._registry = registry

        integration = MagicMock()
        integration.approval_names_for_client = (
            lambda c: {"gmail_send"} if c is client else set()
        )
        with patch(
            "agents.main_agent.integrations.external_mcp_client.get_external_mcp_integration",
            return_value=integration,
        ):
            lookup = agent._build_tool_use_approval_lookup()

        target = lookup(
            {
                "name": "skill_executor",
                "input": {
                    "skill_name": "gmail-for-employees",
                    "tool_name": "gmail_send",
                    "tool_input": {"to": "hr@example.com"},
                },
            }
        )
        assert target is not None
        assert target.tool_name == "gmail_send"
        assert target.tool_input == {"to": "hr@example.com"}
        assert lookup({"name": "some_other_tool", "input": {}}) is None


class _FoldAwareClient:
    """Stand-in external MCP client that folds like the real ones.

    ``list_tools_sync`` re-derives the full server list every call and applies
    ``drop_folded_tools`` against the persisted fold set — exactly the seam
    that poisons a re-bind when the fold from a prior build is still present.
    """

    def __init__(self, names):
        self._names = list(names)
        self._loaded_tools = None

    def list_tools_sync(self):
        from agents.main_agent.integrations.mcp_tool_folding import (
            drop_folded_tools,
        )

        tools = [
            SimpleNamespace(
                tool_name=n,
                tool_spec={"name": n},
                mcp_tool=SimpleNamespace(name=n),
            )
            for n in self._names
        ]
        return drop_folded_tools(self, tools)


class TestBindMcpToolsRebindsAcrossBuilds:
    """A skill-bound external server must stay foldable on EVERY agent build,
    not just the first. Clients are process-global and reused; the fold set
    persists on them and accumulates, and resolve enumerates through the same
    fold-filtered list_tools_sync — so without a per-build reset the second
    build sees zero tools (the reported "works once, then disappears"). """

    @staticmethod
    def _build_once(client):
        from agents.main_agent.tools.tool_filter import ToolFilter
        from agents.main_agent.skills.skill_registry import SkillRegistry

        registry = SkillRegistry()
        registry.load_records(
            [
                SimpleNamespace(
                    skill_id="canvas_check",
                    description="",
                    instructions="",
                    compose=[],
                    bound_tool_ids=["canvas::a"],
                    resources=[],
                )
            ]
        )

        agent = object.__new__(skill_agent.SkillAgent)
        agent._registry = registry
        agent._db_mode = True
        agent.tool_registry = SimpleNamespace(has_tool=lambda _t: False)
        tool_filter = ToolFilter(SimpleNamespace(has_tool=lambda _b: False))
        tool_filter.set_external_mcp_tools(["canvas"])
        agent.tool_filter = tool_filter
        agent.gateway_integration = SimpleNamespace(client=None)
        agent._expand_gateway_tool_ids = lambda _ids: []
        agent.user_id = "u1"

        integration = SimpleNamespace(get_client=lambda _tid, _uid=None: client)
        with patch(
            "agents.main_agent.integrations.external_mcp_client.get_external_mcp_integration",
            return_value=integration,
        ):
            agent._bind_mcp_tools()
        return registry

    def test_second_build_still_resolves_and_folds(self):
        from agents.main_agent.integrations.mcp_tool_folding import (
            folded_tool_names,
        )

        client = _FoldAwareClient(["a", "b"])  # shared across both builds

        reg1 = self._build_once(client)
        assert reg1.get_tools("canvas_check"), "first build should bind tools"
        assert folded_tool_names(client) == {"a", "b"}, "first build folds them"

        # Second build: a fresh registry (as in production) but the SAME cached
        # client, still carrying build 1's fold. The reset must let it rebind.
        reg2 = self._build_once(client)
        assert reg2.get_tools("canvas_check"), (
            "second build must still resolve the bound tools (regression: stale "
            "fold made list_tools_sync return nothing)"
        )
        assert folded_tool_names(client) == {"a", "b"}
