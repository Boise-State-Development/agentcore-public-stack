"""ChatAgent.stream_async continuation-after-max_tokens behavior.

A `continue_truncated=True` call must NOT synthesize a new user prompt: it
forwards an empty-list prompt so Strands appends no message and the model
resumes the truncated assistant message already in restored history.
"""

import pytest

from agents.main_agent.chat_agent import ChatAgent


class _RecordingCoordinator:
    """Captures the prompt stream_async forwards to the coordinator."""

    def __init__(self):
        self.captured = {}

    async def stream_response(self, **kwargs):
        self.captured = kwargs
        if False:  # pragma: no cover - make this an async generator
            yield ""


class _ExplodingMultimodalBuilder:
    """build_prompt must never be called on the continuation path."""

    def build_prompt(self, message, files):  # noqa: D401
        raise AssertionError("multimodal build_prompt called on continuation path")


def _bare_chat_agent(coordinator, multimodal):
    agent = object.__new__(ChatAgent)
    agent.agent = object()  # truthy so _create_agent() is skipped
    agent.stream_coordinator = coordinator
    agent.multimodal_builder = multimodal
    agent.session_manager = object()
    agent.session_id = "sess-1"
    agent.user_id = "user-1"
    return agent


@pytest.mark.asyncio
async def test_continue_truncated_forwards_empty_list_prompt():
    coordinator = _RecordingCoordinator()
    agent = _bare_chat_agent(coordinator, _ExplodingMultimodalBuilder())

    async for _ in agent.stream_async(
        "this message text must be ignored",
        continue_truncated=True,
    ):
        pass

    assert coordinator.captured.get("prompt") == []


@pytest.mark.asyncio
async def test_normal_turn_still_uses_multimodal_builder():
    coordinator = _RecordingCoordinator()

    class _Builder:
        def build_prompt(self, message, files):
            return f"built:{message}"

    agent = _bare_chat_agent(coordinator, _Builder())

    async for _ in agent.stream_async("hello", continue_truncated=False):
        pass

    assert coordinator.captured.get("prompt") == "built:hello"
