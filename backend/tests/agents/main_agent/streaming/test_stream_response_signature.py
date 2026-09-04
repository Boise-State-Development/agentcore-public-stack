"""The `ChatAgent.stream_async` → `StreamCoordinator.stream_response` seam.

`turn_agent_id` (#756) was threaded through `_store_message_metadata`, `ChatAgent`,
`base_agent` and `voice_agent`, and forwarded from inside `stream_response`'s own body —
but never added to `stream_response`'s **signature**. Every invocation raised
``TypeError: StreamCoordinator.stream_response() got an unexpected keyword argument
'turn_agent_id'`` before the model was reached, so 100% of chat turns 500'd and AgentCore
surfaced 424. It reproduced with and without an Agent attached.

⚠️ **The stub below binds against the real signature on purpose.** The existing coordinator
stubs take bare ``**kwargs``, which silently accepts arguments the real coordinator rejects
— a stub shaped like the *caller* rather than the *callee* cannot fail on caller/callee
drift, which is precisely how this shipped CI-green. Binding is what makes these tests able
to fail; do not "simplify" it back to ``**kwargs``.
"""

import inspect

import pytest

from agents.main_agent.chat_agent import ChatAgent
from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


class _SignatureFaithfulCoordinator:
    """Records forwarded kwargs, after checking the real coordinator would accept them."""

    _real_signature = inspect.signature(StreamCoordinator.stream_response)

    def __init__(self):
        self.captured = {}

    async def stream_response(self, **kwargs):
        # Raises TypeError on any drift between what ChatAgent sends and what
        # StreamCoordinator declares — the production failure, in-process.
        self._real_signature.bind(self, **kwargs)
        self.captured = kwargs
        if False:  # pragma: no cover - make this an async generator
            yield ""


class _PassthroughMultimodalBuilder:
    def build_prompt(self, message, files, attachment_names=None):
        return message


def _bare_chat_agent(coordinator):
    """A ChatAgent with the real forwarding code but no Strands agent construction."""
    agent = object.__new__(ChatAgent)
    agent.agent = object()  # truthy so _create_agent() is skipped
    agent.stream_coordinator = coordinator
    agent.multimodal_builder = _PassthroughMultimodalBuilder()
    agent.session_manager = object()
    agent.session_id = "sess-1"
    agent.user_id = "user-1"
    return agent


def test_stream_response_accepts_turn_agent_id():
    """The signature itself — the one line whose absence took chat down."""
    params = inspect.signature(StreamCoordinator.stream_response).parameters

    assert "turn_agent_id" in params, (
        "stream_response() must accept turn_agent_id: ChatAgent.stream_async forwards it "
        "and stream_response's own body passes it to _store_message_metadata."
    )
    assert params["turn_agent_id"].default is None, (
        "turn_agent_id must default to None — voice and non-mention turns omit it."
    )


@pytest.mark.asyncio
async def test_stream_async_forwards_turn_agent_id_the_coordinator_accepts():
    """The seam end-to-end: a mention turn binds cleanly against the real signature."""
    coordinator = _SignatureFaithfulCoordinator()
    agent = _bare_chat_agent(coordinator)

    async for _ in agent.stream_async("summarize this", turn_agent_id="ast-canvas"):
        pass

    assert coordinator.captured.get("turn_agent_id") == "ast-canvas"


@pytest.mark.asyncio
async def test_plain_turn_binds_and_carries_no_agent_id():
    """A turn with no `@`-mention must still bind, and must not invent an agent id."""
    coordinator = _SignatureFaithfulCoordinator()
    agent = _bare_chat_agent(coordinator)

    async for _ in agent.stream_async("hello"):
        pass

    assert coordinator.captured.get("turn_agent_id") is None


@pytest.mark.asyncio
async def test_every_kwarg_chat_agent_forwards_is_accepted():
    """Generalises past this one parameter: no kwarg ChatAgent sends may be unknown.

    This is the assertion that would have caught #756's break on the day it landed, and
    catches the next one without anybody remembering to add a case for it.
    """
    coordinator = _SignatureFaithfulCoordinator()
    agent = _bare_chat_agent(coordinator)

    async for _ in agent.stream_async(
        "a message",
        session_id="sess-9",
        citations=[{"source": "doc-1"}],
        original_message="a message",
        turn_agent_id="ast-canvas",
        turn_lease=object(),
    ):
        pass

    accepted = set(inspect.signature(StreamCoordinator.stream_response).parameters)
    assert set(coordinator.captured) <= accepted
