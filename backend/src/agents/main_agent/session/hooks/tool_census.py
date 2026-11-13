"""Hook that tallies tool calls per model call, for the content-free cost profile.

The admin cost console can say what a conversation *cost* down to the model
call, but until now nothing recorded what the user was *doing* in it — which
tools ran, how often, how often they failed. That signal lives only inside
message content, which the console must never read. This hook counts it out
of band: tool name → ``{calls, errors, errorKinds, resultChars, inputChars,
durationMs}``, attributed to the model call whose output requested the tools,
and the stream coordinator writes the tally onto that call's ``C#`` cost row
as ``toolCalls`` (the same way ``turnAgentId`` and ``prefixFingerprints`` ride
that row).

Tool names are catalog ids and MCP tool names — structural, not content.

**Weight, never contents** (release-ledger spec D4). Counting calls says which
tools ran; it does not say which tools filled the context. So each call also
adds:

- ``resultChars``: the summed ``len()`` of the result's ``text`` blocks and of
  its ``json`` blocks serialised, plus a fixed :data:`IMAGE_BLOCK_CHARS` per
  ``image`` block so an image is not invisible. Other block kinds (a
  ``document``, a ``video``) count zero; no tool on the platform returns one
  today, and counting their raw bytes as characters would overstate them.
- ``inputChars``: the length of the tool input serialised, i.e. what the model
  wrote to call the tool, which also sits in the history from then on.
- ``durationMs``: Strands' own measured execution time
  (``AfterToolCallEvent.duration``, the figure ``AgentStatusHook`` shows on the
  tool row), summed. Present only when at least one call reported it, and
  ``timedCalls`` says how many did, so a mean is never taken over calls that
  had no timing.

Only lengths are kept: the strings are measured where they already sit in
memory and nothing of them is copied or recorded. Beyond those lengths,
nothing of the result is read except ``status``, the MCP App
awaiting-submission check and the OAuth 401 check below.

**Why a call failed, as a closed vocabulary.** An error count alone cannot say
what to fix: an expired token that the consent hook retries, a tool that timed
out, a tool that raised, and a tool that answered "error" are four different
problems. A failed call also adds one to ``errorKinds[kind]``, where ``kind``
is one of :data:`ERROR_KINDS`:

- ``auth``: the result matches the same OAuth 401 check the consent hook
  retries on (``looks_like_auth_failure``). Usually followed by a successful
  retry, so it is the part of an error rate that is not a tool defect.
- ``timeout``: the call raised a timeout.
- ``exception``: the call raised anything else.
- ``result_error``: the tool returned ``status == "error"`` itself.

The kind is a classification, never the error's text or the exception's
message. ``errorKinds`` is present only on a tool that failed, and its values
sum to ``errors``.

Attribution uses the same cycle counter ``AgentStatusHook`` uses: ``cycle``
increments on every ``BeforeModelCallEvent``, and a tool that runs during
cycle *N* was requested by model call *N* — the coordinator's ``call_index``
``N-1`` (0-based). ``tally_for_call`` does that translation so the caller
does not have to.

Deliberately **non-drained** (unlike ``AgentStatusHook``, whose lists the
coordinator empties mid-turn): the tally is read once at turn end, per call,
and reset at the next turn's ``BeforeInvocationEvent``. Per-turn state only —
never anything a later turn could inherit (CLAUDE.md: never cache session
state on an agent instance).

Costs nothing against the model: it runs on hook events the loop already
crosses and writes to an in-process dict. All the measuring is at
``AfterToolCallEvent``, after the tool has run; the ``Before*`` callbacks only
reset and count, so nothing new sits in front of a first token (CLAUDE.md
TTFT budget). Gated by
``COST_DIAGNOSTICS_ENABLED`` (default on, ``=false`` kill switch); while off
every callback returns immediately and ``tally_for_call`` is always ``None``,
so the row simply has no ``toolCalls`` and the profile reads "not tracked".
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, Optional

from strands.hooks import (
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    HookProvider,
    HookRegistry,
)

from agents.main_agent.session.hooks.agent_status import _duration_ms
from agents.main_agent.session.hooks.mcp_app_awaiting import (
    call_awaits_user_submission,
)
from apis.shared.feature_flags import cost_diagnostics_enabled
from apis.shared.oauth.auth_failure import looks_like_auth_failure

logger = logging.getLogger(__name__)

#: Upper bound on distinct tool names tallied per model call. A single call
#: that requests more than this many *distinct* tools is not a shape the
#: platform produces; the cap only guards the row size.
_MAX_TOOLS_PER_CALL = 64

#: What one ``image`` block in a tool result counts toward ``resultChars``.
#: An image's token cost depends on its pixels, not on any string, so it gets
#: a fixed stand-in (release-ledger spec D4) rather than zero.
IMAGE_BLOCK_CHARS = 1_500


#: Why a tool call failed. Closed: a reader may group by it.
ERROR_KINDS = ("auth", "timeout", "exception", "result_error")


def _serialised_len(value: Any) -> int:
    """Length of ``value`` as JSON, the way it reaches the model; 0 if it
    cannot be serialised (a census never raises on a strange payload)."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - weight is best-effort
        return 0


def _result_chars(event: Any) -> int:
    """Characters a tool result put into the context: text, JSON, and a fixed
    figure per image. Lengths only; nothing is kept."""
    result = getattr(event, "result", None)
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            total += len(text)
        if "json" in block:
            total += _serialised_len(block.get("json"))
        if "image" in block:
            total += IMAGE_BLOCK_CHARS
    return total


def _input_chars(event: Any) -> int:
    tool_use = getattr(event, "tool_use", None)
    if not isinstance(tool_use, dict):
        return 0
    return _serialised_len(tool_use.get("input"))


def _tool_name(event: Any) -> Optional[str]:
    tool_use = getattr(event, "tool_use", None) or {}
    if not isinstance(tool_use, dict):
        return None
    name = tool_use.get("name")
    return str(name) if name else None


def _call_failed(event: Any) -> bool:
    """A raised exception OR a result reporting ``status == "error"`` —
    the same definition ``AgentStatusHook`` uses for ``ok=False``, which
    likewise does not count an MCP App form waiting for the user."""
    if getattr(event, "exception", None) is not None:
        return True
    result = getattr(event, "result", None)
    if not (isinstance(result, dict) and result.get("status") == "error"):
        return False
    return not call_awaits_user_submission(event)


def _error_kind(event: Any) -> str:
    """Which of :data:`ERROR_KINDS` a failed call is. Call only when
    :func:`_call_failed` is true."""
    exception = getattr(event, "exception", None)
    if exception is not None:
        if isinstance(exception, TimeoutError) or "timeout" in type(exception).__name__.lower():
            return "timeout"
        return "exception"
    if looks_like_auth_failure(getattr(event, "result", None)):
        return "auth"
    return "result_error"


class ToolCensusHook(HookProvider):
    """Per-turn, per-model-call tool tally:
    ``{cycle: {tool: {calls, errors, resultChars, inputChars[, errorKinds, durationMs, timedCalls]}}}``."""

    def __init__(self) -> None:
        self._cycle = 0
        self._tally: Dict[int, Dict[str, Dict[str, Any]]] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_turn_start)
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)

    # -- reads (called by the stream coordinator at turn end) ---------------

    def tally_for_call(self, call_index: int) -> Optional[Dict[str, Dict[str, Any]]]:
        """The tools requested by model call ``call_index`` (0-based), or
        ``None`` when that call requested none — or the census is off.

        Returns a copy so the caller can hand it to the persistence layer
        without aliasing per-turn state.
        """
        if not cost_diagnostics_enabled():
            return None
        entry = self._tally.get(call_index + 1)
        return copy.deepcopy(entry) if entry else None

    # -- callbacks ----------------------------------------------------------

    def _on_turn_start(self, event: BeforeInvocationEvent) -> None:
        self._cycle = 0
        self._tally = {}

    def _on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        self._cycle += 1

    def _on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        if not cost_diagnostics_enabled():
            return
        try:
            name = _tool_name(event)
            if not name:
                return
            per_call = self._tally.setdefault(self._cycle, {})
            slot = per_call.get(name)
            if slot is None:
                if len(per_call) >= _MAX_TOOLS_PER_CALL:
                    return
                slot = per_call[name] = {
                    "calls": 0,
                    "errors": 0,
                    "resultChars": 0,
                    "inputChars": 0,
                }
            slot["calls"] += 1
            if _call_failed(event):
                slot["errors"] += 1
                kinds = slot.setdefault("errorKinds", {})
                kind = _error_kind(event)
                kinds[kind] = kinds.get(kind, 0) + 1
            slot["resultChars"] += _result_chars(event)
            slot["inputChars"] += _input_chars(event)
            duration_ms = _duration_ms(getattr(event, "duration", None))
            if duration_ms is not None:
                slot["durationMs"] = slot.get("durationMs", 0) + duration_ms
                slot["timedCalls"] = slot.get("timedCalls", 0) + 1
        except Exception as e:  # noqa: BLE001 - a census must never break a turn
            logger.debug("Tool census skipped a call: %s", e)
