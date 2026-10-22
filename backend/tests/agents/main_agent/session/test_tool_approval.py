"""Tests for the per-tool approval hook.

The hook reads a per-MCP-server approval set from the integration cache and
pauses the agent (Strands interrupt) when a flagged tool is about to run.
The user's resume response either lets the call proceed ("approved") or
cancels it ("declined" — anything not "approved" fails closed).
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock

from agents.main_agent.session.hooks.tool_approval import (
    ApprovalDetails,
    MCPExternalApprovalHook,
)


def _gate(hook: MCPExternalApprovalHook, event) -> None:
    """`_gate` is async (the details lookup may do IO); run it to completion."""
    asyncio.run(hook._gate(event))


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
        _gate(hook, event)
        event.interrupt.assert_not_called()
        assert event.cancel_tool is None

    def test_tool_not_in_approval_set_runs_without_pause(self):
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"send_email"}
        )
        event = _make_event("read_email")
        _gate(hook, event)
        event.interrupt.assert_not_called()
        assert event.cancel_tool is None

    def test_flagged_tool_with_approved_response_proceeds(self):
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"send_email"}
        )
        event = _make_event("send_email", interrupt_response="approved")
        _gate(hook, event)

        event.interrupt.assert_called_once()
        # Approved → no cancel
        assert event.cancel_tool is None

    def test_flagged_tool_with_declined_response_cancels(self):
        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"send_email"}
        )
        event = _make_event("send_email", interrupt_response="declined")
        _gate(hook, event)

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
        _gate(hook, event)

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
        _gate(hook, event)

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
        _gate(hook, event)

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

        _gate(hook, event_a)
        _gate(hook, event_b)

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


class TestApprovalDetails:
    """The skill-authoring writes reuse this gate with their own card."""

    def test_details_replace_the_shown_input_and_message(self):
        import json

        async def details(tool_name, tool_input):
            return ApprovalDetails(tool_input={"reach": "2 shared agents", **tool_input}, message="custom")

        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"my_skill_update"},
            approval_details_lookup=details,
        )
        event = _make_event("my_skill_update", interrupt_response="approved")
        _gate(hook, event)

        reason = event.interrupt.call_args.kwargs["reason"]
        assert json.loads(reason["toolInput"]) == {"reach": "2 shared agents", "foo": "bar"}
        assert reason["message"] == "custom"
        # The tool still receives its real input.
        assert event.tool_use["input"] == {"foo": "bar"}

    def test_no_details_means_the_default_card(self):
        import json

        async def details(tool_name, tool_input):
            return None

        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"send_email"},
            approval_details_lookup=details,
        )
        event = _make_event("send_email", interrupt_response="approved")
        _gate(hook, event)

        reason = event.interrupt.call_args.kwargs["reason"]
        assert json.loads(reason["toolInput"]) == {"foo": "bar"}
        assert "configured to require user approval" in reason["message"]

    def test_a_failing_details_lookup_still_pauses(self):
        """Fail closed: a card detail can never be the reason a write skips approval."""

        async def details(tool_name, tool_input):
            raise RuntimeError("dynamodb down")

        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: {"my_skill_update"},
            approval_details_lookup=details,
        )
        event = _make_event("my_skill_update", interrupt_response="declined")
        _gate(hook, event)

        event.interrupt.assert_called_once()
        assert event.cancel_tool is not None

    def test_details_are_not_looked_up_for_an_ungated_call(self):
        calls = []

        async def details(tool_name, tool_input):
            calls.append(tool_name)
            return None

        hook = MCPExternalApprovalHook(
            approval_names_lookup=lambda _: set(),
            approval_details_lookup=details,
        )
        _gate(hook, _make_event("web_search"))
        assert calls == []
