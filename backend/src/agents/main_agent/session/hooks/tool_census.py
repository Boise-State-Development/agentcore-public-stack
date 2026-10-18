"""Hook that tallies tool calls per model call, for the content-free cost profile.

The admin cost console can say what a conversation *cost* down to the model
call, but until now nothing recorded what the user was *doing* in it — which
tools ran, how often, how often they failed. That signal lives only inside
message content, which the console must never read. This hook counts it out
of band: tool name → ``{calls, errors}``, attributed to the model call whose
output requested the tools, and the stream coordinator writes the tally onto
that call's ``C#`` cost row as ``toolCalls`` (the same way ``turnAgentId``
and ``prefixFingerprints`` ride that row).

Tool names are catalog ids and MCP tool names — structural, not content.
Nothing here reads a tool's input, and nothing of its result beyond
``status`` and the MCP App awaiting-submission check; none of it is recorded.

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
crosses and writes to an in-process dict. Gated by
``COST_DIAGNOSTICS_ENABLED`` (default on, ``=false`` kill switch); while off
every callback returns immediately and ``tally_for_call`` is always ``None``,
so the row simply has no ``toolCalls`` and the profile reads "not tracked".
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, Optional

from strands.hooks import (
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    HookProvider,
    HookRegistry,
)

from agents.main_agent.session.hooks.mcp_app_awaiting import (
    call_awaits_user_submission,
)
from apis.shared.feature_flags import cost_diagnostics_enabled

logger = logging.getLogger(__name__)

#: Upper bound on distinct tool names tallied per model call. A single call
#: that requests more than this many *distinct* tools is not a shape the
#: platform produces; the cap only guards the row size.
_MAX_TOOLS_PER_CALL = 64


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


class ToolCensusHook(HookProvider):
    """Per-turn, per-model-call tool tally: ``{cycle: {tool: {calls, errors}}}``."""

    def __init__(self) -> None:
        self._cycle = 0
        self._tally: Dict[int, Dict[str, Dict[str, int]]] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_turn_start)
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)

    # -- reads (called by the stream coordinator at turn end) ---------------

    def tally_for_call(self, call_index: int) -> Optional[Dict[str, Dict[str, int]]]:
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
                slot = per_call[name] = {"calls": 0, "errors": 0}
            slot["calls"] += 1
            if _call_failed(event):
                slot["errors"] += 1
        except Exception as e:  # noqa: BLE001 - a census must never break a turn
            logger.debug("Tool census skipped a call: %s", e)
