"""Record a full watch run's probe as the run makes its calls (quiet-watcher triage spec §3.1).

A quiet watcher's later triage (PR T.2) replays the read-only calls the last full run made and
compares each result's fingerprint with the one this run's model read. This hook takes that
baseline from the run's own results, so there is no window between the run and a separate
probe in which a change could be baked in.

``AfterToolCallEvent``
    For every call except ``report``: the tool use id, the tool's name, whether it only reads
    (``runs_in_parallel``, the rule ``ToolConcurrencyPlugin`` applies, judged against the tool
    object the call ran so MCP annotations are the live ones), the characters of its input as
    canonical JSON, and ``fingerprint_result`` of the result. A retried call is recorded again
    under the same id, so the last attempt wins, as it does in history.

Registered at ``HookOrder.SDK_FIRST``. ``AfterToolCallEvent`` runs each order group from the
lowest, so this sees the tool's own result before any hook rewrites it (the offloader's stub,
the MCP App note), which is what a T.2 replay of the same call will fingerprint.

Once the stream ends the route streams :meth:`sse_frame` as one ``watch_probe`` frame before
``done``. The inputs are not on it: the worker already has them on the run's tool trace.

Turn-path impact: none. Built only on a headless watch run while ``WATCH_TRIAGE_ENABLED`` is on
(``_is_watch_run`` in ``inference_api/chat/routes.py``, the gate ``report`` is built behind), so an
interactive turn never registers it. It hashes and does no I/O, and nothing it produces reaches
the prompt, ``toolConfig`` or history. It never raises: a call it can't record is recorded as not
read-only, which leaves the run with no probe (the fail-closed answer).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from strands.hooks import AfterToolCallEvent, HookOrder

from agents.main_agent.core.tool_concurrency import runs_in_parallel
from apis.shared.scheduled_prompts.watch_probe import (
    REPORT_TOOL_NAME,
    ProbeCall,
    fingerprint_result,
    probe_input_json,
    watch_probe_payload,
)

logger = logging.getLogger(__name__)

WATCH_PROBE_EVENT = "watch_probe"


class WatchProbeRecorder:
    """One headless watch run's calls, fingerprinted. Per run, never on a cached agent."""

    def __init__(self) -> None:
        # By tool use id, in the order first seen; a retry replaces its entry in place.
        self._calls: Dict[str, ProbeCall] = {}

    def register(self, strands_agent: Any) -> None:
        """Add the callback to ``strands_agent`` (the run's own, never a cached one)."""
        strands_agent.add_hook(self.on_after_tool_call, AfterToolCallEvent, order=HookOrder.SDK_FIRST)

    def on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        tool_use = event.tool_use or {}
        name = str(tool_use.get("name") or "")
        if name == REPORT_TOOL_NAME:
            return
        tool_use_id = str(tool_use.get("toolUseId") or "")
        try:
            call = ProbeCall(
                tool_use_id=tool_use_id,
                name=name,
                read_only=runs_in_parallel(event.selected_tool, name),
                fingerprint=fingerprint_result(event.result),
                input_chars=len(probe_input_json(tool_use.get("input"))),
            )
        except Exception:
            logger.warning("watch probe: could not record a call; the run leaves no probe", exc_info=True)
            call = ProbeCall(tool_use_id=tool_use_id, name=name, read_only=False, fingerprint="", input_chars=0)
        self._calls[tool_use_id] = call

    @property
    def calls(self) -> List[ProbeCall]:
        return list(self._calls.values())

    def payload(self) -> Dict[str, Any]:
        return watch_probe_payload(self.calls)

    def sse_frame(self) -> str:
        return f"event: {WATCH_PROBE_EVENT}\ndata: {json.dumps(self.payload())}\n\n"


__all__ = ["WATCH_PROBE_EVENT", "WatchProbeRecorder"]
