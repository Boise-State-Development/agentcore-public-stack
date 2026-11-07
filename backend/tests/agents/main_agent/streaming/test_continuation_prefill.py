"""``stream_response`` never hands Strands a prefill on a model that refuses one.

Driven through the real ``stream_response``, stubbing only the agent, its
wrapper's model config and the session manager (the invocation-state suite's
harness). The coordinator is where the rewrite has to live: rewind,
compaction and the stale-interrupt reset settle the live list there, so the
tail it checks is the tail the model would see.
"""

from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator
from apis.shared.models.assistant_prefill import CONTINUATION_PROMPT


class _RecordingAgent:
    def __init__(self, messages: List[Dict[str, Any]]) -> None:
        self.messages = messages
        self.prompts: List[Any] = []

    def stream_async(self, prompt: Any, **kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        self.prompts.append(prompt)

        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            if False:  # pragma: no cover - an empty async generator
                yield {}

        return _gen()


class _SessionManager:
    cancelled = False
    turn_lease = None

    async def update_after_turn(self, input_tokens, current_messages=None):
        return None


def _wrapper(model_id: str) -> Any:
    return SimpleNamespace(model_config=SimpleNamespace(model_id=model_id))


def _partial_history() -> List[Dict[str, Any]]:
    return [
        {"role": "user", "content": [{"text": "Write a long report."}]},
        {"role": "assistant", "content": [{"text": "Section one of the report"}]},
    ]


async def _run(agent: _RecordingAgent, prompt: Any, model_id: str) -> None:
    async for _ in StreamCoordinator().stream_response(
        agent=agent,
        prompt=prompt,
        session_manager=_SessionManager(),
        session_id="sess-1",
        user_id="user-1",
        main_agent_wrapper=_wrapper(model_id),
    ):
        pass


@pytest.mark.asyncio
async def test_continue_on_a_prefill_refusing_model_sends_a_user_turn() -> None:
    history = _partial_history()
    agent = _RecordingAgent(history)

    await _run(agent, [], "global.anthropic.claude-sonnet-5")

    assert agent.prompts == [CONTINUATION_PROMPT]
    # The stored history is not edited; Strands appends the prompt after it.
    assert agent.messages == _partial_history()


@pytest.mark.asyncio
async def test_continue_on_a_prefill_capable_model_keeps_the_empty_prompt() -> None:
    agent = _RecordingAgent(_partial_history())

    await _run(agent, [], "us.anthropic.claude-haiku-4-5-20251001-v1:0")

    assert agent.prompts == [[]]


@pytest.mark.asyncio
async def test_a_fresh_turn_is_untouched() -> None:
    agent = _RecordingAgent(_partial_history())

    await _run(agent, "next question", "global.anthropic.claude-sonnet-5")

    assert agent.prompts == ["next question"]
