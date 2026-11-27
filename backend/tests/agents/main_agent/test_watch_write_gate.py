"""A watch run asks before it writes (docs/specs/proactive-trust.md §7.5, PR 4.2).

- a ``write``-class call no other gate pauses (``destructiveHint``, the catalog's
  ``writes``) pauses as an ordinary ``tool_approval_required`` interrupt;
- ``needs_approval`` and the skill-authoring writes are left to the approval gate;
- ``unclassified`` and read-only calls run;
- ``actWithoutAsking`` exempts one tool on one server, never its neighbours;
- an approved answer lets the call run, a declined one cancels it;
- the paused snapshot records the run, so a resume rebuilds the same gate.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.core.action_audit import RUN_STATE_KEY, ToolFacts, UnattendedRun
from agents.main_agent.session.hooks.watch_write_gate import WatchWriteGate, exemption_key
from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

pytestmark = pytest.mark.asyncio


def _tool(name, *, destructive=None, read_only=None, server="mail-mcp"):
    annotations = SimpleNamespace(readOnlyHint=read_only, destructiveHint=destructive)
    return SimpleNamespace(tool_name=name, mcp_tool=SimpleNamespace(name=name, annotations=annotations), mcp_client=SimpleNamespace(server=server))


FACTS = ToolFacts(
    approval_names=lambda tool: {"archive"},
    write_names=lambda tool: {"create_draft"},
    tool_id=lambda tool: tool.mcp_client.server,
    skill_write_names=frozenset({"my_skill_create"}),
)


def _gate(exempt=()):
    return WatchWriteGate(lambda: FACTS, exempt)


@pytest.mark.parametrize(
    "tool, name, gated",
    [
        (_tool("send", destructive=True), "send", True),
        (_tool("create_draft"), "create_draft", True),
        (_tool("archive"), "archive", False),  # the approval gate's
        (SimpleNamespace(tool_name="my_skill_create"), "my_skill_create", False),  # the approval gate's
        (_tool("list_files"), "list_files", False),  # unclassified: audited, not gated
        (_tool("list_mail", read_only=True), "list_mail", False),
        (SimpleNamespace(tool_name="report"), "report", False),
    ],
)
async def test_what_it_gates(tool, name, gated):
    assert _gate().should_gate(tool, name) is gated


async def test_an_exemption_names_one_tool_on_one_server():
    gate = _gate(["mail-mcp/create_draft"])
    assert gate.should_gate(_tool("create_draft"), "create_draft") is False
    assert gate.should_gate(_tool("send", destructive=True), "send") is True
    assert gate.should_gate(_tool("create_draft", server="other-mcp"), "create_draft") is True


async def test_a_gateway_tools_exemption_uses_its_own_name():
    tool = SimpleNamespace(tool_name="mail___send", mcp_tool=SimpleNamespace(name="mail___send", annotations=None))
    facts = ToolFacts(tool_id=lambda t: "gateway_mail")
    assert exemption_key(tool, "mail___send", facts) == "gateway_mail/send"


class FakeEvent:
    def __init__(self, tool, name, response=None):
        self.selected_tool = tool
        self.tool_use = {"toolUseId": "tu-1", "name": name, "input": {"to": "x"}}
        self.cancel_tool = None
        self.interrupts = []
        self._response = response

    def interrupt(self, name, reason=None):
        self.interrupts.append((name, reason))
        if self._response is None:
            raise RuntimeError("paused")
        return self._response


async def test_it_pauses_as_an_ordinary_approval():
    event = FakeEvent(_tool("send", destructive=True), "send")
    with pytest.raises(RuntimeError, match="paused"):
        await _gate()._approval._gate(event)
    [(name, reason)] = event.interrupts
    assert name == "tool_approval:tu-1"
    assert reason["type"] == "tool_approval_required" and reason["toolName"] == "send"
    assert "watcher" in reason["message"]


async def test_approved_runs_and_declined_cancels():
    approved = FakeEvent(_tool("send", destructive=True), "send", response="approved")
    await _gate()._approval._gate(approved)
    assert approved.cancel_tool is None
    declined = FakeEvent(_tool("send", destructive=True), "send", response="declined")
    await _gate()._approval._gate(declined)
    assert declined.cancel_tool


async def test_an_ungated_call_never_asks():
    event = FakeEvent(_tool("list_files"), "list_files")
    await _gate()._approval._gate(event)
    assert event.interrupts == []


async def test_register_adds_one_before_tool_call_hook():
    added = []
    agent = SimpleNamespace(add_hook=lambda callback, event_type: added.append(event_type.__name__))
    _gate().register(agent)
    assert added == ["BeforeToolCallEvent"]


async def test_the_paused_snapshot_records_the_run_from_the_requests_state():
    run = UnattendedRun(
        run_id="run-1", user_id="user-1", session_id="headless-1", trigger="schedule",
        schedule_id="sched-1", act_without_asking=("mail-mcp/create_draft",),
    )
    agent = SimpleNamespace(_interrupt_state=SimpleNamespace(activated=True))
    wrapper = SimpleNamespace(_construction_snapshot={"enabled_tools": [], "run_mode": "watch"})
    with patch("apis.shared.sessions.metadata.set_paused_turn", new=AsyncMock()) as write:
        await StreamCoordinator()._persist_paused_turn_snapshot(
            agent, "headless-1", "user-1", wrapper, invocation_state={RUN_STATE_KEY: run}
        )
    snapshot = write.await_args.args[2]
    assert (snapshot.run_id, snapshot.run_trigger, snapshot.run_schedule_id) == ("run-1", "schedule", "sched-1")
    assert snapshot.act_without_asking == ["mail-mcp/create_draft"]


async def test_an_interactive_pause_records_no_run():
    agent = SimpleNamespace(_interrupt_state=SimpleNamespace(activated=True))
    wrapper = SimpleNamespace(_construction_snapshot={"enabled_tools": []})
    with patch("apis.shared.sessions.metadata.set_paused_turn", new=AsyncMock()) as write:
        await StreamCoordinator()._persist_paused_turn_snapshot(agent, "sess-1", "user-1", wrapper)
    snapshot = write.await_args.args[2]
    assert snapshot.run_id is None and snapshot.run_trigger is None and snapshot.act_without_asking is None
