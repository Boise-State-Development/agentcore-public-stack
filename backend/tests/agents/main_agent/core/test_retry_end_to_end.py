"""Retry behavior end to end, through a real Strands ``Agent``.

The unit tests prove the predicate; these prove the outcome a user sees.
Transient faults are rare and dev cannot reach the ``global.*`` GPT-6 profiles
that produced them in prod, so this is where the fault path gets exercised:
a scripted model fails on cue, the agent is wired exactly as
``AgentFactory`` wires it, and the assertions are on what streamed and how
many times the model was called.
"""

from typing import Any, AsyncGenerator, Dict, List

import httpx
import openai
import pytest
from strands import Agent
from strands.models import Model

from agents.main_agent.core.agent_factory import AgentFactory
from agents.main_agent.core.model_config import ModelConfig, ModelProvider, RetryConfig

_REQUEST = httpx.Request("POST", "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1/responses")
_SERVER_ERROR_TEXT = "The server had an error while processing your request. Sorry about that!"


def _server_fault() -> openai.APIError:
    """The in-stream fault GPT-6 Sol raised in prod: no HTTP status."""
    return openai.APIError(_SERVER_ERROR_TEXT, _REQUEST, body=None)


def _text_chunks(text: str) -> List[Dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {}}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        },
    ]


class _ScriptedModel(Model):
    """Plays one scripted step per model call: chunks, then an optional raise."""

    def __init__(self, steps: List[Dict[str, Any]]) -> None:
        self._steps = list(steps)
        self.calls = 0

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Dict[str, Any]:
        return {}

    async def structured_output(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        self.calls += 1
        step = self._steps.pop(0)
        for chunk in step.get("chunks", []):
            yield chunk
        if "raise" in step:
            raise step["raise"]


def _agent(model: _ScriptedModel, max_attempts: int = 4) -> Agent:
    """Wire the agent the way AgentFactory does, with zero backoff."""
    cfg = ModelConfig(
        model_id="global.openai.gpt-6-sol",
        provider=ModelProvider.BEDROCK_RESPONSES,
        retry_config=RetryConfig(sdk_max_attempts=max_attempts, sdk_initial_delay=0, sdk_max_delay=0),
    )
    strategy = AgentFactory._build_retry_strategy(cfg, model)
    return Agent(model=model, retry_strategy=strategy, callback_handler=None)


async def _run(agent: Agent) -> Dict[str, Any]:
    text: List[str] = []
    retries = 0
    error = None
    try:
        async for event in agent.stream_async("hi"):
            if isinstance(event.get("data"), str):
                text.append(event["data"])
            if "event_loop_throttled_delay" in event:
                retries += 1
    except Exception as exc:  # noqa: BLE001 - the outcome under test
        error = exc
    return {"text": "".join(text), "retries": retries, "error": error}


class TestRetryEndToEnd:
    @pytest.mark.asyncio
    async def test_fault_before_output_is_retried_and_answer_streams_once(self):
        model = _ScriptedModel([{"raise": _server_fault()}, {"chunks": _text_chunks("Hello")}])

        outcome = await _run(_agent(model))

        assert outcome["error"] is None
        assert model.calls == 2
        assert outcome["text"] == "Hello"
        # The SPA's model_retry notice is driven by this event.
        assert outcome["retries"] == 1

    @pytest.mark.asyncio
    async def test_fault_after_output_is_not_retried_and_nothing_duplicates(self):
        partial = _text_chunks("Hel")[:3]
        model = _ScriptedModel([{"chunks": partial, "raise": _server_fault()}, {"chunks": _text_chunks("Hello")}])

        outcome = await _run(_agent(model))

        assert model.calls == 1
        assert outcome["text"] == "Hel"
        assert isinstance(outcome["error"], openai.APIError)

    @pytest.mark.asyncio
    async def test_persistent_fault_stops_at_the_attempt_budget(self):
        """One layer, one budget: exactly sdk_max_attempts calls, never a multiple."""
        model = _ScriptedModel([{"raise": _server_fault()} for _ in range(10)])

        outcome = await _run(_agent(model, max_attempts=4))

        assert model.calls == 4
        assert outcome["retries"] == 3
        assert isinstance(outcome["error"], openai.APIError)

    @pytest.mark.asyncio
    async def test_request_error_is_not_retried(self):
        bad_request = openai.BadRequestError(
            "Invalid value for 'input'", response=httpx.Response(400, request=_REQUEST), body=None
        )
        model = _ScriptedModel([{"raise": bad_request}, {"chunks": _text_chunks("Hello")}])

        outcome = await _run(_agent(model))

        assert model.calls == 1
        assert outcome["error"] is bad_request
