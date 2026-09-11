"""An Agent's scoped tool bindings must reach the MCP client as a name filter.

The layers either side of this seam are each covered on their own — the resolver
in ``test_agent_binding_resolver.py``, the client filter in
``tests/agents/main_agent/integrations/test_external_mcp_client.py``. What neither
proves is that they are *connected*: a resolver that collapsed a scoped ref to its
base would pass its own tests and quietly hand the turn all 44 of a server's tools,
which is invisible from outside because the agent still works. It just stops being
fenced, and the tool definitions it does not need stay in the cacheable prefix on
every turn for the life of the session.

So this drives the real chain — ``Assistant.bindings`` → ``resolve_agent_invocation``
→ ``enabled_tools`` → ``ToolFilter`` classification → ``load_external_tools`` — and
asserts the filter that actually reaches ``create_external_mcp_client``. Only the
catalog and the client constructor are stubbed.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.main_agent.integrations.external_mcp_client import ExternalMCPIntegration
from agents.main_agent.tools.tool_filter import ToolFilter
from apis.inference_api.chat.agent_binding_resolver import resolve_agent_invocation
from apis.shared.assistants.models import AgentBinding, Assistant
from apis.shared.auth.models import User

RESOLVER = "apis.inference_api.chat.agent_binding_resolver"
SERVER = "canvas_faculty"

# The seven the Rubric Builder agent actually needs, of the forty-four the server has.
BOUND = [
    "list_courses",
    "list_assignments",
    "get_assignment_details",
    "list_rubrics",
    "get_rubric",
    "create_rubric",
    "associate_rubric",
]
# The ones its system prompt currently asks it not to call.
UNBOUND = ["grade_submission", "bulk_grade_submissions", "create_assignment", "delete_rubric"]


def _user() -> User:
    return User(email="prof@x.edu", user_id="u-prof", name="Prof", roles=[])


def _agent(refs) -> Assistant:
    return Assistant(
        assistantId="ast-rubric",
        ownerId="u-alice",
        ownerName="Alice",
        name="Rubric Builder",
        description="d",
        instructions="i",
        vectorIndexId="idx",
        visibility="SHARED",
        createdAt="t",
        updatedAt="t",
        status="COMPLETE",
        bindings=[AgentBinding(kind="tool", ref=r) for r in refs],
    )


def _grant_whole_server(monkeypatch):
    """The invoker's role grants the server; the binding decides the subset."""
    svc = MagicMock()
    svc.can_access_tool = AsyncMock(return_value=True)
    monkeypatch.setattr(f"{RESOLVER}.get_app_role_service", lambda: svc)


def _catalog_tool():
    return SimpleNamespace(
        tool_id=SERVER,
        protocol="mcp_external",
        mcp_config=SimpleNamespace(
            server_url="https://example.com/mcp",
            approval_required_names=lambda: set(),
        ),
        forward_auth_token=False,
        requires_oauth_provider=None,
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


async def _allowed_names_for(tool_ids):
    """Run ``tool_ids`` through the runtime and return the client's name filter."""
    integration = ExternalMCPIntegration()
    repo = SimpleNamespace(get_tool=AsyncMock(return_value=_catalog_tool()))
    client = SimpleNamespace(load_tools=AsyncMock(return_value=[]))
    with patch(
        "apis.shared.tools.repository.get_tool_catalog_repository", return_value=repo
    ), patch(
        "agents.main_agent.integrations.external_mcp_client.create_external_mcp_client",
        return_value=client,
    ) as create_mock:
        await integration.load_external_tools(tool_ids)
    assert create_mock.call_count == 1, "one server ⇒ one client"
    return create_mock.call_args.kwargs["allowed_tool_names"]


class TestScopedBindingsNarrowTheTurn:
    @pytest.mark.asyncio
    async def test_scoped_bindings_yield_a_filtered_client(self, monkeypatch):
        _grant_whole_server(monkeypatch)
        plan = await resolve_agent_invocation(
            _agent([f"{SERVER}::{name}" for name in BOUND]), _user()
        )

        allowed = await _allowed_names_for(plan.tools.tool_ids)

        assert allowed == set(BOUND)
        # The point of the exercise: the destructive tools are absent from the turn,
        # not merely unused by it.
        for name in UNBOUND:
            assert name not in allowed

    @pytest.mark.asyncio
    async def test_bare_binding_still_loads_the_whole_server(self, monkeypatch):
        """Additive — every Agent binding a server today keeps all of its tools."""
        _grant_whole_server(monkeypatch)
        plan = await resolve_agent_invocation(_agent([SERVER]), _user())

        assert await _allowed_names_for(plan.tools.tool_ids) is None

    @pytest.mark.asyncio
    async def test_a_bare_binding_alongside_scoped_ones_wins(self, monkeypatch):
        """Documented ``collect_tool_name_filters`` precedence, asserted end to end.

        An author who binds the server *and* two of its tools has asked for the
        server; the filter must not silently narrow to the two.
        """
        _grant_whole_server(monkeypatch)
        plan = await resolve_agent_invocation(
            _agent([SERVER, f"{SERVER}::list_courses", f"{SERVER}::get_rubric"]), _user()
        )

        assert await _allowed_names_for(plan.tools.tool_ids) is None

    @pytest.mark.asyncio
    async def test_scoped_ids_classify_as_external_mcp_tools(self, monkeypatch):
        """The step between: ``ToolFilter`` must route a scoped id to the MCP loader.

        It classifies on the *base* id, so a scoped ref that failed to match would
        fall through to the "not a known tool id" warning and be dropped silently —
        the agent would run with no Canvas tools at all.
        """
        _grant_whole_server(monkeypatch)
        plan = await resolve_agent_invocation(
            _agent([f"{SERVER}::{name}" for name in BOUND]), _user()
        )

        tool_filter = ToolFilter(registry=SimpleNamespace(has_tool=lambda _: False))
        tool_filter.set_external_mcp_tools([SERVER])
        result = tool_filter.filter_tools_extended(plan.tools.tool_ids)

        assert result.external_mcp_tool_ids == plan.tools.tool_ids
        assert result.local_tools == [] and result.gateway_tool_ids == []
