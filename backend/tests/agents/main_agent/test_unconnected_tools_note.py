"""The system-prompt note naming servers left out for missing consent.

An enabled OAuth-gated server whose `tools/list` 401s for a user who has not
connected its provider is dropped from the build. Before this note the model
saw no trace of it, so asked to list GitHub issues it invented `search_issues`
and got "Unknown tool" (prod, 2026-10-16).

The note sits in the cacheable prefix, so what matters is that it is absent
when nothing is missing and byte-identical for the same set of missing servers.
"""

from __future__ import annotations

from types import SimpleNamespace

from agents.builtin_tools.ask_user_question import SYSTEM_PROMPT_GUIDANCE
from agents.main_agent.chat_agent import ChatAgent, unconnected_tools_note
from agents.main_agent.integrations.external_mcp_client import ConsentGatedTool


def _gated(display_name: str, tool_id: str = "", provider_id: str = "github") -> ConsentGatedTool:
    return ConsentGatedTool(
        tool_id=tool_id or display_name.lower().replace(" ", "_"),
        display_name=display_name,
        provider_id=provider_id,
    )


def _agent(gated=None) -> ChatAgent:
    agent = ChatAgent.__new__(ChatAgent)
    agent.system_prompt = "BASE PROMPT"
    if gated is not None:
        agent.consent_gated_tools = gated
    return agent


def _tool(name: str):
    return SimpleNamespace(tool_spec={"name": name, "description": "", "inputSchema": {}})


def test_absent_when_nothing_was_left_out():
    assert _agent([])._system_prompt_for([]) == "BASE PROMPT"


def test_absent_on_an_agent_built_before_the_attribute_existed():
    assert _agent()._system_prompt_for([]) == "BASE PROMPT"


def test_names_every_left_out_server():
    gated = [_gated("Github Issues"), _gated("Github Orgs"), _gated("Github Repos")]

    prompt = _agent(gated)._system_prompt_for([])

    assert prompt.startswith("BASE PROMPT\n\n")
    assert "Github Issues, Github Orgs, Github Repos" in prompt
    assert "Do not call, guess or invent tools" in prompt


def test_follows_the_question_guidance_so_both_survive():
    prompt = _agent([_gated("Github Issues")])._system_prompt_for([_tool("ask_user_question")])

    assert prompt.index(SYSTEM_PROMPT_GUIDANCE) < prompt.index("Github Issues")


def test_same_servers_render_the_same_bytes():
    """Prefix bytes: two builds with one gap must agree exactly."""
    gated = [_gated("Github Issues"), _gated("Github Repos")]

    assert unconnected_tools_note(gated) == unconnected_tools_note(list(gated))


def test_a_name_shared_by_two_servers_is_listed_once():
    gated = [_gated("Zoom", tool_id="zoom"), _gated("Zoom", tool_id="zoom_hosted", provider_id="zoom")]

    assert unconnected_tools_note(gated).count("Zoom") == 1


def test_does_not_touch_the_snapshotted_prompt():
    """`self.system_prompt` is hashed into the agent cache key and replayed on
    resume; the note is derived from the build and must stay out of it."""
    agent = _agent([_gated("Github Issues")])

    agent._system_prompt_for([])

    assert agent.system_prompt == "BASE PROMPT"
