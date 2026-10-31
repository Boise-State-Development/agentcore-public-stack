"""`client_tool_call` extraction from a paused turn.

Mirrors `test_user_question_events.py`: the extractor reads
`agent._interrupt_state` at the `done` event, emits one SSE event per
client-tool interrupt, ignores every other flavor, and persists a breadcrumb
best-effort.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


def _parse(sse: str) -> dict:
    data_line = next(line for line in sse.splitlines() if line.startswith("data: "))
    return json.loads(data_line[len("data: ") :])


def _coordinator() -> StreamCoordinator:
    return StreamCoordinator.__new__(StreamCoordinator)


def _agent(*interrupts, activated: bool = True):
    return SimpleNamespace(
        _interrupt_state=SimpleNamespace(
            activated=activated,
            interrupts={f"i-{n}": i for n, i in enumerate(interrupts)},
        )
    )


def _interrupt(reason, interrupt_id: str = "v1:tool_call:tu-1:abc"):
    return SimpleNamespace(id=interrupt_id, reason=reason)


def _reason(tool_name="run_command", arguments=None, tool_use_id="tu-1"):
    return {
        "type": "client_tool_call",
        "toolUseId": tool_use_id,
        "toolName": tool_name,
        "arguments": {"command": "ls"} if arguments is None else arguments,
    }


@pytest.fixture
def add_pending():
    with patch("apis.shared.sessions.metadata.add_pending_interrupt", new_callable=AsyncMock) as mock:
        yield mock


class TestEmission:
    @pytest.mark.asyncio
    async def test_emits_one_event_per_client_tool_interrupt(self, add_pending):
        events = await _coordinator()._extract_client_tool_call_events(
            _agent(_interrupt(_reason()), _interrupt(_reason("read_file", {"path": "x"}), "v1:tool_call:tu-2:def")),
            session_id="s",
            user_id="u",
        )
        assert len(events) == 2
        first = _parse(events[0])
        assert first == {
            "type": "client_tool_call",
            "interruptId": "v1:tool_call:tu-1:abc",
            "toolUseId": "tu-1",
            "toolName": "run_command",
            "arguments": {"command": "ls"},
        }
        assert events[0].startswith("event: client_tool_call\n")

    @pytest.mark.asyncio
    async def test_other_flavors_are_ignored(self, add_pending):
        events = await _coordinator()._extract_client_tool_call_events(
            _agent(_interrupt({"type": "user_question_required", "questions": []}), _interrupt({"type": "oauth_required"})),
            session_id="s",
            user_id="u",
        )
        assert events == []
        add_pending.assert_not_called()

    @pytest.mark.asyncio
    async def test_nothing_when_not_paused(self, add_pending):
        assert await _coordinator()._extract_client_tool_call_events(_agent(activated=False), "s", "u") == []
        assert await _coordinator()._extract_client_tool_call_events(SimpleNamespace(), "s", "u") == []

    @pytest.mark.asyncio
    async def test_a_nameless_interrupt_is_dropped(self, add_pending):
        events = await _coordinator()._extract_client_tool_call_events(_agent(_interrupt(_reason(tool_name=""))), "s", "u")
        assert events == []

    @pytest.mark.asyncio
    async def test_non_dict_arguments_become_empty(self, add_pending):
        [sse] = await _coordinator()._extract_client_tool_call_events(_agent(_interrupt(_reason(arguments="junk"))), "s", "u")
        assert _parse(sse)["arguments"] == {}


class TestPersistence:
    @pytest.mark.asyncio
    async def test_breadcrumb_carries_the_call(self, add_pending):
        await _coordinator()._extract_client_tool_call_events(_agent(_interrupt(_reason())), session_id="s", user_id="u")
        add_pending.assert_awaited_once()
        pending = add_pending.call_args.kwargs["interrupt"]
        assert pending.kind == "client_tool"
        assert pending.tool_name == "run_command"
        assert json.loads(pending.tool_input) == {"command": "ls"}
        assert pending.interrupt_id == "v1:tool_call:tu-1:abc"

    @pytest.mark.asyncio
    async def test_a_failed_write_still_emits(self, add_pending):
        add_pending.side_effect = RuntimeError("dynamo down")
        events = await _coordinator()._extract_client_tool_call_events(_agent(_interrupt(_reason())), "s", "u")
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_no_write_without_a_session(self, add_pending):
        events = await _coordinator()._extract_client_tool_call_events(_agent(_interrupt(_reason())), None, None)
        assert len(events) == 1
        add_pending.assert_not_called()


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_paused_turn_snapshot_carries_the_client_tools(self):
        """The resume path rebuilds the tools from this field; without it the
        resumed tool body does not exist and the resume fails."""
        specs = json.dumps([{"name": "run_command", "description": "d", "inputSchema": {"type": "object", "properties": {}}}])
        wrapper = SimpleNamespace(_construction_snapshot={"enabled_tools": None, "model_id": "m", "client_tools": specs})
        agent = _agent(_interrupt(_reason()))
        with patch("apis.shared.sessions.metadata.set_paused_turn", new_callable=AsyncMock) as set_paused:
            await _coordinator()._persist_paused_turn_snapshot(agent, session_id="s", user_id="u", main_agent_wrapper=wrapper)
        snapshot = set_paused.call_args.args[2]
        assert snapshot.client_tools == specs
