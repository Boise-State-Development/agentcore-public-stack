"""``TurnToolSet``: a turn's tools with no agent (quiet-watcher triage §4.1).

It runs ``BaseAgent``'s own tool assembly, inherited, and skips the rest of ``BaseAgent.__init__``.
The pin below fails the moment one of those methods reads an attribute ``TurnToolSet`` does not
set, so the watch triage can never quietly drift from the turn's tools.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from agents.main_agent.base_agent import BaseAgent
from agents.main_agent.tool_set import TurnToolSet, load_tools

#: The BaseAgent methods TurnToolSet reuses, directly or through each other.
REUSED = ("_register_external_mcp_tools", "_build_filtered_tools", "_resolve_gateway_tools")


def _self_reads(method_name: str) -> set[str]:
    source = textwrap.dedent(inspect.getsource(getattr(BaseAgent, method_name)))
    reads = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            reads.add(node.attr)
    return reads


@pytest.fixture()
def no_catalog(monkeypatch):
    """No catalog row for any id: every enabled id is a registry tool or nothing, as on a bare stack."""

    class _Repo:
        async def get_tool(self, tool_id):
            return None

    import apis.shared.tools.repository as repository

    monkeypatch.setattr(repository, "get_tool_catalog_repository", lambda: _Repo())


def test_every_attribute_the_reused_methods_read_is_set(no_catalog):
    tool_set = TurnToolSet(user_id="u", auth_token=None, enabled_tools=["calculator"])
    missing = {
        attr
        for name in REUSED
        for attr in _self_reads(name)
        if not hasattr(tool_set, attr)
    }
    assert missing == set(), f"TurnToolSet must set what BaseAgent's tool assembly reads: {sorted(missing)}"


def test_it_never_builds_an_agent(no_catalog):
    tool_set = TurnToolSet(user_id="u", auth_token=None, enabled_tools=[])
    for absent in ("agent", "session_manager", "system_prompt", "model_config"):
        assert not hasattr(tool_set, absent)
    with pytest.raises(NotImplementedError):
        tool_set._create_agent()


def test_registry_tools_and_extra_tools_load_by_name(no_catalog):
    from strands import tool

    @tool
    def whoami() -> str:
        """Who the caller is."""
        return "Avery Example"

    tools_by_name, registry, gated = load_tools(
        user_id="u", auth_token=None, enabled_tools=["calculator", "fetch_url_content"], extra_tools=[whoami]
    )
    try:
        assert {"calculator", "fetch_url_content", "whoami"} <= set(tools_by_name)
        assert gated == []
    finally:
        registry.cleanup()


def test_an_id_the_user_does_not_have_loads_nothing(no_catalog):
    tools_by_name, registry, _ = load_tools(user_id="u", auth_token=None, enabled_tools=[])
    try:
        assert "calculator" not in tools_by_name
    finally:
        registry.cleanup()
