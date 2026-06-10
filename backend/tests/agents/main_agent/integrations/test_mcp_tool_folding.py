"""Tests for the per-client MCP tool-fold mechanism (PR-6b)."""

from types import SimpleNamespace

from agents.main_agent.integrations.mcp_tool_folding import (
    drop_folded_tools,
    folded_tool_names,
    set_folded_tool_names,
)


def _tool(name):
    return SimpleNamespace(tool_name=name)


class _Client:
    """Minimal stand-in for an MCPClient (just the fields the helpers touch)."""

    def __init__(self, loaded=None):
        self._loaded_tools = loaded


class TestSetFolded:
    def test_records_names(self):
        c = _Client()
        set_folded_tool_names(c, ["a", "b"])
        assert folded_tool_names(c) == {"a", "b"}

    def test_accumulates_across_calls(self):
        c = _Client()
        set_folded_tool_names(c, ["a"])
        set_folded_tool_names(c, ["b", "c"])
        assert folded_tool_names(c) == {"a", "b", "c"}

    def test_invalidates_loaded_tools_cache(self):
        # The external pre-flight primes _loaded_tools before folds are known;
        # setting a fold must reset it so Strands re-lists with the fold.
        c = _Client(loaded=[_tool("x")])
        set_folded_tool_names(c, ["x"])
        assert c._loaded_tools is None

    def test_no_cache_to_invalidate_is_fine(self):
        c = _Client(loaded=None)
        set_folded_tool_names(c, ["x"])
        assert c._loaded_tools is None


class TestDropFolded:
    def test_drops_only_folded(self):
        c = _Client()
        set_folded_tool_names(c, ["hide_me"])
        tools = [_tool("keep"), _tool("hide_me"), _tool("keep2")]
        kept = drop_folded_tools(c, tools)
        assert [t.tool_name for t in kept] == ["keep", "keep2"]

    def test_no_fold_set_is_passthrough(self):
        c = _Client()
        tools = [_tool("a"), _tool("b")]
        assert drop_folded_tools(c, tools) is tools

    def test_empty_fold_set_is_passthrough(self):
        c = _Client()
        set_folded_tool_names(c, [])
        tools = [_tool("a")]
        # An empty set is falsy → treated as "nothing folded".
        assert drop_folded_tools(c, tools) == tools

    def test_folded_names_empty_default(self):
        assert folded_tool_names(_Client()) == set()
