"""One clashing tool name must cost that tool, not the whole turn.

dev, 2026-10-17: two catalog entries pointing at Zoom's hosted MCP server both
expose `get_file_content`. Strands' registry refused the second copy, so the
agent was never built and every conversation of a user with both enabled failed
with "Failed to load tool <UICapableMCPClient ...>: Tool name 'get_file_content'
already exists".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

import pytest
from mcp.types import Tool as MCPTool
from strands import tool
from strands.tools import ToolProvider
from strands.tools.mcp import MCPAgentTool
from strands.tools.registry import ToolRegistry

from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.chat_agent import ChatAgent
from agents.main_agent.tools.tool_filter import ToolFilterResult
from agents.main_agent.tools.tool_name_conflicts import (
    MAX_TOOL_NAME_LENGTH,
    NameExcludingToolProvider,
    SkippedToolName,
    resolve_tool_name_conflicts,
    skipped_tools_note,
)


def _named_tool(name: str):
    @tool(name=name)
    def _impl() -> str:
        """A test tool."""
        return name

    return _impl


class _FakeServer(ToolProvider):
    """Stands in for a pre-flighted external MCP client.

    Its tools are real ``MCPAgentTool``s: unlike ``@tool`` functions they do
    not support hot reload, which is what makes the registry refuse a clash.
    """

    def __init__(self, label: str, names: List[str]):
        self.context_label = label
        self._tools = [
            MCPAgentTool(MCPTool(name=n, inputSchema={"type": "object"}), self)
            for n in names
        ]
        self.consumers: set = set()

    async def load_tools(self, **kwargs: Any):
        return self._tools

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.add(consumer_id)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.consumers.discard(consumer_id)


def _registered_names(tools) -> List[str]:
    registry = ToolRegistry()
    registry.process_tools(tools)
    return sorted(registry.registry)


def test_the_registry_refuses_a_clash_without_resolution():
    """The failure as dev saw it, so the test below is known to cover it."""
    zoom = _FakeServer("Zoom", ["get_file_content", "search_meetings"])
    zoom_2 = _FakeServer("Race Test Zoom 2", ["get_file_content", "recordings_list"])

    with pytest.raises(ValueError, match="get_file_content' already exists"):
        _registered_names([zoom, zoom_2])


def test_a_clashing_server_keeps_its_other_tools():
    zoom = _FakeServer("Zoom", ["get_file_content", "search_meetings"])
    zoom_2 = _FakeServer("Race Test Zoom 2", ["get_file_content", "recordings_list"])

    resolved, skipped = resolve_tool_name_conflicts([zoom, zoom_2])

    assert _registered_names(resolved) == ["get_file_content", "recordings_list", "search_meetings"]
    assert skipped == [SkippedToolName("get_file_content", "Race Test Zoom 2", "Zoom")]


def test_the_first_source_keeps_the_name():
    """List order decides, so a built-in is never displaced by a server."""
    builtin = _named_tool("fetch_url")
    server = _FakeServer("Web MCP", ["fetch_url", "crawl"])

    resolved, skipped = resolve_tool_name_conflicts([builtin, server])

    assert resolved[0] is builtin
    assert isinstance(resolved[1], NameExcludingToolProvider)
    assert skipped == [SkippedToolName("fetch_url", "Web MCP", "built-in tools")]


def test_a_platform_tool_wins_even_when_it_comes_after_a_server():
    """dev, 2026-10-17, after #1643: `whoami` is a system tool injected as a
    context-bound tool, which the build appends after the MCP servers. Canvas
    for Faculty also exposes `whoami`, and "first wins" let it shadow the
    platform's own. Before #1643, Strands' hot reload let the built-in win.
    """
    canvas = _FakeServer("Canvas for Faculty", ["whoami", "list_courses"])
    builtin_whoami = _named_tool("whoami")

    resolved, skipped = resolve_tool_name_conflicts([canvas, builtin_whoami])

    assert resolved[1] is builtin_whoami
    assert isinstance(resolved[0], NameExcludingToolProvider)
    assert skipped == [SkippedToolName("whoami", "Canvas for Faculty", "built-in tools")]
    registry = ToolRegistry()
    registry.process_tools(resolved)
    assert registry.registry["whoami"] is builtin_whoami


def test_names_that_differ_only_by_dash_and_underscore_clash():
    """Strands rejects these as duplicates too."""
    first = _FakeServer("A", ["get-file"])
    second = _FakeServer("B", ["get_file", "other"])

    resolved, skipped = resolve_tool_name_conflicts([first, second])

    assert _registered_names(resolved) == ["get-file", "other"]
    assert [s.name for s in skipped] == ["get_file"]


def test_the_shared_client_is_not_changed():
    """External clients are cached across agents, most of which have no
    clash; only this agent's view may lose the tool."""
    zoom = _FakeServer("Zoom", ["get_file_content"])
    zoom_2 = _FakeServer("Race Test Zoom 2", ["get_file_content", "recordings_list"])

    resolve_tool_name_conflicts([zoom, zoom_2])

    assert [t.tool_name for t in zoom_2._tools] == ["get_file_content", "recordings_list"]


def test_the_wrapper_hands_lifecycle_to_the_client():
    zoom = _FakeServer("Zoom", ["get_file_content"])
    zoom_2 = _FakeServer("Race Test Zoom 2", ["get_file_content", "recordings_list"])
    resolved, _ = resolve_tool_name_conflicts([zoom, zoom_2])

    registry = ToolRegistry()
    registry.process_tools(resolved)
    assert zoom_2.consumers  # the registry's consumer reached the real client
    registry.cleanup()
    assert not zoom_2.consumers
    assert resolved[1].context_label == "Race Test Zoom 2"


def test_no_clash_leaves_the_list_untouched():
    a = _FakeServer("A", ["one"])
    b = _FakeServer("B", ["two"])
    builtin = _named_tool("three")

    resolved, skipped = resolve_tool_name_conflicts([builtin, a, b])

    assert resolved == [builtin, a, b]
    assert skipped == []


def test_a_server_that_cannot_list_is_left_for_the_registry():
    class _Broken(_FakeServer):
        async def load_tools(self, **kwargs: Any):
            raise RuntimeError("unreachable")

    broken = _Broken("Broken", [])
    resolved, skipped = resolve_tool_name_conflicts([broken])

    assert resolved == [broken]
    assert skipped == []


@pytest.mark.asyncio
async def test_works_from_inside_a_running_event_loop():
    """`get_agent` builds the agent on the inference API's loop thread."""
    zoom = _FakeServer("Zoom", ["get_file_content"])
    zoom_2 = _FakeServer("Race Test Zoom 2", ["get_file_content"])

    _, skipped = resolve_tool_name_conflicts([zoom, zoom_2])

    assert [s.name for s in skipped] == ["get_file_content"]


# --- wiring: the agent build and the system prompt ---------------------------


class _FakeIntegration:
    def __init__(self, clients):
        self.clients = clients

    async def load_external_tools(
        self, ids, *, user_id, auth_token, timings=None, consent_gated=None
    ):
        return list(self.clients)


def _agent_stub(external_clients):
    ids = [c.context_label for c in external_clients]
    result = ToolFilterResult([], [], ids)
    return SimpleNamespace(
        enabled_tools=ids,
        tool_filter=SimpleNamespace(filter_tools_extended=lambda _ids: result),
        extra_tools=[],
        user_id="u",
        auth_token=None,
        consent_gated_tools=[],
        skipped_tool_names=[],
    )


def test_the_agent_build_resolves_clashes_and_records_them():
    zoom = _FakeServer("Zoom", ["get_file_content", "search_meetings"])
    zoom_2 = _FakeServer("Race Test Zoom 2", ["get_file_content", "recordings_list"])
    agent = _agent_stub([zoom, zoom_2])

    with patch(
        "agents.main_agent.integrations.external_mcp_client.get_external_mcp_integration",
        return_value=_FakeIntegration([zoom, zoom_2]),
    ):
        tools = BaseAgent._build_filtered_tools(agent)

    assert _registered_names(tools) == ["get_file_content", "recordings_list", "search_meetings"]
    assert agent.skipped_tool_names == [
        SkippedToolName("get_file_content", "Race Test Zoom 2", "Zoom")
    ]


def _chat_agent(skipped=None) -> ChatAgent:
    agent = ChatAgent.__new__(ChatAgent)
    agent.system_prompt = "BASE PROMPT"
    agent.consent_gated_tools = []
    if skipped is not None:
        agent.skipped_tool_names = skipped
    return agent


def test_the_model_is_told_which_tools_were_left_out():
    skipped = [SkippedToolName("get_file_content", "Race Test Zoom 2", "Zoom")]

    prompt = _chat_agent(skipped)._system_prompt_for([])

    assert prompt.startswith("BASE PROMPT\n\n")
    assert "`get_file_content` from Race Test Zoom 2" in prompt
    assert "Zoom's" in prompt


def test_no_note_without_a_clash():
    assert _chat_agent([])._system_prompt_for([]) == "BASE PROMPT"
    # An agent built before the attribute existed (a resumed snapshot).
    assert _chat_agent()._system_prompt_for([]) == "BASE PROMPT"


def test_the_note_is_byte_stable():
    """It sits in the cacheable prefix."""
    skipped = [
        SkippedToolName("a", "S2", "S1"),
        SkippedToolName("b", "S3", "S1"),
    ]
    assert skipped_tools_note(skipped) == skipped_tools_note(list(skipped))


# --- names Bedrock will not accept ------------------------------------------


_TOO_LONG = "generate_quarterly_enrollment_summary_report_for_every_active_program"
assert len(_TOO_LONG) > MAX_TOOL_NAME_LENGTH


def test_a_name_too_long_for_bedrock_is_left_out_and_the_rest_survive():
    """`toolSpec.name` is capped at 64 characters and Bedrock refuses the whole
    request, so one over-long name would cost the turn every other tool with
    it. An MCP server names its own tools, so it must only cost that tool.
    """
    server = _FakeServer("Reports MCP", [_TOO_LONG, "list_reports"])

    resolved, skipped = resolve_tool_name_conflicts([server])

    assert _registered_names(resolved) == ["list_reports"]
    assert skipped == [
        SkippedToolName(_TOO_LONG, "Reports MCP", reason="too_long")
    ]


def test_a_name_exactly_at_the_limit_is_kept():
    at_limit = "a" * MAX_TOOL_NAME_LENGTH
    server = _FakeServer("Edge MCP", [at_limit])

    resolved, skipped = resolve_tool_name_conflicts([server])

    assert _registered_names(resolved) == [at_limit]
    assert skipped == []


def test_an_over_long_platform_tool_is_left_out_too():
    builtin = _named_tool(_TOO_LONG)
    ok = _named_tool("summarize")

    resolved, skipped = resolve_tool_name_conflicts([builtin, ok])

    assert resolved == [ok]
    assert [s.reason for s in skipped] == ["too_long"]


def test_an_over_long_name_is_not_held_against_another_source():
    """It is left out everywhere, so it is never recorded as taken — the second
    copy is dropped for being too long, not for clashing."""
    first = _FakeServer("A", [_TOO_LONG])
    second = _FakeServer("B", [_TOO_LONG, "fine"])

    resolved, skipped = resolve_tool_name_conflicts([first, second])

    assert _registered_names(resolved) == ["fine"]
    assert [(s.source, s.reason) for s in skipped] == [
        ("A", "too_long"),
        ("B", "too_long"),
    ]


def test_the_model_is_told_about_a_name_that_is_too_long():
    note = skipped_tools_note(
        [SkippedToolName(_TOO_LONG, "Reports MCP", reason="too_long")]
    )

    assert "too long" in note
    assert f"`{_TOO_LONG}` from Reports MCP" in note
    assert str(MAX_TOOL_NAME_LENGTH) in note


def test_both_kinds_of_note_can_appear_together():
    note = skipped_tools_note(
        [
            SkippedToolName("whoami", "Salesforce", "Canvas"),
            SkippedToolName(_TOO_LONG, "Reports MCP", reason="too_long"),
        ]
    )

    assert note.count("##") == 2
    assert note.index("name clash") < note.index("too long")
