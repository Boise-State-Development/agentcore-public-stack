"""Narrate what the agent is doing while a turn is still streaming.

A multi-tool turn spends most of its wall-clock time in places the content
stream says nothing about: the model deciding, a Lambda-backed MCP tool doing
a round trip, a batch of parallel calls finishing at different times. The SPA
covered that silence with cycling phrases ("Pondering...", "Cross-referencing")
which are honest about *nothing* — they look identical whether the agent is
thinking, waiting on Canvas, or hung.

This hook watches the boundaries the event loop already crosses and records a
small status transition at each one. The stream coordinator drains them into
``agent_status`` SSE events (see ``_drain_agent_status_events``), and the SPA
turns them into a live line — "Using list_assignments..." — plus a real
duration on every finished tool row.

WHAT IT OBSERVES
----------------
``BeforeInvocationEvent``
    Turn boundary. Resets per-turn state so a queue left behind by an aborted
    or interrupted turn can never leak into the next one.

``BeforeModelCallEvent``
    The model is generating. Emitted once per event-loop cycle, so a turn that
    calls three tools in series reports "thinking" four times — that is the
    real shape of the turn and the SPA renders each as a fresh cycle.

``BeforeToolCallEvent`` / ``AfterToolCallEvent``
    One tool starting and finishing. ``AfterToolCallEvent`` carries Strands'
    own ``duration``, so the per-tool timing the rail shows is measured by the
    event loop rather than guessed from stream arrival times on the client.

``AfterToolsEvent``
    The batch is done. Closes the batch and parks a **bounded** record of it
    (tool names, truncated inputs, truncated results) for the tool-summary
    side-channel to consume. Fires from a ``finally``, so it also fires on the
    cancel / error / interrupt paths — a batch closed there is still a true
    record of tools that ran, which is exactly what a summary should describe.

WHAT IT DELIBERATELY DOES NOT OBSERVE
-------------------------------------
"The agent is writing its answer." The SPA already knows that: text deltas are
arriving. Deriving it here would mean a second source of truth for a fact the
client holds first-hand, and the two would disagree at the edges.

COST
----
Nothing reaches the model. The hook writes to two in-process lists and the
coordinator drains them between stream events; nothing it produces is appended
to the conversation, so the cacheable prefix is untouched (CLAUDE.md
prompt-cache contract). Both lists are capped — a pathological 500-tool turn
costs a bounded amount of memory and drops the overflow rather than growing
without limit.

Best-effort in every direction: every callback is wrapped, and any failure is
swallowed. A status line is a nicety; it must never be able to break a turn.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from strands.hooks import (
    AfterToolCallEvent,
    AfterToolsEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

logger = logging.getLogger(__name__)

# Queue caps. A normal turn produces a handful of transitions; these exist so a
# runaway agent loop degrades to "stops narrating" instead of "grows until the
# container is killed".
_MAX_QUEUED_STATUSES = 400
_MAX_QUEUED_BATCHES = 40
# Per-tool payload caps for the summarizer record. The summarizer truncates
# again before it builds its prompt; this is the first bound, applied at
# capture time so an 8MB tool result never sits in memory for the turn.
_MAX_INPUT_CHARS = 600
_MAX_RESULT_CHARS = 1200
# A batch wider than this is summarized from its first N calls. Beyond that the
# marginal call adds prompt cost without changing the one-line summary.
_MAX_CALLS_PER_BATCH = 12


def _truncate(text: str, limit: int) -> str:
    """Clip ``text`` to ``limit`` characters with a visible ellipsis."""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _stringify(value: Any, limit: int) -> str:
    """Render a tool input/result as bounded text for the summarizer.

    JSON where possible (the summarizer reads structure better than a repr),
    ``str()`` otherwise. Never raises: a value that will not serialize is worth
    less than the turn it would break.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return _truncate(value, limit)
    try:
        return _truncate(json.dumps(value, default=str), limit)
    except Exception:  # noqa: BLE001 - defensive, see docstring
        return _truncate(str(value), limit)


def _duration_ms(raw: Any) -> Optional[int]:
    """Normalize Strands' ``AfterToolCallEvent.duration`` to whole ms.

    The SDK has expressed this as both a ``float`` of seconds and a
    ``timedelta`` across versions, and the attribute is absent on older ones.
    All three degrade to ``None`` rather than to a wrong number, because a
    wrong duration on a tool row is worse than no duration.
    """
    if raw is None:
        return None
    total_seconds = getattr(raw, "total_seconds", None)
    if callable(total_seconds):
        try:
            return max(0, int(total_seconds() * 1000))
        except Exception:  # noqa: BLE001
            return None
    if isinstance(raw, (int, float)):
        return max(0, int(float(raw) * 1000))
    return None


def _tool_use_fields(event: Any) -> tuple[Optional[str], Optional[str], Any]:
    """Pull ``(tool_use_id, tool_name, input)`` off a tool hook event."""
    tool_use = getattr(event, "tool_use", None) or {}
    if not isinstance(tool_use, dict):
        return None, None, None
    tool_use_id = tool_use.get("toolUseId") or tool_use.get("tool_use_id")
    name = tool_use.get("name")
    return (
        str(tool_use_id) if tool_use_id else None,
        str(name) if name else None,
        tool_use.get("input"),
    )


class AgentStatusHook(HookProvider):
    """Record model-call and tool-call transitions for the live status line.

    Holds only per-turn state, reset at ``BeforeInvocationEvent`` and drained
    within the same turn. Per the CLAUDE.md rule that per-session state is
    never cached on an agent instance: there is no session state here to go
    stale — an ``@``-mention turn builds a second ``Agent`` with its own hook,
    and each narrates its own turn.
    """

    def __init__(self) -> None:
        # Status transitions awaiting a drain by the stream coordinator.
        self._statuses: List[Dict[str, Any]] = []
        # Closed tool batches awaiting a summarizer task.
        self._batches: List[Dict[str, Any]] = []
        # Calls completed in the batch currently in flight.
        self._open_batch: List[Dict[str, Any]] = []
        # Event-loop cycle counter, 1-based. Lets the SPA tell "thinking again
        # after a tool" apart from a stutter in the same cycle.
        self._cycle = 0
        # Monotonic batch counter, used to build a stable batch id.
        self._batch_seq = 0

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_turn_start)
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)
        registry.add_callback(AfterToolsEvent, self._on_after_tools)

    # -- drains (called by the stream coordinator) -------------------------

    def drain_statuses(self) -> List[Dict[str, Any]]:
        """Take the status transitions recorded since the last drain."""
        statuses, self._statuses = self._statuses, []
        return statuses

    def drain_batches(self) -> List[Dict[str, Any]]:
        """Take the tool batches closed since the last drain."""
        batches, self._batches = self._batches, []
        return batches

    # -- callbacks ---------------------------------------------------------

    def _on_turn_start(self, event: BeforeInvocationEvent) -> None:
        """Drop anything a previous turn left behind.

        The interrupt path (OAuth consent, tool approval) unwinds the event
        loop without draining, so without this reset the next turn's first
        drain would replay stale transitions and the SPA would narrate tools
        that are not running.
        """
        self._statuses = []
        self._batches = []
        self._open_batch = []
        self._cycle = 0
        self._batch_seq = 0

    def _on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        if not self._enabled():
            return
        try:
            self._cycle += 1
            self._push({"phase": "thinking", "cycle": self._cycle})
        except Exception:  # noqa: BLE001 - narration must never break a turn
            logger.debug("Agent status (before model call) skipped", exc_info=True)

    def _on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if not self._enabled():
            return
        try:
            tool_use_id, name, _ = _tool_use_fields(event)
            if not name:
                return
            self._push(
                {
                    "phase": "tool_start",
                    "cycle": self._cycle,
                    "toolName": name,
                    "toolUseId": tool_use_id,
                }
            )
        except Exception:  # noqa: BLE001
            logger.debug("Agent status (before tool call) skipped", exc_info=True)

    def _on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Record the finished call: a status transition AND a batch entry.

        The two are gated separately on purpose. The status transition is the
        live line and rides ``AGENT_STATUS_ENABLED``; the batch entry is what
        the tool-summary side-channel consumes and rides
        ``TOOL_SUMMARIES_ENABLED`` (checked downstream, at the summarizer). A
        deployment that wants summaries without a live status line — or the
        reverse — gets exactly that, instead of one flag silently disabling
        the other feature.
        """
        try:
            tool_use_id, name, tool_input = _tool_use_fields(event)
            if not name:
                return
            duration_ms = _duration_ms(getattr(event, "duration", None))
            ok = getattr(event, "exception", None) is None
            result = getattr(event, "result", None)
            if ok and isinstance(result, dict):
                # A tool can fail *inside* a successful invocation; Strands
                # reports that as a result with status "error", not as a raised
                # exception. The rail's red dot depends on catching both.
                ok = result.get("status") != "error"

            if self._enabled():
                self._push(
                    {
                        "phase": "tool_end",
                        "cycle": self._cycle,
                        "toolName": name,
                        "toolUseId": tool_use_id,
                        "durationMs": duration_ms,
                        "ok": ok,
                    }
                )

            if len(self._open_batch) < _MAX_CALLS_PER_BATCH:
                self._open_batch.append(
                    {
                        "toolUseId": tool_use_id,
                        "toolName": name,
                        "input": _stringify(tool_input, _MAX_INPUT_CHARS),
                        "result": _stringify(result, _MAX_RESULT_CHARS),
                        "ok": ok,
                        "durationMs": duration_ms,
                    }
                )
        except Exception:  # noqa: BLE001
            logger.debug("Agent status (after tool call) skipped", exc_info=True)

    def _on_after_tools(self, event: AfterToolsEvent) -> None:
        """Close the in-flight batch and park it for the summarizer.

        Runs even with the status flag off, because the tool-summary
        side-channel is gated separately — a deployment can want summaries
        without the live status line, and vice versa.
        """
        try:
            calls, self._open_batch = self._open_batch, []
            if not calls:
                return
            self._batch_seq += 1
            batch_id = calls[0].get("toolUseId") or f"batch-{self._batch_seq}"
            if len(self._batches) < _MAX_QUEUED_BATCHES:
                self._batches.append(
                    {
                        "batchId": str(batch_id),
                        "cycle": self._cycle,
                        "toolUseIds": [
                            c["toolUseId"] for c in calls if c.get("toolUseId")
                        ],
                        "calls": calls,
                    }
                )
        except Exception:  # noqa: BLE001
            logger.debug("Agent status (after tools) skipped", exc_info=True)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _enabled() -> bool:
        from apis.shared.feature_flags import agent_status_enabled

        return agent_status_enabled()

    def _push(self, status: Dict[str, Any]) -> None:
        """Queue a transition, dropping it if the turn has run away."""
        if len(self._statuses) >= _MAX_QUEUED_STATUSES:
            return
        self._statuses.append(status)
