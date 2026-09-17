"""Hook that records, per model call, what happened to the context *before* it.

Two content-free facts the cost anatomy could not otherwise answer from stored
data, both found missing during the 2026-09-15 prod cost audit:

- ``windowRemovedMessages`` — the conversation manager's cumulative
  ``removed_message_count`` at the moment of the call. A rise between two
  consecutive rows means the message list was trimmed in between (Strands'
  sliding window sliding, or a compaction slice), which re-writes the cached
  prefix. Distinguishing "pure window slide" sessions from "compaction spiral"
  sessions took an hour of fingerprint reading; with this field it is one
  query.
- ``compactionEvents`` — the compaction decisions the session manager made
  since the previous call (restore-time slice applied, checkpoint advanced,
  and whatever a future scheduling policy records: forced cut, floor
  unreachable). Each carries the summary's token size at that moment, which
  is what proves a summary cap shrank summaries without another scan.
- ``documentReads`` — how many ``document_read`` retrievals the call
  requested and how many pages / bytes they pulled back into context
  (docs/specs/document-context-offload.md §6.1). With the per-row document
  context fields the coordinator adds, this is what says whether a turn
  answered from the full document, from a digest, or from retrieved pages.

Per-turn, per-model-call, held on the agent wrapper exactly like the tool
census: the stream coordinator reads ``ledger_for_call(idx)`` at turn end and
persists it on that call's ``C#`` cost row. Gated by the same
``COST_DIAGNOSTICS_ENABLED`` kill switch; while off the callbacks return
immediately and nothing is written, so a row without the fields reads
"not tracked", never "0".
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

from strands.hooks import (
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    HookProvider,
    HookRegistry,
)

from apis.shared.feature_flags import cost_diagnostics_enabled

logger = logging.getLogger(__name__)

#: A call never carries more events than this; a runaway recorder must not
#: grow a cost row without bound.
_MAX_EVENTS_PER_CALL = 8

#: The document-retrieval tool whose results the ledger tallies
#: (``documentReads``): calls, pages and bytes the model pulled back into
#: context. Read from the tool result's own ``json`` block — numbers only.
DOCUMENT_READ_TOOL_NAME = "document_read"


def _document_read_counts(event: Any) -> Optional[Dict[str, int]]:
    """``{"calls": 1, "pages": n, "bytes": b}`` for a successful
    ``document_read`` result, ``None`` for anything else. Only the result's
    numeric metadata is read; the document block itself is never inspected."""
    tool_use = getattr(event, "tool_use", None) or {}
    if not isinstance(tool_use, dict) or tool_use.get("name") != DOCUMENT_READ_TOOL_NAME:
        return None
    result = getattr(event, "result", None)
    if not isinstance(result, dict) or result.get("status") != "success":
        return None
    pages = 0
    size = 0
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        payload = block.get("json")
        if isinstance(payload, dict):
            try:
                pages += int(payload.get("pages_returned") or 0)
            except (TypeError, ValueError):
                pass
        document = block.get("document")
        if isinstance(document, dict):
            raw = (document.get("source") or {}).get("bytes")
            if isinstance(raw, (bytes, bytearray)):
                size += len(raw)
    return {"calls": 1, "pages": pages, "bytes": size}


def _removed_message_count(agent: Any) -> Optional[int]:
    manager = getattr(agent, "conversation_manager", None)
    value = getattr(manager, "removed_message_count", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _drain_compaction_events(agent: Any) -> List[Dict[str, Any]]:
    """Take the session manager's pending compaction events, if it keeps any.

    Strands stores the manager as ``agent._session_manager``; ours exposes
    ``drain_compaction_events``. Anything else (tests, other managers) yields
    an empty list.
    """
    manager = getattr(agent, "_session_manager", None)
    drain = getattr(manager, "drain_compaction_events", None)
    if not callable(drain):
        return []
    try:
        events = drain()
    except Exception as e:  # noqa: BLE001 - a ledger must never break a turn
        logger.debug("Context ledger could not drain compaction events: %s", e)
        return []
    return list(events or [])[:_MAX_EVENTS_PER_CALL]


class ContextLedgerHook(HookProvider):
    """Per-turn, per-model-call context ledger: ``{cycle: {...}}``."""

    def __init__(self) -> None:
        self._cycle = 0
        self._ledger: Dict[int, Dict[str, Any]] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_turn_start)
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)

    def _on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Tally ``document_read`` retrievals against the model call that
        requested them (same cycle attribution as the tool census: a tool
        running during cycle N was requested by call N). The pages come back
        into context for call N+1; the row of call N says the model asked."""
        if not cost_diagnostics_enabled():
            return
        try:
            counts = _document_read_counts(event)
            if counts is None:
                return
            entry = self._ledger.setdefault(self._cycle, {})
            reads = entry.setdefault("documentReads", {"calls": 0, "pages": 0, "bytes": 0})
            for key, value in counts.items():
                reads[key] = reads.get(key, 0) + value
        except Exception as e:  # noqa: BLE001 - a ledger must never break a turn
            logger.debug("Context ledger skipped a tool result: %s", e)

    def ledger_for_call(self, call_index: int) -> Optional[Dict[str, Any]]:
        """The ledger entry for model call ``call_index`` (0-based), or
        ``None`` when nothing was recorded — or the diagnostics are off.

        Returns a copy so the caller can hand it to the persistence layer
        without aliasing per-turn state.
        """
        if not cost_diagnostics_enabled():
            return None
        entry = self._ledger.get(call_index + 1)
        return copy.deepcopy(entry) if entry else None

    def _on_turn_start(self, event: BeforeInvocationEvent) -> None:
        self._cycle = 0
        self._ledger = {}

    def _on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        self._cycle += 1
        if not cost_diagnostics_enabled():
            return
        try:
            agent = event.agent
            entry: Dict[str, Any] = {}
            removed = _removed_message_count(agent)
            if removed is not None:
                entry["windowRemovedMessages"] = removed
            events = _drain_compaction_events(agent)
            if events:
                entry["compactionEvents"] = events
            if entry:
                self._ledger[self._cycle] = entry
        except Exception as e:  # noqa: BLE001 - a ledger must never break a turn
            logger.debug("Context ledger skipped a call: %s", e)
