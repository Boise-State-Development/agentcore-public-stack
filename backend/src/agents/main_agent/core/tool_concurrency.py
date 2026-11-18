"""Run a model's tool batch in parallel where that is safe.

Strands runs every tool in a batch one after another under
``SequentialToolExecutor``. That executor was chosen for NovaAct, whose
Playwright driver failed when two browser operations started at once. NovaAct is
gone (the browser is raw CDP, ``builtin_tools/browser/cdp_client.py``), but the
executor stayed, so a batch of N independent calls still takes the sum of their
durations instead of roughly the longest one.

While ``CONCURRENT_TOOL_EXECUTION_ENABLED`` is on, the agent uses
``ConcurrentToolExecutor`` and this module's ``ExecuteToolStage`` middleware
decides, per call, whether it may overlap with others:

* **Parallel:** a call to a tool that only reads (:data:`PARALLEL_SAFE_TOOLS`,
  a Gateway ``<target>___WebSearch``, or an MCP tool whose server declares
  ``readOnlyHint`` and not ``destructiveHint``).
* **Serialized:** everything else, behind one lock per agent, so a call that
  writes a shared resource (the browser, a Code Interpreter session, an Office
  document, an artifact, a skill) never overlaps another such call. Those calls
  keep running one at a time, as they did before.

The policy fails closed: a tool nobody has classified is serialized. Widening it
is a one-line change to :data:`PARALLEL_SAFE_TOOLS`, and each addition should be
a tool that touches nothing another call in the same batch could also touch.

What this does **not** change: the model's request, ``toolConfig`` and the
system prompt. Strands appends a concurrent batch's results in the order the
model asked for them, not the order they finished, so the persisted message is
byte-identical to a sequential run's and the cacheable prefix never moves. The
one difference is a batch that pauses on an interrupt: sequential execution
stops at the interrupt, while here the batch's other calls run beside it, and on
resume the results that finished before the pause come first. That message is
still written once, so history is stable from then on. Nothing here runs before
the first token.

It also guards harness-sdk#4713. ``ConcurrentToolExecutor`` catches only
``Exception`` in its per-tool task, so a ``BaseException`` raised by a tool, such
as an anyio ``CancelledError`` escaping an MCP client, is swallowed by
``asyncio.gather(..., return_exceptions=True)``. The call then has no tool result,
and the next model call fails on a ``toolUse`` with no matching ``toolResult``.
The middleware turns such an error into an ordinary error result instead. It
re-raises a real cancellation of the task (``Task.cancelling()``), which is how
the executor stops its tasks when the stream is abandoned on a Stop.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import AsyncIterator, Callable
from typing import Any, Optional

from strands.middleware import ExecuteToolContext, ExecuteToolStage
from strands.plugins import Plugin
from strands.types.events import ToolResultEvent

logger = logging.getLogger(__name__)

#: Built-in tools that only read, by the name the model calls. A tool belongs
#: here only if two calls to it, or one call beside any other tool, can never
#: touch the same mutable resource.
PARALLEL_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "calculator",
        "document_read",
        "fetch_url_content",
        "get_my_quota",
        "get_my_settings",
        "knowledge_files",
        "list_spreadsheets",
        "project_files",
        "verify_sources",
        "whoami",
    }
)

#: Gateway tools are named ``<target>___<tool>``; Web Search is read-only
#: whatever the admin named the target.
_WEB_SEARCH_SUFFIX = "___WebSearch"


def _mcp_annotations(tool: Any) -> Any:
    """The MCP ``ToolAnnotations`` behind a Strands ``MCPAgentTool``, if any."""
    mcp_tool = getattr(tool, "mcp_tool", None)
    return getattr(mcp_tool, "annotations", None) if mcp_tool is not None else None


def runs_in_parallel(tool: Any, tool_name: str) -> bool:
    """Whether a call to ``tool`` may overlap with the rest of its batch.

    Fails closed: anything not known to be read-only is serialized.
    """
    if tool_name in PARALLEL_SAFE_TOOLS:
        return True
    if tool_name.endswith(_WEB_SEARCH_SUFFIX):
        return True
    annotations = _mcp_annotations(tool)
    if annotations is None:
        return False
    read_only = getattr(annotations, "readOnlyHint", None) is True
    destructive = getattr(annotations, "destructiveHint", None) is True
    return read_only and not destructive


def _is_task_cancellation() -> bool:
    """True when the running task itself has been asked to cancel.

    Distinguishes the executor cancelling its tasks (a Stop, an abandoned
    stream) from a ``CancelledError`` that escaped a tool's own internals.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _error_result(context: ExecuteToolContext, error: BaseException) -> ToolResultEvent:
    tool_use_id = str(context.tool_use.get("toolUseId", ""))
    name = type(error).__name__
    result = {
        "toolUseId": tool_use_id,
        "status": "error",
        "content": [{"text": f"Tool failed: {name}"}],
    }
    return ToolResultEvent(result, exception=Exception(f"{name}: {error}"))


class ToolConcurrencyPlugin(Plugin):
    """Registers :meth:`middleware` on an agent, with that agent's own lock."""

    name = "tool-concurrency"

    def __init__(self) -> None:
        super().__init__()
        # One lock per agent. An agent serves one turn at a time (the session
        # lease), so the lock never spans two conversations. Keyed by the
        # running loop too: an asyncio.Lock binds to the first loop that waits
        # on it, and the agent is built before any loop runs its turn.
        self._locks: "weakref.WeakKeyDictionary[Any, tuple[Any, asyncio.Lock]]" = weakref.WeakKeyDictionary()
        self._fallback: Optional[tuple[Any, asyncio.Lock]] = None

    def init_agent(self, agent: Any) -> None:
        agent.add_middleware(ExecuteToolStage, self.middleware)

    def _lock_for(self, agent: Any) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        try:
            entry = self._locks.get(agent)
            if entry is None or entry[0] is not loop:
                entry = (loop, asyncio.Lock())
                self._locks[agent] = entry
        except TypeError:  # an agent that cannot be weakly referenced: fail closed
            if self._fallback is None or self._fallback[0] is not loop:
                self._fallback = (loop, asyncio.Lock())
            return self._fallback[1]
        return entry[1]

    async def middleware(
        self,
        context: ExecuteToolContext,
        next_fn: Callable[[ExecuteToolContext], AsyncIterator[Any]],
    ) -> AsyncIterator[Any]:
        """Serialize calls that are not parallel-safe, and guard #4713."""
        tool_name = str(context.tool_use.get("name", ""))
        if runs_in_parallel(context.tool, tool_name):
            async for event in _guarded(context, next_fn):
                yield event
            return

        async with self._lock_for(context.agent):
            async for event in _guarded(context, next_fn):
                yield event


async def _guarded(
    context: ExecuteToolContext,
    next_fn: Callable[[ExecuteToolContext], AsyncIterator[Any]],
) -> AsyncIterator[Any]:
    """Pass the chain through, turning a stray ``BaseException`` into an error result."""
    produced_result = False
    try:
        async for event in next_fn(context):
            if isinstance(event, ToolResultEvent):
                produced_result = True
            yield event
    except (GeneratorExit, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        # Strands' own handling (error results, interrupts) owns these.
        raise
    except asyncio.CancelledError as error:
        if _is_task_cancellation() or produced_result:
            raise
        logger.warning(
            "Tool %s raised CancelledError without being cancelled; reporting an error result",
            context.tool_use.get("name"),
        )
        yield _error_result(context, error)
    except BaseException as error:  # noqa: BLE001 - the #4713 guard is exactly this
        if produced_result:
            raise
        logger.warning(
            "Tool %s raised %s; reporting an error result",
            context.tool_use.get("name"),
            type(error).__name__,
        )
        yield _error_result(context, error)
