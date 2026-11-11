"""MCP form elicitation over a real wire, through a real Strands event loop.

The two things this feature rests on, which no mock can prove
(``docs/specs/mcp-elicitation.md`` §8):

1. **A middleware interrupt resumes.** Hook interrupts (``v1:…``) and tool
   interrupts (``v1:tool_call:…``) are proven through the resume path;
   ``v1:middleware_execute_tool:…`` was not. Here the turn pauses, resumes, and
   a call that asks twice pauses twice and keeps its first answer.
2. **The slot reaches the callback.** The callback runs on the MCP client's
   background loop; the contextvar the middleware sets must be copied there.

The server is the SDK's own 2.x ``MCPServer`` asking through its resolver
(``Elicit``), which on a 2026-07-28 connection answers ``tools/call`` with an
``InputRequiredResult`` — the shape GitHub's ``delete_repository`` uses. Only
the model is scripted.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, AsyncIterable, List

import pytest
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Elicit, Resolve
from pydantic import BaseModel
from strands import Agent
from strands.models.model import Model

from agents.main_agent.integrations.mcp_apps import UICapableMCPClient
from agents.main_agent.integrations.mcp_elicitation import (
    McpElicitationPlugin,
    elicitation_callback,
)
from agents.main_agent.integrations.mcp_transport import streamable_http_transport
from apis.shared.mcp_elicitation.forms import ELICITATION_EVENT_TYPE
from tests.fixtures.flaky_mcp_server import FlakyMCPServer

REPO = "acme/scratch"


class Confirm(BaseModel):
    repository_name: str


class Why(BaseModel):
    why: str


# Module level: the server reads the tools' annotations, and this module's
# `from __future__ import annotations` makes them strings it resolves by name.
def ask_name(repo: str):
    return Elicit(f'Type "{repo}" to confirm permanent deletion.', Confirm)


def ask_why(repo: str):
    return Elicit("Why delete it?", Why)


def _setup(deleted: List[Any]):
    def setup(server):
        @server.tool()
        def delete_repository(
            repo: str, answer: Annotated[ElicitationResult[Confirm], Resolve(ask_name)]
        ) -> str:
            if isinstance(answer, AcceptedElicitation) and answer.data.repository_name == repo:
                deleted.append(repo)
                return f"deleted {repo}"
            return f"{repo} was not deleted"

        @server.tool()
        def delete_with_reason(
            repo: str,
            answer: Annotated[ElicitationResult[Confirm], Resolve(ask_name)],
            why: Annotated[ElicitationResult[Why], Resolve(ask_why)],
        ) -> str:
            if isinstance(answer, AcceptedElicitation) and isinstance(why, AcceptedElicitation):
                deleted.append((repo, why.data.why))
                return f"deleted {repo}"
            return f"{repo} was not deleted"

        @server.tool()
        def list_repositories() -> str:
            return "acme/scratch"

    return setup


class _ScriptedModel(Model):
    """Calls one tool (or several at once), then answers in text."""

    def __init__(self, *tool_calls: str) -> None:
        self.tool_calls = tool_calls or ("delete_repository",)
        self.calls = 0

    def update_config(self, **model_config: Any) -> None:  # pragma: no cover
        pass

    def get_config(self) -> Any:  # pragma: no cover
        return {}

    def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterable[dict]:
        self.calls += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.calls == 1:
            for index, name in enumerate(self.tool_calls):
                arguments = {} if name == "list_repositories" else {"repo": f"{REPO}-{index}" if index else REPO}
                yield {
                    "contentBlockStart": {
                        "start": {"toolUse": {"name": name, "toolUseId": f"tu-{index}"}},
                        "contentBlockIndex": index,
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "delta": {"toolUse": {"input": json.dumps(arguments)}},
                        "contentBlockIndex": index,
                    }
                }
                yield {"contentBlockStop": {"contentBlockIndex": index}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}}
        yield {"contentBlockDelta": {"delta": {"text": "Done."}, "contentBlockIndex": 0}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


async def _run(agent: Agent, prompt: Any) -> None:
    async for _ in agent.stream_async(prompt):
        pass


def _pending(agent: Agent):
    return [i for i in agent._interrupt_state.interrupts.values() if i.response is None]


def _tool_results(agent: Agent) -> List[dict]:
    return [
        block["toolResult"]
        for message in agent.messages
        for block in (message.get("content") or [])
        if isinstance(block, dict) and "toolResult" in block
    ]


def _result_text(result: dict) -> str:
    return " ".join(block.get("text", "") for block in result.get("content") or [])


def _resume(agent: Agent, interrupt, response) -> None:
    asyncio.run(
        _run(agent, [{"interruptResponse": {"interruptId": interrupt.id, "response": response}}])
    )


@pytest.fixture
def deleted() -> List[Any]:
    return []


@pytest.fixture
def server(deleted):
    with FlakyMCPServer(fail_calls=False, setup=_setup(deleted)) as srv:
        yield srv


def _tool_calls(server) -> List[dict]:
    return [r for r in server.requests if r.get("method") == "tools/call"]


def _client(server, *, elicit: bool = True) -> UICapableMCPClient:
    kwargs = {"elicitation_callback": elicitation_callback} if elicit else {}
    client = UICapableMCPClient(lambda: streamable_http_transport(server.url), **kwargs)
    client.context_label = "GitHub"
    return client


def _agent(client, *tool_calls: str) -> Agent:
    return Agent(
        model=_ScriptedModel(*tool_calls),
        tools=client.list_tools_sync(),
        plugins=[McpElicitationPlugin()],
        callback_handler=None,
    )


class TestPause:
    def test_the_turn_pauses_on_the_servers_form(self, server, deleted):
        with _client(server) as client:
            agent = _agent(client)
            asyncio.run(_run(agent, "delete it"))

            assert agent._interrupt_state.activated
            (interrupt,) = _pending(agent)
            assert interrupt.id.startswith("v1:middleware_execute_tool:tu-0:")
            assert interrupt.reason["type"] == ELICITATION_EVENT_TYPE
            assert interrupt.reason["toolUseId"] == "tu-0"
            assert interrupt.reason["toolName"] == "delete_repository"
            assert interrupt.reason["serverName"] == "GitHub"
            assert interrupt.reason["message"] == f'Type "{REPO}" to confirm permanent deletion.'
            assert [f["name"] for f in interrupt.reason["fields"]] == ["repository_name"]
            assert deleted == []

    def test_no_tool_result_is_recorded_while_paused(self, server):
        with _client(server) as client:
            agent = _agent(client)
            asyncio.run(_run(agent, "delete it"))

            assert _tool_results(agent) == []

    def test_the_client_advertises_form_elicitation_only(self, server):
        with _client(server) as client:
            asyncio.run(_run(_agent(client), "delete it"))

        meta = _tool_calls(server)[0]["params"]["_meta"]
        capabilities = meta["io.modelcontextprotocol/clientCapabilities"]
        assert capabilities["elicitation"] == {"form": {}}

    def test_without_a_callback_nothing_is_advertised(self, server):
        with _client(server, elicit=False) as client:
            asyncio.run(_run(_agent(client, "list_repositories"), "list them"))

        meta = _tool_calls(server)[0]["params"]["_meta"]
        assert "elicitation" not in meta["io.modelcontextprotocol/clientCapabilities"]


class TestResume:
    def test_an_accepted_answer_reaches_the_server_and_the_call_completes(self, server, deleted):
        with _client(server) as client:
            agent = _agent(client)
            asyncio.run(_run(agent, "delete it"))
            (interrupt,) = _pending(agent)

            _resume(agent, interrupt, {"action": "accept", "content": {"repository_name": REPO}})

            assert deleted == [REPO]
            assert not agent._interrupt_state.activated
            (result,) = _tool_results(agent)
            assert result["status"] == "success"
            assert _result_text(result) == f"deleted {REPO}"

    def test_the_answer_never_enters_the_conversation(self, server):
        with _client(server) as client:
            agent = _agent(client)
            asyncio.run(_run(agent, "delete it"))
            (interrupt,) = _pending(agent)
            _resume(agent, interrupt, {"action": "accept", "content": {"repository_name": REPO}})

            history = json.dumps(agent.messages)
            assert "permanent deletion" not in history
            assert "repository_name" not in history

    def test_a_decline_is_the_servers_to_handle(self, server, deleted):
        with _client(server) as client:
            agent = _agent(client)
            asyncio.run(_run(agent, "delete it"))
            (interrupt,) = _pending(agent)

            _resume(agent, interrupt, {"action": "decline"})

            assert deleted == []
            (result,) = _tool_results(agent)
            assert _result_text(result) == f"{REPO} was not deleted"

    def test_an_answer_that_does_not_fit_is_never_forwarded(self, server, deleted):
        with _client(server) as client:
            agent = _agent(client)
            asyncio.run(_run(agent, "delete it"))
            (interrupt,) = _pending(agent)

            _resume(agent, interrupt, {"action": "accept", "content": {"password": "hunter2"}})

            assert deleted == []
            assert "hunter2" not in json.dumps(server.requests)
            (result,) = _tool_results(agent)
            assert _result_text(result) == f"{REPO} was not deleted"

    def test_a_call_that_asks_twice_pauses_twice_and_keeps_its_first_answer(
        self, server, deleted
    ):
        with _client(server) as client:
            agent = _agent(client, "delete_with_reason")
            asyncio.run(_run(agent, "delete it"))

            (first,) = _pending(agent)
            _resume(agent, first, {"action": "accept", "content": {"repository_name": REPO}})
            (second,) = _pending(agent)
            assert second.id != first.id
            assert second.reason["message"] == "Why delete it?"
            # The first answer is still in the state the next resume restores.
            assert agent._interrupt_state.interrupts[first.id].response is not None

            _resume(agent, second, {"action": "accept", "content": {"why": "cleanup"}})

            assert deleted == [(REPO, "cleanup")]
            assert not agent._interrupt_state.activated

    def test_two_calls_that_each_ask_pause_together(self, server, deleted):
        with _client(server) as client:
            agent = _agent(client, "delete_repository", "delete_repository")
            asyncio.run(_run(agent, "delete both"))

            pending = _pending(agent)
            assert sorted(i.reason["toolUseId"] for i in pending) == ["tu-0", "tu-1"]
            responses = [
                {
                    "interruptResponse": {
                        "interruptId": i.id,
                        "response": {
                            "action": "accept",
                            "content": {
                                "repository_name": REPO if i.reason["toolUseId"] == "tu-0" else f"{REPO}-1"
                            },
                        },
                    }
                }
                for i in pending
            ]
            asyncio.run(_run(agent, responses))

            assert sorted(deleted) == [REPO, f"{REPO}-1"]


class TestPassThrough:
    def test_a_tool_that_never_asks_runs_once_and_unchanged(self, server):
        with _client(server) as client:
            agent = _agent(client, "list_repositories")
            asyncio.run(_run(agent, "list them"))

            assert not agent._interrupt_state.activated
            (result,) = _tool_results(agent)
            assert _result_text(result) == "acme/scratch"
            assert len(_tool_calls(server)) == 1

    def test_a_client_without_the_callback_never_pauses(self, server, deleted):
        with _client(server, elicit=False) as client:
            agent = _agent(client)
            asyncio.run(_run(agent, "delete it"))

            assert not agent._interrupt_state.activated
            assert deleted == []
            (result,) = _tool_results(agent)
            assert result["status"] == "error"
