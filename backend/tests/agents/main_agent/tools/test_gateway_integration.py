"""Tests for gateway tool-id expansion (#419 Blocker 2).

A protocol='mcp' catalog tool carries one id (`gateway_class_search`), but the
AgentCore Gateway exposes its target's tools as `<targetName>___<toolName>`.
`expand_gateway_tool_ids` bridges the two so the agent's FilteredMCPClient can
match them; without it, an enabled #419 tool is silently dropped.
"""

from types import SimpleNamespace

import pytest

from agents.main_agent.tools.gateway_integration import expand_gateway_tool_ids


class _FakeRepo:
    """Minimal async tool-catalog repository for the expansion helper."""

    def __init__(self, tools: dict):
        self._tools = tools

    async def get_tool(self, tool_id: str):
        return self._tools.get(tool_id)


def _gateway_tool(target_name: str, tool_names: list[str]):
    return SimpleNamespace(
        protocol="mcp",
        mcp_gateway_config=SimpleNamespace(
            target_name=target_name,
            tools=[SimpleNamespace(name=n) for n in tool_names],
        ),
    )


@pytest.mark.asyncio
async def test_expands_catalog_tool_to_runtime_ids():
    repo = _FakeRepo(
        {"gateway_class_search": _gateway_tool("gateway-class-search", ["search_classes", "get_class_details"])}
    )
    out = await expand_gateway_tool_ids(["gateway_class_search"], repo)
    assert out == [
        "gateway_gateway-class-search___search_classes",
        "gateway_gateway-class-search___get_class_details",
    ]


@pytest.mark.asyncio
async def test_runtime_ids_pass_through_without_lookup():
    # A raw `___` id is already runtime — must not even hit the repo.
    class _BoomRepo:
        async def get_tool(self, tool_id):  # pragma: no cover - must not run
            raise AssertionError("should not look up a runtime id")

    out = await expand_gateway_tool_ids(
        ["gateway_wikipedia-search___wikipedia_search"], _BoomRepo()
    )
    assert out == ["gateway_wikipedia-search___wikipedia_search"]


@pytest.mark.asyncio
async def test_unknown_and_empty_tool_list_pass_through():
    repo = _FakeRepo(
        {
            # catalog tool with no curated tools (e.g. DYNAMIC listing)
            "gateway_empty": _gateway_tool("some-target", []),
        }
    )
    out = await expand_gateway_tool_ids(["gateway_empty", "gateway_unknown"], repo)
    assert out == ["gateway_empty", "gateway_unknown"]


@pytest.mark.asyncio
async def test_scoped_id_expands_to_single_runtime_id():
    # A scoped catalog id selects ONE tool of the target.
    repo = _FakeRepo(
        {"gateway_class_search": _gateway_tool("gateway-class-search", ["search_classes", "get_class_details"])}
    )
    out = await expand_gateway_tool_ids(["gateway_class_search::search_classes"], repo)
    assert out == ["gateway_gateway-class-search___search_classes"]


@pytest.mark.asyncio
async def test_scoped_id_works_for_tool_not_in_curated_list():
    # Discover-path: a scoped tool need not be in the curated tools[] (e.g. a
    # DYNAMIC target discovered live). The target name still resolves it.
    repo = _FakeRepo({"gateway_dyn": _gateway_tool("dyn-target", [])})
    out = await expand_gateway_tool_ids(["gateway_dyn::live_tool"], repo)
    assert out == ["gateway_dyn-target___live_tool"]


@pytest.mark.asyncio
async def test_scoped_id_without_target_is_skipped():
    # Cannot build a runtime id without a target_name — skip rather than emit a
    # malformed id.
    repo = _FakeRepo({"gateway_x": SimpleNamespace(protocol="mcp", mcp_gateway_config=None)})
    out = await expand_gateway_tool_ids(["gateway_x::foo"], repo)
    assert out == []


@pytest.mark.asyncio
async def test_dedupes_preserving_order():
    repo = _FakeRepo(
        {
            "gateway_a": _gateway_tool("t", ["one", "two"]),
            # second catalog tool repeats one runtime id
            "gateway_b": _gateway_tool("t", ["two", "three"]),
        }
    )
    out = await expand_gateway_tool_ids(["gateway_a", "gateway_b"], repo)
    assert out == [
        "gateway_t___one",
        "gateway_t___two",
        "gateway_t___three",
    ]
