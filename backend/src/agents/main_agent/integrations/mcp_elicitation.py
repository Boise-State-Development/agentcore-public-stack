"""MCP form elicitation: pause the turn on a server's form, replay the call with
the user's answer (``docs/specs/mcp-elicitation.md`` §2).

Three pieces, each small:

* ``elicitation_callback`` is the one function handed to every opted-in
  ``MCPClient`` as its ``elicitation_callback``. It runs on the client's
  background loop, inside the tool call's own task on a 2026-07-28 connection
  (Strands' ``_compat._drive_input_required``). It answers from the user's
  stored response when there is one, and otherwise records the form and raises
  ``ElicitationPending``, which ends the call without sending anything more to
  the server.
* ``elicitation_middleware`` wraps ``ExecuteToolStage`` for MCP tools from an
  opted-in server. It opens a ``CallElicitationSlot`` for the call in a
  contextvar, which ``asyncio.run_coroutine_threadsafe`` copies onto the
  background loop, runs the call, and, when the slot recorded a form, raises a
  Strands interrupt through ``ExecuteToolContext.interrupt``. On resume Strands
  runs the call again; the callback finds the answer under the same interrupt
  name and returns it, and the server completes the call.
* ``McpElicitationPlugin`` registers the middleware on an agent.

**Why a replay is safe here.** SEP-2322 is stateless: the server's first round
returns before it acts, and everything it needs comes back on the retry. A
replay starts a fresh round, so a server-side expiry on ``request_state``
(GitHub's is 10 minutes) never runs out while the user thinks. Legacy
``elicitation/create`` is never advertised (``_UIExtensionClientSession``), and
a legacy request that arrives anyway finds no slot and is refused exactly as the
SDK's default would refuse it.

**Session state.** Nothing lives on the agent or on the shared client. The slot
exists for one call; the answers are read from the agent's own
``_interrupt_state``, which Strands restores per resume from the session store.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Optional, Set

from mcp.types import INVALID_REQUEST, ElicitResult, ErrorData
from strands.interrupt import InterruptException
from strands.middleware import ExecuteToolContext, ExecuteToolStage
from strands.plugins import Plugin
from strands.tools.mcp import MCPAgentTool
from strands.types.events import ToolResultEvent

from apis.shared.mcp_elicitation.forms import (
    ElicitationForm,
    InvalidElicitationAnswer,
    UnsupportedElicitation,
    build_reason,
    interrupt_name,
    normalize_form,
    unsupported_form_text,
    validate_answer,
)

logger = logging.getLogger(__name__)

#: What a request we cannot attribute to a call is answered with. The same
#: error the SDK's own default callback returns, so a server that was never
#: told we support elicitation sees exactly what it saw before.
_NOT_SUPPORTED = ErrorData(code=INVALID_REQUEST, message="Elicitation not supported")


class ElicitationPending(Exception):
    """Raised by the callback to end a call whose form the user hasn't answered.

    It escapes ``call_tool`` before the retry round is sent, so the server
    receives nothing more for this call. Strands turns it into an error result,
    which the middleware discards in favour of the interrupt.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"waiting for the user to answer MCP elicitation {name}")
        self.name = name


@dataclass
class _PendingForm:
    name: str
    form: ElicitationForm


@dataclass
class CallElicitationSlot:
    """Per-call state shared between the middleware and the callback.

    ``lookup`` returns the user's stored response for an interrupt name, or
    None. Round tracking: the callback sees each input request of a round once;
    a key seen again means the server moved to its next round. A server that
    uses fresh keys per round stays on round 0, which is still unambiguous
    because the key is part of the interrupt name.
    """

    lookup: Callable[[str], Any]
    round_index: int = 0
    keys_in_round: Set[str] = field(default_factory=set)
    pending: Optional[_PendingForm] = None
    unsupported: bool = False

    def next_name(self, request_key: str, requested_schema: Any) -> str:
        if request_key in self.keys_in_round:
            self.round_index += 1
            self.keys_in_round = set()
        self.keys_in_round.add(request_key)
        return interrupt_name(self.round_index, request_key, requested_schema)


_SLOT: contextvars.ContextVar[Optional[CallElicitationSlot]] = contextvars.ContextVar(
    "mcp_elicitation_slot", default=None
)


async def elicitation_callback(context: Any, params: Any) -> Any:
    """The ``elicitation_callback`` for an opted-in MCP client.

    Returns an ``ElicitResult`` built from the user's stored answer, a
    ``cancel`` when the form cannot be shown or the stored answer does not fit
    it, or the SDK's own "not supported" error when no call is waiting on it.

    Raises:
        ElicitationPending: The user has not answered this form yet.
    """
    slot = _SLOT.get()
    if slot is None:
        return _NOT_SUPPORTED
    try:
        dumped: Dict[str, Any] = params.model_dump(by_alias=True, mode="json", exclude_none=True)
    except Exception:  # noqa: BLE001 - an unreadable request is refused, never raised
        return _NOT_SUPPORTED
    if dumped.get("mode", "form") != "form":
        # URL mode is never advertised; refuse it if a server sends it anyway.
        return _NOT_SUPPORTED

    requested_schema = dumped.get("requestedSchema")
    try:
        form = normalize_form(dumped.get("message"), requested_schema)
    except UnsupportedElicitation as exc:
        logger.info("MCP elicitation form not shown: %s", exc)
        slot.unsupported = True
        return ElicitResult(action="cancel")

    name = slot.next_name(str(getattr(context, "request_id", "")), requested_schema)
    response = slot.lookup(name)
    if response is None:
        slot.pending = _PendingForm(name=name, form=form)
        raise ElicitationPending(name)

    try:
        answer = validate_answer(form, response)
    except InvalidElicitationAnswer as exc:
        # The SPA validates before it posts, so this is a broken or hostile
        # client. Never forward an answer that does not fit the form.
        logger.warning("MCP elicitation answer refused (%s): %s", name, exc)
        return ElicitResult(action="cancel")
    return ElicitResult(action=answer.action, content=answer.content)


def handles(tool: Any) -> bool:
    """Whether a tool comes from a client that advertises form elicitation."""
    return isinstance(tool, MCPAgentTool) and bool(
        getattr(tool.mcp_client, "elicitation_enabled", False)
    )


def _stored_response(context: ExecuteToolContext, name: str) -> Any:
    """The user's response to ``name`` on this tool call, or None.

    ``ExecuteToolContext.interrupt`` only reads interrupt state (the executor
    registers interrupts, never the middleware), so calling it to probe for a
    stored answer has no side effect; it raises when there is none.
    """
    try:
        return context.interrupt(name).response
    except InterruptException:
        return None


def _with_note(event: ToolResultEvent, note: str) -> ToolResultEvent:
    result = dict(event.tool_result)
    result["content"] = [*(result.get("content") or []), {"text": note}]
    return ToolResultEvent(result, exception=event.exception)


async def elicitation_middleware(
    context: ExecuteToolContext,
    next_fn: Callable[[ExecuteToolContext], AsyncIterator[Any]],
) -> AsyncIterator[Any]:
    """``ExecuteToolStage`` Wrap middleware that pauses on a server's form."""
    tool = context.tool
    if not handles(tool):
        async for event in next_fn(context):
            yield event
        return

    slot = CallElicitationSlot(lookup=lambda name: _stored_response(context, name))
    result_event: Optional[ToolResultEvent] = None
    token = _SLOT.set(slot)
    try:
        async for event in next_fn(context):
            if isinstance(event, ToolResultEvent):
                result_event = event
                continue
            yield event
    finally:
        try:
            _SLOT.reset(token)
        except ValueError:
            # Closed from another context; the slot dies with the call anyway.
            pass

    server_name = getattr(tool.mcp_client, "context_label", None)
    if slot.pending is not None:
        reason = build_reason(
            slot.pending.form,
            tool_name=str(context.tool_use.get("name", "")),
            server_name=server_name,
        )
        reason["toolUseId"] = str(context.tool_use.get("toolUseId", ""))
        context.interrupt(slot.pending.name, reason=reason)
        # interrupt() returns only when an answer exists, and the callback
        # found none a moment ago. Fall through rather than hang the turn.
        logger.warning("MCP elicitation %s was answered mid-call", slot.pending.name)

    if result_event is None:
        return
    if slot.unsupported:
        result_event = _with_note(result_event, unsupported_form_text(server_name))
    yield result_event


class McpElicitationPlugin(Plugin):
    """Registers ``elicitation_middleware`` on an agent."""

    name = "mcp-elicitation"

    def init_agent(self, agent: Any) -> None:
        agent.add_middleware(ExecuteToolStage, elicitation_middleware)


class _DropPendingTraceback(logging.Filter):
    """Keep Strands from logging a traceback for every paused call.

    ``MCPClient.call_tool_async`` logs any exception from the call with
    ``logger.exception``. A pause is not a failure, so its record is dropped;
    every other record passes untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        stack = [record.exc_info[1]] if record.exc_info else []
        seen: Set[int] = set()
        while stack:
            exc = stack.pop()
            if exc is None or id(exc) in seen:
                continue
            if isinstance(exc, ElicitationPending):
                return False
            seen.add(id(exc))
            stack.extend((exc.__cause__, exc.__context__))
            stack.extend(getattr(exc, "exceptions", ()) or ())  # ExceptionGroup
        return True


_FILTER = _DropPendingTraceback()


def install_log_filter() -> None:
    """Idempotently attach the filter to Strands' MCP client logger."""
    strands_logger = logging.getLogger("strands.tools.mcp.mcp_client")
    if _FILTER not in strands_logger.filters:
        strands_logger.addFilter(_FILTER)
