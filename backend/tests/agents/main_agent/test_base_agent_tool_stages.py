"""`agent_build.tools` decomposed (P1a in docs/specs/turn-path-ttft.md).

The stage was 2039ms on a cold dev build with no owner named. These pin that
`_build_filtered_tools` closes its four sub-stages in order whatever the turn
carries — so the shape of the line never depends on the toolset — and that the
per-server MCP timings reach the recorder from the calling thread, including
when the load crosses into the executor.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import patch

from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.tools.tool_filter import ToolFilterResult
from apis.shared.observability import build_stages

_STAGES = ["tools.filter", "tools.gateway", "tools.mcp", "tools.extra"]


def _agent(*, external_ids=None, extra_tools=None):
    result = ToolFilterResult(["local"], [], list(external_ids or []))
    return SimpleNamespace(
        enabled_tools=["calculator", *(external_ids or [])],
        tool_filter=SimpleNamespace(filter_tools_extended=lambda _ids: result),
        extra_tools=extra_tools or [],
        user_id="u",
        auth_token=None,
    )


class _FakeIntegration:
    def __init__(self):
        self.client = object()

    async def load_external_tools(self, ids, *, user_id, auth_token, timings=None):
        for tool_id in ids:
            timings.append({"id": tool_id, "outcome": "loaded", "totalMs": 5})
        return [self.client]


def _build(agent):
    stages, details = [], {}
    token = build_stages.set_stage_recorder(stages.append, details.__setitem__)
    try:
        tools = BaseAgent._build_filtered_tools(agent)
    finally:
        build_stages.reset_stage_recorder(token)
    return tools, stages, details


def test_a_turn_without_mcp_still_closes_every_sub_stage():
    tools, stages, details = _build(_agent(extra_tools=["spreadsheet"]))

    assert tools == ["local", "spreadsheet"]
    assert stages == _STAGES
    assert "mcpServers" not in details


def test_mcp_server_timings_reach_the_recorder():
    integration = _FakeIntegration()
    with patch(
        "agents.main_agent.integrations.external_mcp_client.get_external_mcp_integration",
        return_value=integration,
    ):
        tools, stages, details = _build(_agent(external_ids=["canvas"]))

    assert integration.client in tools
    assert stages == _STAGES
    assert details["mcpServers"] == [{"id": "canvas", "outcome": "loaded", "totalMs": 5}]


@pytest.mark.asyncio
async def test_timings_cross_the_executor_hop():
    """Under a running loop the load goes to a worker thread; the list must
    still come back filled and the marks still land on this thread."""
    integration = _FakeIntegration()
    with patch(
        "agents.main_agent.integrations.external_mcp_client.get_external_mcp_integration",
        return_value=integration,
    ):
        _, stages, details = _build(_agent(external_ids=["canvas"]))

    assert stages == _STAGES
    assert [s["id"] for s in details["mcpServers"]] == ["canvas"]
