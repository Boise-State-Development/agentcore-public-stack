"""Tests for the hook that defaults AgentCore Web Search's `maxResults` to 5.

The hook must fill in the count only when the model left it out, only for a
Gateway Web Search tool (under whatever target name an admin chose), and never
by writing into the tool use Strands hands it: that dict is the `toolUse`
block in the assistant message already in history, so a write there would
make the live conversation disagree with the stored one.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterable
from unittest.mock import MagicMock

import pytest
from strands import Agent, tool
from strands.models.model import Model

from agents.main_agent.session.hooks.web_search_defaults import (
    WEB_SEARCH_DEFAULT_MAX_RESULTS,
    WebSearchDefaultsHook,
    is_web_search_tool,
)


def _event(name: str, tool_input: Any, cancel: Any = False):
    event = MagicMock()
    event.tool_use = {"toolUseId": "tu-1", "name": name, "input": tool_input}
    event.cancel_tool = cancel
    return event


@pytest.fixture
def hook():
    return WebSearchDefaultsHook()


def test_omitted_max_results_becomes_five(hook):
    event = _event("web-search___WebSearch", {"query": "aurora forecast"})

    hook._before_tool_call(event)

    assert event.tool_use["input"] == {"query": "aurora forecast", "maxResults": 5}
    assert WEB_SEARCH_DEFAULT_MAX_RESULTS == 5


def test_explicit_max_results_passes_through(hook):
    event = _event("web-search___WebSearch", {"query": "aurora forecast", "maxResults": 8})
    original = event.tool_use

    hook._before_tool_call(event)

    assert event.tool_use is original
    assert event.tool_use["input"] == {"query": "aurora forecast", "maxResults": 8}


def test_other_tools_are_untouched(hook):
    for name in ("gateway_brave_search___brave_web_search", "fetch_url_content", "WebSearch"):
        event = _event(name, {"query": "aurora forecast"})
        original = event.tool_use

        hook._before_tool_call(event)

        assert event.tool_use is original
        assert event.tool_use["input"] == {"query": "aurora forecast"}


def test_matches_any_target_name():
    assert is_web_search_tool("web-search___WebSearch")
    assert is_web_search_tool("gateway_campus-web___WebSearch")
    assert not is_web_search_tool("web-search___WebSearchExtra")
    assert not is_web_search_tool(None)


def test_never_writes_into_the_model_s_tool_use(hook):
    model_input = {"query": "aurora forecast"}
    event = _event("web-search___WebSearch", model_input)
    original = event.tool_use

    hook._before_tool_call(event)

    assert event.tool_use is not original
    assert original["input"] == {"query": "aurora forecast"}
    assert model_input == {"query": "aurora forecast"}


def test_explicit_null_counts_as_omitted(hook):
    event = _event("web-search___WebSearch", {"query": "q", "maxResults": None})

    hook._before_tool_call(event)

    assert event.tool_use["input"]["maxResults"] == 5


def test_missing_input_gets_the_default(hook):
    event = _event("web-search___WebSearch", None)

    hook._before_tool_call(event)

    assert event.tool_use["input"] == {"maxResults": 5}


def test_non_object_input_is_left_for_the_tool_to_reject(hook):
    event = _event("web-search___WebSearch", "aurora forecast")
    original = event.tool_use

    hook._before_tool_call(event)

    assert event.tool_use is original


def test_a_cancelled_call_is_left_alone(hook):
    event = _event("web-search___WebSearch", {"query": "q"}, cancel="denied")
    original = event.tool_use

    hook._before_tool_call(event)

    assert event.tool_use is original


# ------------------------------------------------- through a real Strands agent


class _SearchOnceModel(Model):
    """Calls the search tool once with `tool_input`, then answers in text."""

    def __init__(self, tool_input: dict) -> None:
        self._tool_input = tool_input

    def update_config(self, **model_config: Any) -> None:  # pragma: no cover
        pass

    def get_config(self) -> Any:  # pragma: no cover
        return {}

    def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, *args: Any, **kwargs: Any) -> AsyncIterable[dict]:
        answered = any("toolResult" in block for block in messages[-1]["content"])
        yield {"messageStart": {"role": "assistant"}}
        if answered:
            yield {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}}
            yield {"contentBlockDelta": {"delta": {"text": "Done."}, "contentBlockIndex": 0}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        yield {
            "contentBlockStart": {
                "start": {"toolUse": {"name": "web-search___WebSearch", "toolUseId": "tu-1"}},
                "contentBlockIndex": 0,
            }
        }
        yield {
            "contentBlockDelta": {
                "delta": {"toolUse": {"input": json.dumps(self._tool_input)}},
                "contentBlockIndex": 0,
            }
        }
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "tool_use"}}


async def _run(tool_input: dict) -> tuple[list[dict], list[dict]]:
    received: list[dict] = []

    @tool(name="web-search___WebSearch")
    def web_search(query: str, maxResults: int | None = None) -> str:  # noqa: N803 - the connector's name
        """Search the web."""
        received.append({"query": query, "maxResults": maxResults})
        return "results"

    agent = Agent(
        model=_SearchOnceModel(tool_input),
        tools=[web_search],
        hooks=[WebSearchDefaultsHook()],
        callback_handler=None,
    )
    async for _ in agent.stream_async("search"):
        pass
    tool_uses = [
        block["toolUse"]
        for message in agent.messages
        for block in message["content"]
        if "toolUse" in block
    ]
    return received, tool_uses


@pytest.mark.asyncio
async def test_agent_sends_five_and_history_keeps_the_model_s_request():
    received, tool_uses = await _run({"query": "aurora forecast"})

    assert received == [{"query": "aurora forecast", "maxResults": 5}]
    assert tool_uses[0]["input"] == {"query": "aurora forecast"}


@pytest.mark.asyncio
async def test_agent_passes_the_model_s_own_count():
    received, tool_uses = await _run({"query": "aurora forecast", "maxResults": 8})

    assert received == [{"query": "aurora forecast", "maxResults": 8}]
    assert tool_uses[0]["input"] == {"query": "aurora forecast", "maxResults": 8}
