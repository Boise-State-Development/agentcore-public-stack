"""Per-tool approval for Gateway (protocol='mcp') tools (#454).

The admin form stores `needs_approval` on each tool of a Gateway target. These
tests cover the three links between that flag and the approval card:

1. `resolve_gateway_tools` turns the flags into the names the model calls
   (`<target>___<tool>`), so a flag on one target never gates another target's
   tool of the same name.
2. `GatewayIntegration` answers only for its own client.
3. The approval hook built by `BaseAgent` asks the Gateway integration about a
   Gateway tool, pauses on a flagged one, and leaves every other tool alone.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from strands.tools.mcp import MCPAgentTool

from agents.main_agent.tools.gateway_integration import (
    GatewayIntegration,
    resolve_gateway_tools,
)


class _FakeRepo:
    def __init__(self, tools: dict):
        self._tools = tools

    async def get_tool(self, tool_id: str):
        return self._tools.get(tool_id)


def _entry(name: str, needs_approval: bool = False):
    return SimpleNamespace(name=name, needs_approval=needs_approval)


def _gateway_row(target: str, entries: list):
    return SimpleNamespace(
        protocol="mcp",
        mcp_gateway_config=SimpleNamespace(target_name=target, tools=entries),
    )


# ------------------------------------------------------------ resolution


@pytest.mark.asyncio
async def test_flagged_tools_become_target_qualified_names():
    repo = _FakeRepo(
        {
            "gateway_policy_search": _gateway_row(
                "policy-search",
                [_entry("search_policies"), _entry("list_policy_categories", True)],
            )
        }
    )
    out = await resolve_gateway_tools(["gateway_policy_search"], repo)
    assert out.runtime_ids == [
        "gateway_policy-search___search_policies",
        "gateway_policy-search___list_policy_categories",
    ]
    assert out.approval_names == {"policy-search___list_policy_categories"}


@pytest.mark.asyncio
async def test_same_tool_name_on_another_target_is_not_gated():
    repo = _FakeRepo(
        {
            "gateway_a": _gateway_row("target-a", [_entry("search", True)]),
            "gateway_b": _gateway_row("target-b", [_entry("search")]),
        }
    )
    out = await resolve_gateway_tools(["gateway_a", "gateway_b"], repo)
    assert out.approval_names == {"target-a___search"}


@pytest.mark.asyncio
async def test_scoped_id_keeps_the_targets_flags():
    repo = _FakeRepo(
        {"gateway_a": _gateway_row("target-a", [_entry("write", True), _entry("read")])}
    )
    out = await resolve_gateway_tools(["gateway_a::write"], repo)
    assert out.runtime_ids == ["gateway_target-a___write"]
    assert "target-a___write" in out.approval_names


@pytest.mark.asyncio
async def test_rbac_runtime_ids_and_dynamic_targets_carry_no_flags():
    # Runtime ids without a catalog row, and targets with no curated tool list,
    # keep working and are not gated (not failed closed either).
    repo = _FakeRepo({"gateway_dyn": _gateway_row("dyn", [])})
    out = await resolve_gateway_tools(
        ["gateway_arxiv___search_papers", "gateway_dyn"], repo
    )
    assert out.runtime_ids == ["gateway_arxiv___search_papers", "gateway_dyn"]
    assert out.approval_names == set()


# ------------------------------------------------------------ integration


def _integration_with_client(approval_names):
    integration = GatewayIntegration()
    client = MagicMock(name="gateway-client")
    with patch(
        "agents.main_agent.tools.gateway_integration.get_gateway_client_if_enabled",
        return_value=client,
    ):
        integration.get_client(
            ["gateway_t___x"], auth_token="tok", approval_names=approval_names
        )
    return integration, client


def test_integration_answers_only_for_its_own_client():
    integration, client = _integration_with_client({"t___x"})
    assert integration.approval_names_for_client(client) == {"t___x"}
    assert integration.approval_names_for_client(MagicMock()) == set()
    assert integration.approval_names_for_client(None) == set()


def test_integration_without_a_client_has_no_names():
    integration = GatewayIntegration()
    with patch(
        "agents.main_agent.tools.gateway_integration.get_gateway_client_if_enabled",
        return_value=None,
    ):
        integration.get_client(["gateway_t___x"], approval_names={"t___x"})
    assert integration.approval_names_for_client(None) == set()


# ------------------------------------------------------------ approval hook


def _hook(monkeypatch, gateway_integration, external_names=frozenset()):
    from agents.main_agent import base_agent

    external = MagicMock()
    external.approval_names_for_client.return_value = set(external_names)
    monkeypatch.setattr(
        "agents.main_agent.integrations.external_mcp_client.get_external_mcp_integration",
        lambda: external,
    )
    owner = SimpleNamespace(user_id="u1", gateway_integration=gateway_integration)
    return base_agent.BaseAgent._build_mcp_external_approval_hook(owner)


def _mcp_tool(client):
    tool = MagicMock(spec=MCPAgentTool)
    tool.mcp_client = client
    return tool


def _event(tool_name: str, selected_tool, response="approved"):
    event = MagicMock()
    event.tool_use = {"name": tool_name, "toolUseId": "tu-1", "input": {}}
    event.cancel_tool = None
    event.interrupt = MagicMock(return_value=response)
    event.selected_tool = selected_tool
    return event


def test_flagged_gateway_tool_pauses_for_approval(monkeypatch):
    integration, client = _integration_with_client({"policy-search___list_policy_categories"})
    hook = _hook(monkeypatch, integration)

    event = _event("policy-search___list_policy_categories", _mcp_tool(client))
    asyncio.run(hook._gate(event))

    event.interrupt.assert_called_once()
    assert event.interrupt.call_args.kwargs["reason"]["type"] == "tool_approval_required"
    assert event.cancel_tool is None


def test_declined_gateway_tool_is_cancelled(monkeypatch):
    integration, client = _integration_with_client({"t___write"})
    hook = _hook(monkeypatch, integration)

    event = _event("t___write", _mcp_tool(client), response="declined")
    asyncio.run(hook._gate(event))

    assert event.cancel_tool is not None


def test_unflagged_gateway_tool_runs(monkeypatch):
    integration, client = _integration_with_client({"t___write"})
    hook = _hook(monkeypatch, integration)

    event = _event("t___read", _mcp_tool(client))
    asyncio.run(hook._gate(event))

    event.interrupt.assert_not_called()


def test_external_mcp_tools_keep_their_own_set(monkeypatch):
    integration, _ = _integration_with_client({"t___write"})
    hook = _hook(monkeypatch, integration, external_names={"send_email"})

    assert hook._approval_names_lookup(_mcp_tool(MagicMock())) == {"send_email"}
