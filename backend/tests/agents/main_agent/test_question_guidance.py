"""The system-prompt guidance that makes `ask_user_question` actually fire.

Without it the model answers an ambiguous request in prose instead of asking:
measured 4/24 against a production-shaped tool set, 24/24 with it, and 0/18 on
unambiguous requests either way. The same words placed in the tool's own
description measured no better than baseline, so the text is load-bearing in
this position specifically.

These tests pin the wiring, not the wording — the wording is pinned by the
harness and by the warning on the constant. What can silently regress here is
the *gating*: appending for a user who does not have the tool, keying off the
request instead of the filtered list, or mutating `self.system_prompt` (which
is snapshotted for resume and hashed into the agent cache key).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.builtin_tools.ask_user_question import SYSTEM_PROMPT_GUIDANCE
from agents.main_agent.chat_agent import ChatAgent


def _tool(name: str):
    return SimpleNamespace(tool_spec={"name": name, "description": "", "inputSchema": {}})


def _agent(base_prompt: str = "BASE PROMPT") -> ChatAgent:
    """A ChatAgent shell with just enough state for the prompt helper.

    `__new__` rather than a constructor: BaseAgent.__init__ builds a registry,
    a session manager and a Strands agent, none of which this unit needs.
    """
    agent = ChatAgent.__new__(ChatAgent)
    agent.system_prompt = base_prompt
    return agent


class TestGating:
    """Req: the clause appears exactly when the tool does."""

    def test_appended_when_the_tool_is_present(self):
        prompt = _agent()._system_prompt_for([_tool("browse_web"), _tool("ask_user_question")])

        assert prompt.startswith("BASE PROMPT")
        assert SYSTEM_PROMPT_GUIDANCE in prompt

    def test_absent_when_the_tool_is_not(self):
        """A user without the tool must not be told to call it."""
        prompt = _agent()._system_prompt_for([_tool("browse_web"), _tool("calculator")])

        assert prompt == "BASE PROMPT"
        assert SYSTEM_PROMPT_GUIDANCE not in prompt

    def test_absent_for_a_turn_with_no_tools(self):
        assert _agent()._system_prompt_for([]) == "BASE PROMPT"

    def test_appended_only_once(self):
        """Guards a duplicate slipping in if the list ever carries the tool
        twice — the clause is prefix bytes, and two copies is waste."""
        prompt = _agent()._system_prompt_for(
            [_tool("ask_user_question"), _tool("ask_user_question")]
        )

        assert prompt.count(SYSTEM_PROMPT_GUIDANCE) == 1

    def test_tolerates_tools_without_a_spec(self):
        """Injected/factory tools are ordinary objects; a malformed entry must
        not take down agent construction."""
        prompt = _agent()._system_prompt_for(
            [object(), SimpleNamespace(tool_spec=None), _tool("ask_user_question")]
        )

        assert SYSTEM_PROMPT_GUIDANCE in prompt


class TestItKeysOnTheFilteredList:
    """Req: keyed on what reaches `toolConfig`, not on what was requested.

    `ToolFilter` drops a catalog id the registry does not know — `canvas_faculty`
    does this in dev today, with a "not a known tool id, skipping" log line. A
    check against the request's `enabled_tools` would advertise a tool that is
    not in the turn's `toolConfig` at all.
    """

    def test_a_requested_but_dropped_tool_does_not_add_the_clause(self):
        agent = _agent()
        # What the request asked for is irrelevant; only the filtered list counts.
        agent.enabled_tools = ["ask_user_question", "canvas_faculty"]

        prompt = agent._system_prompt_for([_tool("browse_web")])

        assert SYSTEM_PROMPT_GUIDANCE not in prompt


class TestItDoesNotDisturbResumeOrTheCacheKey:
    """Req: `self.system_prompt` is left alone.

    That field is snapshotted into `PausedTurnSnapshot` and hashed into the
    agent cache key. The clause is derived from `enabled_tools`, which the key
    already covers via `tools_hash`, so mutating it would move resume onto a
    different cache slot for no benefit — the bug class called out in
    `base_agent.py`'s snapshot comment.
    """

    def test_self_system_prompt_is_unchanged(self):
        agent = _agent()

        agent._system_prompt_for([_tool("ask_user_question")])

        assert agent.system_prompt == "BASE PROMPT"

    def test_repeated_calls_do_not_accumulate(self):
        agent = _agent()

        first = agent._system_prompt_for([_tool("ask_user_question")])
        second = agent._system_prompt_for([_tool("ask_user_question")])

        assert first == second
        assert second.count(SYSTEM_PROMPT_GUIDANCE) == 1


class TestKillSwitch:
    """Req: `ASK_USER_QUESTION_ENABLED=false` silences the guidance too.

    No second flag check is needed — while off the tool is never registered, so
    it cannot appear in the filtered list. This pins that the two stay coupled.
    """

    def test_disabled_registry_yields_no_tool_and_so_no_guidance(self, monkeypatch):
        monkeypatch.setenv("ASK_USER_QUESTION_ENABLED", "false")
        from agents.main_agent.tools.tool_registry import create_default_registry
        from agents.main_agent.tools.tool_filter import ToolFilter

        registry = create_default_registry()
        tools, _ = ToolFilter(registry).filter_tools(["calculator", "ask_user_question"])

        assert all(t.tool_spec["name"] != "ask_user_question" for t in tools)
        assert SYSTEM_PROMPT_GUIDANCE not in _agent()._system_prompt_for(tools)


class TestTheTextItself:
    """Req: the wording is what was measured.

    Not a style assertion — the measured lift (4/24 -> 24/24) belongs to these
    sentences in this position. The same idea in the tool description scored at
    baseline, so a well-meant reword can silently cost the whole feature.
    """

    def test_names_the_tool_so_the_model_can_act_on_it(self):
        assert "ask_user_question" in SYSTEM_PROMPT_GUIDANCE

    def test_is_short_enough_to_be_free(self):
        # ~70 tokens against a prefix measured at ~24k. If this grows into a
        # paragraph, re-price it before shipping.
        assert len(SYSTEM_PROMPT_GUIDANCE) < 400

    def test_is_guidance_not_a_mandate(self):
        """"Prefer" and "when" keep it conditional. An unconditional "always
        ask" is what the 0/18 clear-prompt result would have lost."""
        assert "Prefer" in SYSTEM_PROMPT_GUIDANCE
        assert "always" not in SYSTEM_PROMPT_GUIDANCE.lower()


class TestItReachesTheStrandsAgent:
    """Req: `_create_agent` actually hands the augmented prompt to the factory.

    The unit tests above prove the helper computes the right string; this proves
    the string is the one the model receives. A refactor that reverts the call
    site to `self.system_prompt` would leave every test above green while the
    feature quietly stops working.
    """

    def _build(self, monkeypatch, tool_names):
        from agents.main_agent.core import AgentFactory

        captured = {}

        def fake_create_agent(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(AgentFactory, "create_agent", staticmethod(fake_create_agent))

        agent = ChatAgent.__new__(ChatAgent)
        agent.system_prompt = "BASE PROMPT"
        agent.model_config = MagicMock()
        agent.session_manager = MagicMock()
        agent._accessible_skill_ids = None
        monkeypatch.setattr(
            ChatAgent, "_build_filtered_tools",
            lambda self: [_tool(n) for n in tool_names], raising=False,
        )
        monkeypatch.setattr(ChatAgent, "_create_hooks", lambda self: [], raising=False)

        agent._create_agent()
        return captured

    def test_factory_receives_the_guidance_when_the_tool_is_present(self, monkeypatch):
        captured = self._build(monkeypatch, ["browse_web", "ask_user_question"])

        assert SYSTEM_PROMPT_GUIDANCE in captured["system_prompt"]

    def test_factory_receives_the_plain_prompt_otherwise(self, monkeypatch):
        captured = self._build(monkeypatch, ["browse_web"])

        assert captured["system_prompt"] == "BASE PROMPT"
