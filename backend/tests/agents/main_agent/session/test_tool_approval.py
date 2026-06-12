"""Tests for the per-tool approval hook.

The hook reads a per-MCP-server approval set from the integration cache and
pauses the agent (Strands interrupt) when a flagged tool is about to run.
The user's resume response either lets the call proceed ("approved") or
cancels it ("declined" — anything not "approved" fails closed).
"""

from typing import Any
from unittest.mock import MagicMock

from agents.main_agent.session.hooks.tool_approval import (
    FoldedToolApproval,
    MCPExternalApprovalHook,
)


def _make_event(tool_name: str, tool_use_id: str = "tu-1", interrupt_response: Any = None):
    """Mock BeforeToolCallEvent with a configurable interrupt() return value."""
    event = MagicMock()
    event.tool_use = {
        "name": tool_name,
        "toolUseId": tool_use_id,
        "input": {"foo": "bar"},
    }
    event.cancel_tool = None
    event.interrupt = MagicMock(return_value=interrupt_response)
    event.selected_tool = MagicMock()
    return event


class TestMCPExternalApprovalHook:
    """Req: per-tool approval gate uses real Strands interrupts and acts on
    the user's response, not a flag-and-continue pattern."""

    def test_unflagged_tool_runs_without_pause(self):
        hook = MCPExternalApprovalHook(approval_names_lookup=lambda _: set())
        event = _make_event("read_email")
        hook._gate(event)
        event.interrupt.assert_not_called()
        assert event.cancel_tool is None

    def test_tool_not_in_approval_set_runs_without_pause(self):
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"send_email"}
        )
        event = _make_event("read_email")
        hook._gate(event)
        event.interrupt.assert_not_called()
        assert event.cancel_tool is None

    def test_flagged_tool_with_approved_response_proceeds(self):
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"send_email"}
        )
        event = _make_event("send_email", interrupt_response="approved")
        hook._gate(event)

        event.interrupt.assert_called_once()
        # Approved → no cancel
        assert event.cancel_tool is None

    def test_flagged_tool_with_declined_response_cancels(self):
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"send_email"}
        )
        event = _make_event("send_email", interrupt_response="declined")
        hook._gate(event)

        event.interrupt.assert_called_once()
        assert event.cancel_tool is not None
        assert "send_email" in event.cancel_tool

    def test_unknown_response_treated_as_decline(self):
        """Fail closed: anything other than the literal "approved" string
        cancels. Guards against bug-introduced typos or partial responses."""
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"send_email"}
        )
        event = _make_event("send_email", interrupt_response={"unexpected": "shape"})
        hook._gate(event)

        assert event.cancel_tool is not None

    def test_interrupt_payload_carries_tool_name_and_input(self):
        """The frontend modal needs tool name and the args to render a
        meaningful prompt. The reason payload is the contract — assert it.
        ``toolInput`` ships as a JSON-encoded string so DynamoDB persistence
        doesn't coerce floats and the frontend can render it verbatim."""
        import json

        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"create_event"}
        )
        event = _make_event(
            "create_event", tool_use_id="tu-99", interrupt_response="approved"
        )
        hook._gate(event)

        call_kwargs = event.interrupt.call_args.kwargs
        reason = call_kwargs["reason"]
        assert reason["type"] == "tool_approval_required"
        assert reason["toolName"] == "create_event"
        assert reason["toolUseId"] == "tu-99"
        assert json.loads(reason["toolInput"]) == {"foo": "bar"}
        assert "message" in reason

    def test_empty_tool_input_serializes_as_none(self):
        """Empty input → None so the frontend skips the args affordance."""
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"create_event"}
        )
        event = _make_event("create_event", interrupt_response="approved")
        event.tool_use["input"] = {}
        hook._gate(event)

        reason = event.interrupt.call_args.kwargs["reason"]
        assert reason["toolInput"] is None

    def test_interrupt_name_disambiguates_parallel_calls(self):
        """Two parallel calls of the same tool must produce distinct
        interrupt names so the frontend can correlate per-prompt."""
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"create_event"}
        )
        event_a = _make_event(
            "create_event", tool_use_id="tu-A", interrupt_response="approved"
        )
        event_b = _make_event(
            "create_event", tool_use_id="tu-B", interrupt_response="approved"
        )

        hook._gate(event_a)
        hook._gate(event_b)

        name_a = event_a.interrupt.call_args.kwargs["name"]
        name_b = event_b.interrupt.call_args.kwargs["name"]
        assert name_a != name_b
        assert "tu-A" in name_a
        assert "tu-B" in name_b

    def test_register_hooks_subscribes_to_before_tool_call(self):
        from strands.hooks import BeforeToolCallEvent

        hook = MCPExternalApprovalHook(approval_names_lookup=lambda _: set())
        registry = MagicMock()
        hook.register_hooks(registry)
        registry.add_callback.assert_called_once()
        args = registry.add_callback.call_args.args
        assert args[0] is BeforeToolCallEvent


class TestApprovalHookSkillFoldedTools:
    """Regression guard for the skills-mode approval bypass.

    A skill-bound external MCP tool executes through the `skill_executor`
    meta-tool, so `selected_tool` is the executor (not an MCPAgentTool) and
    `approval_names_lookup` returns an empty set — and `tool_use["name"]`
    is "skill_executor", so the direct name match could never fire either.
    Before the `tool_use_approval_lookup` fallback existed, an admin's
    needs_approval=True flag was silently bypassed whenever the tool was
    bound to a skill: the call ran with no user prompt.
    """

    @staticmethod
    def _tool_use_lookup(tool_use: dict) -> FoldedToolApproval | None:
        # Stands in for skills/mcp_binding.make_folded_tool_approval_lookup
        # (covered by its own tests); resolves the executor's folded target
        # and applies the needs_approval check itself.
        if tool_use.get("name") != "skill_executor":
            return None
        tool_input = tool_use.get("input") or {}
        if tool_input.get("tool_name") != "gmail_send":
            return None
        return FoldedToolApproval(
            tool_name="gmail_send", tool_input=tool_input.get("tool_input")
        )

    def _executor_event(self, tool_name="gmail_send", interrupt_response=None):
        event = _make_event("skill_executor", tool_use_id="tu_skill")
        event.tool_use["input"] = {
            "skill_name": "gmail-for-employees",
            "tool_name": tool_name,
            "tool_input": {"to": "hr@example.com", "body": "resignation"},
        }
        event.interrupt = MagicMock(return_value=interrupt_response)
        return event

    def test_flagged_folded_tool_pauses_and_describes_inner_tool(self):
        """The interrupt's toolName/toolInput must describe the folded tool
        (gmail_send and its args) — not skill_executor — so the frontend
        dialog is meaningful. toolUseId stays the executor's, since that is
        the streamed tool_use block the frontend correlates with."""
        import json

        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: set(),  # executor isn't an MCPAgentTool
            tool_use_approval_lookup=self._tool_use_lookup,
        )
        event = self._executor_event(interrupt_response="approved")
        hook._gate(event)

        event.interrupt.assert_called_once()
        reason = event.interrupt.call_args.kwargs["reason"]
        assert reason["type"] == "tool_approval_required"
        assert reason["toolName"] == "gmail_send"
        assert reason["toolUseId"] == "tu_skill"
        assert json.loads(reason["toolInput"]) == {
            "to": "hr@example.com",
            "body": "resignation",
        }
        # Approved → executor proceeds.
        assert event.cancel_tool is None

    def test_declined_folded_tool_cancels_with_inner_tool_name(self):
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: set(),
            tool_use_approval_lookup=self._tool_use_lookup,
        )
        event = self._executor_event(interrupt_response="declined")
        hook._gate(event)

        assert event.cancel_tool is not None
        assert "gmail_send" in event.cancel_tool
        assert "skill_executor" not in event.cancel_tool

    def test_unflagged_folded_tool_runs_without_pause(self):
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: set(),
            tool_use_approval_lookup=self._tool_use_lookup,
        )
        event = self._executor_event(tool_name="gmail_search")
        hook._gate(event)

        event.interrupt.assert_not_called()
        assert event.cancel_tool is None

    def test_without_tool_use_lookup_executor_is_not_gated(self):
        """Plain ChatAgent wiring (no lookup) keeps the old behavior."""
        hook = MCPExternalApprovalHook(approval_names_lookup=lambda _: set())
        event = self._executor_event()
        hook._gate(event)

        event.interrupt.assert_not_called()
        assert event.cancel_tool is None

    def test_direct_path_takes_precedence_over_folded_lookup(self):
        """A directly-selected flagged tool interrupts via the direct path;
        the folded lookup must not be consulted for it."""

        def exploding_lookup(_tool_use: dict):
            raise AssertionError("folded lookup consulted on the direct path")

        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"send_email"},
            tool_use_approval_lookup=exploding_lookup,
        )
        event = _make_event("send_email", interrupt_response="approved")
        hook._gate(event)

        event.interrupt.assert_called_once()
        assert event.cancel_tool is None
