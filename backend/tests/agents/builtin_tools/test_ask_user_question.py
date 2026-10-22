"""The `ask_user_question` tool: pause, then answer.

The tool raises its own Strands interrupt through `ToolContext` rather than
from a hook, because here the pause *is* the tool. `ToolContext.interrupt`
re-raises until a response is present and returns it afterwards, so a single
call site covers both passes — these tests drive each pass separately.

Coverage is the reason payload (the SSE contract downstream reads it verbatim),
the fail-fast on malformed model output, and the kill switch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.builtin_tools.ask_user_question import (
    INTERRUPT_NAME,
    ask_user_question,
)

# The @tool decorator wraps the function; call the original for unit tests.
_fn = ask_user_question._tool_func  # type: ignore[attr-defined]


def _context(interrupt_response: Any = None, tool_use_id: str = "tu-1"):
    context = MagicMock()
    context.tool_use = {"toolUseId": tool_use_id, "name": "ask_user_question"}
    context.interrupt = MagicMock(return_value=interrupt_response)
    return context


VALID = [
    {
        "header": "Scope",
        "question": "How much should this cover?",
        "options": [
            {"label": "Just the API", "description": "Routes only"},
            {"label": "Everything"},
        ],
    }
]


class TestPause:
    """Req: a valid call pauses the turn with a renderable reason payload."""

    def test_interrupt_is_raised_with_the_tool_scoped_name(self):
        context = _context(interrupt_response={"skipped": True})
        _fn(questions=VALID, tool_context=context)

        context.interrupt.assert_called_once()
        assert context.interrupt.call_args.kwargs["name"] == INTERRUPT_NAME

    def test_reason_payload_is_the_sse_contract(self):
        """`_extract_user_question_required_events` reads these keys verbatim;
        a rename here silently stops the prompt from ever reaching the SPA."""
        context = _context(interrupt_response={"skipped": True})
        _fn(questions=VALID, tool_context=context)

        reason = context.interrupt.call_args.kwargs["reason"]
        assert reason["type"] == "user_question_required"
        assert reason["toolUseId"] == "tu-1"
        [question] = reason["questions"]
        assert question["header"] == "Scope"
        assert question["multiSelect"] is False
        assert question["options"][0]["label"] == "Just the API"
        # exclude_none: an option with no description must not ship a null the
        # SPA would have to special-case.
        assert "description" not in question["options"][1]

    def test_malformed_questions_never_pause_the_turn(self):
        """The worst outcome is a pause behind a prompt nothing can draw, so
        rejection happens before the interrupt, while the model can still fix
        it."""
        context = _context()
        result = _fn(questions=[{"question": "no options?"}], tool_context=context)

        context.interrupt.assert_not_called()
        assert result["status"] == "error"


class TestResume:
    """Req: the client's answer becomes the model-visible tool result."""

    def test_selections_reach_the_model(self):
        context = _context(
            interrupt_response={"answers": {"Scope": {"selected": ["Everything"]}}}
        )
        result = _fn(questions=VALID, tool_context=context)

        assert result["status"] == "success"
        assert "- Scope: Everything" in result["content"][0]["text"]

    def test_skip_resolves_the_call_instead_of_erroring(self):
        """A skip is an answer, not a failure: the turn continues and the model
        is told to proceed on its own judgement."""
        context = _context(interrupt_response={"skipped": True})
        result = _fn(questions=VALID, tool_context=context)

        assert result["status"] == "success"
        assert "skipped" in result["content"][0]["text"]

    def test_unrecognized_response_still_resolves(self):
        """Fail *open*, unlike tool approval. There the safe default is to not
        run the tool; here the safe default is to not leave the user stuck."""
        context = _context(interrupt_response={"totally": "unexpected"})
        result = _fn(questions=VALID, tool_context=context)

        assert result["status"] == "success"


class TestKillSwitch:
    def test_disabled_returns_an_error_without_pausing(self, monkeypatch):
        monkeypatch.setenv("ASK_USER_QUESTION_ENABLED", "false")
        context = _context()

        result = _fn(questions=VALID, tool_context=context)

        context.interrupt.assert_not_called()
        assert result["status"] == "error"
        assert "disabled" in result["content"][0]["text"]

    @pytest.mark.parametrize("value", ["", "true", "TRUE", "1", "anything"])
    def test_anything_but_false_is_enabled(self, monkeypatch, value):
        monkeypatch.setenv("ASK_USER_QUESTION_ENABLED", value)
        context = _context(interrupt_response={"skipped": True})

        _fn(questions=VALID, tool_context=context)

        context.interrupt.assert_called_once()


class TestRegistration:
    """Req: the kill switch keeps the tool out of `toolConfig` entirely."""

    def test_registered_by_default(self, monkeypatch):
        monkeypatch.delenv("ASK_USER_QUESTION_ENABLED", raising=False)
        from agents.main_agent.tools.tool_registry import create_default_registry

        assert create_default_registry().has_tool("ask_user_question")

    def test_absent_when_disabled(self, monkeypatch):
        monkeypatch.setenv("ASK_USER_QUESTION_ENABLED", "false")
        from agents.main_agent.tools.tool_registry import create_default_registry

        assert not create_default_registry().has_tool("ask_user_question")
