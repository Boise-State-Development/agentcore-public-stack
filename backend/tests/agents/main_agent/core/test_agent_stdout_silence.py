"""Model output must never reach the process's stdout/stderr.

Strands installs ``PrintingCallbackHandler`` on any ``Agent`` built without an
explicit ``callback_handler``, and that handler ``print``s every streamed text
delta with ``end=""``. The runtime ships stdout to CloudWatch, so a turn's
response text landed in the AgentCore Runtime log group — user conversation
content in logs — and, being unterminated, glued itself onto the front of the
next EMF line, which CloudWatch then declines to extract as a metric.

These tests build a real ``Agent`` (not a mock of the class) so they fail if
the SDK default ever comes back, and stream a real turn through it.
"""

import asyncio
from typing import Any, AsyncIterable
from unittest.mock import patch

from strands.handlers.callback_handler import PrintingCallbackHandler, null_callback_handler
from strands.models import Model

from agents.main_agent.core.model_config import ModelConfig, ModelProvider

_SECRET_TEXT = "the user's private question about their grades"


class _TextModel(Model):
    """Streams one fixed text response."""

    def update_config(self, **model_config: Any) -> None:  # pragma: no cover
        pass

    def get_config(self) -> Any:  # pragma: no cover
        return {}

    def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterable[dict]:
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}}
        yield {"contentBlockDelta": {"delta": {"text": _SECRET_TEXT}, "contentBlockIndex": 0}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def _build_agent():
    from agents.main_agent.core.agent_factory import AgentFactory

    with patch("agents.main_agent.core.agent_factory.CountTokensBedrockModel", return_value=_TextModel()):
        return AgentFactory.create_agent(
            model_config=ModelConfig(model_id="anthropic.claude-3-sonnet", provider=ModelProvider.BEDROCK),
            system_prompt="You are a helpful assistant.",
            tools=[],
            session_manager=None,
        )


def test_factory_agent_installs_no_printing_callback_handler():
    agent = _build_agent()

    assert not isinstance(agent.callback_handler, PrintingCallbackHandler)
    assert agent.callback_handler is null_callback_handler


def test_streamed_turn_writes_no_model_text_to_stdout_or_stderr(capfd):
    agent = _build_agent()

    async def _drain() -> str:
        streamed = ""
        async for event in agent.stream_async("hello"):
            streamed += event.get("data", "") if isinstance(event, dict) else ""
        return streamed

    streamed = asyncio.run(_drain())
    out, err = capfd.readouterr()

    # The text still reaches the stream consumer — only the side channel is gone.
    assert streamed == _SECRET_TEXT
    assert _SECRET_TEXT not in out
    assert _SECRET_TEXT not in err

