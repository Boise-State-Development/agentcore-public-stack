"""A cached agent's MCP session that died on its own reconnects on the next call.

The dev sequence (2026-10-18): GitHub's MCP endpoint answered a `tools/call` it
had already performed with 503. The MCP SDK ends the whole session on any
non-2xx POST, Strands' background thread exits, and nothing calls `stop()`. The
client is shared by every agent built over it and agents are cached across
turns, so every later model-driven call on it failed with "the client session
is not running" until the agent was evicted.

The integration tests drive the real loop: a real `Agent` holding a real
Strands `MCPClient` as its tool provider, against a real streamable-HTTP MCP
server that 503s. Only the model is scripted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from typing import Any, AsyncIterable
from unittest.mock import MagicMock

import pytest
from strands import Agent
from strands.models.model import Model
from strands.tools.mcp import MCPAgentTool, MCPClient

from agents.main_agent.session.hooks import mcp_session_heal as heal_mod
from agents.main_agent.session.hooks.mcp_session_heal import MCPSessionHealHook
from apis.shared.tools import mcp_session
from tests.fixtures.flaky_mcp_server import FlakyMCPServer


class _Client(MCPClient):
    """What this stack builds (`UICapableMCPClient`, `FilteredMCPClient`).

    Both were `ReleasesCallsOnSessionEnd, MCPClient` until strands-agents
    1.61.1; the mixin retired with the upstream fix (see the test below).
    """


class _ScriptedModel(Model):
    """Each turn: call `issue_write` with the next title, then answer in text."""

    def __init__(self, titles: list[str]) -> None:
        self._titles = list(titles)
        self.calls = 0

    def update_config(self, **model_config: Any) -> None:  # pragma: no cover
        pass

    def get_config(self) -> Any:  # pragma: no cover
        return {}

    def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, *args: Any, **kwargs: Any) -> AsyncIterable[dict]:
        self.calls += 1
        last = messages[-1]["content"] if messages else []
        answered = any("toolResult" in block for block in last)
        yield {"messageStart": {"role": "assistant"}}
        if answered or not self._titles:
            yield {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}}
            yield {
                "contentBlockDelta": {
                    "delta": {"text": "Done."},
                    "contentBlockIndex": 0,
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        title = self._titles.pop(0)
        yield {
            "contentBlockStart": {
                "start": {
                    "toolUse": {"name": "issue_write", "toolUseId": f"tu-{title}"}
                },
                "contentBlockIndex": 0,
            }
        }
        yield {
            "contentBlockDelta": {
                "delta": {"toolUse": {"input": json.dumps({"title": title})}},
                "contentBlockIndex": 0,
            }
        }
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def _agent(client: Any, titles: list[str], *, heal: bool = True) -> Agent:
    return Agent(
        model=_ScriptedModel(titles),
        tools=[client],
        hooks=[MCPSessionHealHook()] if heal else [],
        callback_handler=None,
    )


async def _turn(agent: Agent, prompt: str = "go") -> dict:
    """Run one turn; return the tool result it produced."""
    async for _ in agent.stream_async(prompt):
        pass
    results = [
        block["toolResult"]
        for message in agent.messages
        for block in (message.get("content") or [])
        if isinstance(block, dict) and "toolResult" in block
    ]
    return results[-1]


def _text(result: dict) -> str:
    return " ".join(block.get("text", "") for block in result["content"])


@contextlib.contextmanager
def _stopping(client: Any):
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            client.stop(None, None, None)


@pytest.mark.asyncio
async def test_a_call_whose_session_dies_resolves_rather_than_hangs():
    """Fixed upstream in strands-agents 1.58.0 (#4499); our workaround is gone.

    On 1.57.2 the call the 503 answered never returned: its `run_async` task
    was mid-cleanup when the dying session stopped the background loop, and the
    `cancel_signal` every agent passes made it lose that race every time, so
    the turn hung. #1659 added `ReleasesCallsOnSessionEnd` to keep the loop
    alive until those tasks resolved, and left this test to say when the
    workaround could go — which the 1.61.1 bump did.

    The plain SDK client is now the whole assertion. If a later SDK regresses,
    this times out and the mixin has to come back (it is in git history at the
    commit that removed it).
    """
    with FlakyMCPServer() as server:
        with _stopping(server.client(MCPClient)) as client:
            await client.load_tools()
            call = client.call_tool_async(
                "t-1", "issue_write", {"title": "MCPClient"},
                cancel_signal=threading.Event(),
            )
            result = await asyncio.wait_for(call, timeout=5)
            assert result["status"] == "error"
            assert "closed" in _text(result)
        assert server.writes == ["MCPClient"]


@pytest.mark.asyncio
async def test_without_healing_a_cached_agent_stays_on_the_dead_session():
    """The premise: a crashed shared session never comes back by itself."""
    with FlakyMCPServer() as server, _stopping(server.client(_Client)) as client:
        agent = _agent(client, ["first", "second"], heal=False)
        await _turn(agent)
        assert server.writes == ["first"]
        assert mcp_session.session_crashed(client)

        server.fail_calls = False
        result = await _turn(agent)

        assert result["status"] == "error"
        assert "not running" in _text(result)
        assert server.writes == ["first"]


@pytest.mark.asyncio
async def test_a_cached_agents_next_model_driven_call_reconnects():
    """The 503 kills the session; the same agent's next turn reconnects.

    The call the 503 answered is never re-run, and the model is told its
    outcome is unknown, so it does not retry a write that already happened
    now that a retry would succeed.
    """
    with FlakyMCPServer() as server, _stopping(server.client(_Client)) as client:
        agent = _agent(client, ["first", "second"])

        lost = await _turn(agent)
        assert server.writes == ["first"]
        assert lost["status"] == "error"
        assert "may have completed" in _text(lost)
        assert "issue_write" in _text(lost)
        assert mcp_session.session_crashed(client)

        server.fail_calls = False
        result = await _turn(agent)

        assert result["status"] == "success", _text(result)
        assert server.writes == ["first", "second"]
        assert mcp_session.session_is_active(client)
        # Still the agent's tool provider, still counting its registry.
        assert mcp_session.is_tool_provider(client)
        assert agent.tool_registry._registry_id in client._consumers


@pytest.mark.asyncio
async def test_a_new_agent_built_over_the_crashed_client_reconnects():
    """`load_tools()` hands a new agent the dead session's tool list.

    That list is still right — it is the cached agents' `toolConfig`, byte for
    byte — so the build keeps it, and the first call reconnects.
    """
    with FlakyMCPServer() as server, _stopping(server.client(_Client)) as client:
        first = _agent(client, ["first"])
        await _turn(first)
        assert mcp_session.session_crashed(client)

        server.fail_calls = False
        second = _agent(client, ["second"])
        assert sorted(second.tool_names) == sorted(first.tool_names)
        result = await _turn(second)

        assert result["status"] == "success", _text(result)
        assert server.writes == ["first", "second"]
        # Both registries still count, so collecting one agent cannot stop
        # the session under the other.
        assert first.tool_registry._registry_id in client._consumers
        assert second.tool_registry._registry_id in client._consumers


@pytest.mark.asyncio
async def test_a_failed_reconnect_is_tried_again_on_the_next_call():
    """A server still down when the agent reconnects fails that call only."""
    with FlakyMCPServer() as server, _stopping(server.client(_Client)) as client:
        agent = _agent(client, ["first", "second", "third"])
        await _turn(agent)
        assert mcp_session.session_crashed(client)

        server.down = True
        refused = await _turn(agent)
        assert refused["status"] == "error"
        # Never sent, so not an unknown outcome.
        assert "may have completed" not in _text(refused)
        assert server.writes == ["first"]
        assert mcp_session.is_tool_provider(client)

        server.down = False
        server.fail_calls = False
        result = await _turn(agent)
        assert result["status"] == "success", _text(result)
        assert server.writes == ["first", "third"]


# --- The hook in isolation -------------------------------------------------


class _Session:
    """Stands in for a Strands MCPClient's session bookkeeping."""

    def __init__(self, active: bool = True, start_raises: Exception | None = None):
        self.active = active
        self.start_raises = start_raises
        self.starts = 0
        self._background_thread = threading.current_thread() if active else None
        self._tool_provider_started = True
        self._consumers = {"registry-a"}
        self._loaded_tools = ["tool"]

    def _is_session_active(self) -> bool:
        return self.active

    def start(self):
        self.starts += 1
        if self.start_raises:
            raise self.start_raises
        self.active = True
        self._background_thread = threading.Thread(target=lambda: None)
        return self

    def stop(self, *_exc) -> None:
        self.active = False
        self._background_thread = None
        self._tool_provider_started = False
        self._consumers = set()
        self._loaded_tools = None


def _mcp_tool(client: Any) -> MCPAgentTool:
    tool = MagicMock(spec=MCPAgentTool)
    tool.mcp_client = client
    return tool


def _before(tool: Any, tool_use_id: str = "tu-1", cancel: Any = None):
    event = MagicMock()
    event.selected_tool = tool
    event.tool_use = {"name": "issue_write", "toolUseId": tool_use_id, "input": {}}
    event.cancel_tool = cancel
    return event


def _after(tool: Any, result: dict, tool_use_id: str = "tu-1"):
    event = MagicMock()
    event.selected_tool = tool
    event.tool_use = {"name": "issue_write", "toolUseId": tool_use_id, "input": {}}
    event.result = result
    return event


_ERROR = {"toolUseId": "tu-1", "status": "error", "content": [{"text": "boom"}]}


def test_a_healthy_session_is_not_touched(monkeypatch):
    client = _Session(active=True)
    monkeypatch.setattr(
        heal_mod.asyncio, "to_thread", MagicMock(side_effect=AssertionError)
    )
    asyncio.run(MCPSessionHealHook()._before_tool_call(_before(_mcp_tool(client))))
    assert client.starts == 0


def test_non_mcp_and_cancelled_calls_are_skipped():
    client = _Session(active=False)
    hook = MCPSessionHealHook()
    asyncio.run(hook._before_tool_call(_before(MagicMock())))
    asyncio.run(hook._before_tool_call(_before(_mcp_tool(client), cancel="declined")))
    assert client.starts == 0
    assert hook._session_at_call == {}


def test_a_dead_session_is_restarted_with_its_bookkeeping():
    client = _Session(active=False)
    client._background_thread = threading.Thread(target=lambda: None)  # dead
    asyncio.run(MCPSessionHealHook()._before_tool_call(_before(_mcp_tool(client))))
    assert client.starts == 1
    assert client._tool_provider_started is True
    assert client._consumers == {"registry-a"}
    assert client._loaded_tools == ["tool"]


def test_a_failed_restart_leaves_the_bookkeeping_for_the_next_try():
    client = _Session(active=False, start_raises=RuntimeError("503"))
    hook = MCPSessionHealHook()
    asyncio.run(hook._before_tool_call(_before(_mcp_tool(client))))
    assert client.starts == 1
    assert client._tool_provider_started is True
    assert client._consumers == {"registry-a"}
    assert client._loaded_tools == ["tool"]
    # Not recorded, so its failure is reported as Strands words it.
    event = _after(_mcp_tool(client), dict(_ERROR))
    hook._after_tool_call(event)
    assert event.result == _ERROR


def test_an_ordinary_error_on_a_healthy_session_is_left_alone():
    client = _Session(active=True)
    tool = _mcp_tool(client)
    hook = MCPSessionHealHook()
    asyncio.run(hook._before_tool_call(_before(tool)))
    event = _after(tool, dict(_ERROR))
    hook._after_tool_call(event)
    assert event.result == _ERROR
    assert hook._session_at_call == {}


def test_a_session_replaced_under_the_call_reports_an_unknown_outcome():
    """Another agent may reconnect the session before this call's After runs."""
    client = _Session(active=True)
    tool = _mcp_tool(client)
    hook = MCPSessionHealHook()
    asyncio.run(hook._before_tool_call(_before(tool)))
    client._background_thread = threading.Thread(target=lambda: None)  # a new one
    event = _after(tool, dict(_ERROR))
    hook._after_tool_call(event)
    assert event.result["status"] == "error"
    assert event.result["toolUseId"] == "tu-1"
    assert "may have completed" in event.result["content"][0]["text"]


def test_a_success_is_never_rewritten():
    client = _Session(active=True)
    tool = _mcp_tool(client)
    hook = MCPSessionHealHook()
    asyncio.run(hook._before_tool_call(_before(tool)))
    client.active = False
    ok = {"toolUseId": "tu-1", "status": "success", "content": [{"text": "ok"}]}
    event = _after(tool, dict(ok))
    hook._after_tool_call(event)
    assert event.result == ok
