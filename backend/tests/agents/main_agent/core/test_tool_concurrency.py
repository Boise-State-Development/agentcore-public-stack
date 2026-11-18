"""Concurrent tool execution: read-only calls overlap, everything else stays serial.

Driven through a real Strands ``Agent`` with ``ConcurrentToolExecutor`` and the
plugin, with a scripted model that asks for one batch of tools and then answers.
What is pinned:

1. Calls the policy marks read-only run at the same time.
2. Every other call runs one at a time, even inside a concurrent batch.
3. Tool results reach history in the order the model asked for them, so the
   persisted message, and therefore the cached prefix, is independent of
   which call finished first. (After an interrupt resume, the results that
   finished before the pause come first; that message is still written once.)
4. harness-sdk#4713: a ``BaseException`` from a tool becomes an error result
   instead of a missing one, while a real cancellation still cancels.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, AsyncIterable

import pytest
from strands import Agent, ToolContext, tool
from strands.models.model import Model
from strands.tools.executors import ConcurrentToolExecutor

from agents.main_agent.core.tool_concurrency import (
    PARALLEL_SAFE_TOOLS,
    ToolConcurrencyPlugin,
    runs_in_parallel,
)

# ------------------------------------------------------------------ the policy


def _mcp_tool(read_only: Any = None, destructive: Any = None) -> SimpleNamespace:
    annotations = SimpleNamespace(readOnlyHint=read_only, destructiveHint=destructive)
    return SimpleNamespace(mcp_tool=SimpleNamespace(annotations=annotations))


def test_known_read_only_builtins_run_in_parallel():
    for name in ("document_read", "verify_sources", "fetch_url_content", "calculator"):
        assert name in PARALLEL_SAFE_TOOLS
        assert runs_in_parallel(object(), name)


def test_gateway_web_search_runs_in_parallel_under_any_target_name():
    assert runs_in_parallel(object(), "web-search___WebSearch")
    assert runs_in_parallel(object(), "my-search-target___WebSearch")


def test_mcp_read_only_hint_runs_in_parallel():
    assert runs_in_parallel(_mcp_tool(read_only=True), "canvas___list_courses")


@pytest.mark.parametrize(
    "tool_obj",
    [
        _mcp_tool(read_only=True, destructive=True),
        _mcp_tool(read_only=False),
        _mcp_tool(read_only=None),
        SimpleNamespace(mcp_tool=SimpleNamespace(annotations=None)),
        object(),
    ],
)
def test_everything_else_is_serialized(tool_obj):
    """Fails closed: an unclassified tool never overlaps."""
    assert not runs_in_parallel(tool_obj, "github___create_issue")


@pytest.mark.parametrize(
    "name", ["browse_web", "create_word_document", "analyze_spreadsheet", "my_skill_create", "local_shell"]
)
def test_shared_resource_builtins_are_serialized(name):
    assert not runs_in_parallel(object(), name)


# ------------------------------------------------- through a real Strands agent


class _BatchModel(Model):
    """Asks for every tool in ``batch`` in one message, then answers in text."""

    def __init__(self, batch: list[str]) -> None:
        self._batch = batch

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
        for index, name in enumerate(self._batch):
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"name": name, "toolUseId": f"tu-{index}"}},
                    "contentBlockIndex": index,
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": json.dumps({})}},
                    "contentBlockIndex": index,
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": index}}
        yield {"messageStop": {"stopReason": "tool_use"}}


class _Tracker:
    """Records how many calls ran at once."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.finished: list[str] = []

    async def run(self, name: str, seconds: float) -> str:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(seconds)
        finally:
            self.active -= 1
        self.finished.append(name)
        return f"{name} ok"


def _make_tools(tracker: _Tracker, durations: dict[str, float]) -> list[Any]:
    built = []
    for name, seconds in durations.items():

        def make(tool_name: str, wait: float):
            @tool(name=tool_name)
            async def _t() -> str:
                """A tool that waits."""
                return await tracker.run(tool_name, wait)

            return _t

        built.append(make(name, seconds))
    return built


def _agent(batch: list[str], tools: list[Any]) -> Agent:
    return Agent(
        model=_BatchModel(batch),
        tools=tools,
        tool_executor=ConcurrentToolExecutor(),
        plugins=[ToolConcurrencyPlugin()],
        callback_handler=None,
    )


async def _drain(stream) -> Any:
    result = None
    async for event in stream:
        if "result" in event:
            result = event["result"]
    return result


def _tool_result_ids(agent: Agent) -> list[str]:
    for message in agent.messages:
        ids = [block["toolResult"]["toolUseId"] for block in message["content"] if "toolResult" in block]
        if ids:
            return ids
    return []


def test_read_only_calls_overlap():
    tracker = _Tracker()
    batch = ["document_read", "verify_sources", "calculator"]
    agent = _agent(batch, _make_tools(tracker, {name: 0.2 for name in batch}))

    asyncio.run(_drain(agent.stream_async("go")))

    assert tracker.peak == 3


def test_other_calls_stay_serial_inside_a_concurrent_batch():
    tracker = _Tracker()
    batch = ["create_word_document", "browse_web", "analyze_spreadsheet"]
    agent = _agent(batch, _make_tools(tracker, {name: 0.05 for name in batch}))

    asyncio.run(_drain(agent.stream_async("go")))

    assert tracker.peak == 1
    assert sorted(tracker.finished) == sorted(batch)


def test_a_read_overlaps_a_serialized_write():
    tracker = _Tracker()
    batch = ["create_word_document", "document_read"]
    agent = _agent(batch, _make_tools(tracker, {name: 0.2 for name in batch}))

    asyncio.run(_drain(agent.stream_async("go")))

    assert tracker.peak == 2


def test_results_land_in_the_order_the_model_asked_not_the_order_they_finished():
    tracker = _Tracker()
    batch = ["document_read", "verify_sources", "calculator"]
    # The first call finishes last.
    durations = {"document_read": 0.3, "verify_sources": 0.05, "calculator": 0.1}
    agent = _agent(batch, _make_tools(tracker, durations))

    asyncio.run(_drain(agent.stream_async("go")))

    assert tracker.finished == ["verify_sources", "calculator", "document_read"]
    assert _tool_result_ids(agent) == ["tu-0", "tu-1", "tu-2"]


def test_a_stray_base_exception_becomes_an_error_result_not_a_missing_one():
    """harness-sdk#4713: without the guard the toolResult for tu-0 is dropped."""

    @tool(name="document_read")
    async def broken() -> str:
        """Raises the way an anyio cancel scope can leak out of an MCP client."""
        raise asyncio.CancelledError()

    tracker = _Tracker()
    agent = _agent(["document_read", "calculator"], [broken, *_make_tools(tracker, {"calculator": 0.01})])

    result = asyncio.run(_drain(agent.stream_async("go")))

    assert result.stop_reason == "end_turn"
    assert _tool_result_ids(agent) == ["tu-0", "tu-1"]
    tool_results = [
        block["toolResult"]
        for message in agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    assert tool_results[0]["status"] == "error"
    assert "CancelledError" in tool_results[0]["content"][0]["text"]


@pytest.mark.parametrize("concurrent", [False, True])
def test_abandoning_the_stream_behaves_as_it_does_today(concurrent):
    """Parity with the sequential executor: an abandoned stream lets the batch finish.

    Strands does not cancel running tools when the consumer closes the stream;
    it runs the batch out (the sequential executor took the sum, the concurrent
    one about the longest). Interrupting an in-flight call is the per-turn
    ``cancel_signal``'s job, not this plugin's.
    """
    from strands.tools.executors import SequentialToolExecutor

    tracker = _Tracker()
    batch = ["document_read", "create_word_document"]
    tools = _make_tools(tracker, {name: 0.1 for name in batch})
    agent = (
        _agent(batch, tools)
        if concurrent
        else Agent(
            model=_BatchModel(batch),
            tools=tools,
            tool_executor=SequentialToolExecutor(),
            callback_handler=None,
        )
    )

    async def scenario() -> None:
        stream = agent.stream_async("go")
        async for _event in stream:
            if tracker.active:
                break
        await stream.aclose()

    asyncio.run(scenario())

    assert sorted(tracker.finished) == sorted(batch)


def test_a_real_task_cancellation_is_reraised_not_reported():
    """The guard must never turn the executor's own cancel into an error result."""
    from agents.main_agent.core.tool_concurrency import _guarded

    started = asyncio.Event()

    async def slow_chain(_context):
        started.set()
        await asyncio.sleep(5)
        yield "never"

    context = SimpleNamespace(tool_use={"name": "document_read", "toolUseId": "tu-0"})
    seen: list[Any] = []

    async def consume() -> None:
        async for event in _guarded(context, slow_chain):
            seen.append(event)

    async def scenario() -> None:
        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert seen == []


def test_an_interrupt_beside_a_read_pauses_and_resumes_without_rerunning_the_read():
    """The pause/resume contract (approvals, ask_user_question) holds in a concurrent batch.

    Sequential execution stopped the batch at the first interrupt; concurrent
    execution runs the read beside it. Strands records the read's result as
    pending, so the resume answers the interrupt and does not run the read again.
    """
    tracker = _Tracker()
    asked: list[Any] = []

    @tool(name="ask_user_question", context=True)
    def ask(tool_context: ToolContext) -> str:
        """Pauses for the user."""
        answer = tool_context.interrupt("pick", reason={"q": "which?"})
        asked.append(answer)
        return f"user said {answer}"

    agent = _agent(["ask_user_question", "document_read"], [ask, *_make_tools(tracker, {"document_read": 0.05})])

    first = asyncio.run(_drain(agent.stream_async("go")))

    assert first.stop_reason == "interrupt"
    assert tracker.finished == ["document_read"]
    interrupt_id = first.interrupts[0].id

    second = asyncio.run(
        _drain(agent.stream_async([{"interruptResponse": {"interruptId": interrupt_id, "response": "blue"}}]))
    )

    assert second.stop_reason == "end_turn"
    assert asked == ["blue"]
    assert tracker.finished == ["document_read"]
    # Both results are in one message. After a resume the one that finished
    # before the pause comes first; sequential execution would have run the
    # read only after the answer. Written once either way, so the persisted
    # history is stable from then on, and Bedrock accepts either order.
    assert _tool_result_ids(agent) == ["tu-1", "tu-0"]
