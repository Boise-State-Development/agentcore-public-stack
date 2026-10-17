"""Per-request client data reaches Strands as ``invocation_state``, and only then.

The sign-in panel's frame height (authenticated-web-assessment spec D3a) rides
``invocation_state`` so tools can read it from ``ToolContext`` without it ever
entering the prompt. Two things are pinned here:

1. ``stream_response`` hands it to ``agent.stream_async`` when there is one.
2. When there is none it calls ``agent.stream_async(prompt)`` exactly as it
   always has, so a turn that carries nothing is byte-for-byte the old call.

Driven through the real ``stream_response``, stubbing only the agent and the
session manager (the steering-events suite's harness).
"""

from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


class _RecordingAgent:
    def __init__(self) -> None:
        self.messages = [{"role": "user", "content": [{"text": "hi"}]}]
        self.calls: List[Dict[str, Any]] = []

    def stream_async(self, prompt: Any, **kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
        self.calls.append({"prompt": prompt, **kwargs})

        async def _gen() -> AsyncIterator[Dict[str, Any]]:
            if False:  # pragma: no cover - an empty async generator
                yield {}

        return _gen()


class _SessionManager:
    cancelled = False
    turn_lease = None

    async def update_after_turn(self, input_tokens, current_messages=None):
        return None


async def _run(agent: _RecordingAgent, invocation_state: Optional[Dict[str, Any]]) -> None:
    async for _ in StreamCoordinator().stream_response(
        agent=agent,
        prompt="hi",
        session_manager=_SessionManager(),
        session_id="sess-1",
        user_id="user-1",
        invocation_state=invocation_state,
    ):
        pass


@pytest.mark.asyncio
async def test_invocation_state_reaches_the_strands_call() -> None:
    agent = _RecordingAgent()

    await _run(agent, {"browser_takeover_frame_height": 778})

    assert agent.calls == [
        {"prompt": "hi", "invocation_state": {"browser_takeover_frame_height": 778}}
    ]


@pytest.mark.asyncio
async def test_no_invocation_state_keeps_the_bare_call() -> None:
    agent = _RecordingAgent()

    await _run(agent, None)

    assert agent.calls == [{"prompt": "hi"}]
