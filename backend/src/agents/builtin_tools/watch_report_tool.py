"""``report`` — how an unattended watcher run says what it found (quiet-watcher spec §3.3).

A schedule in ``watch`` mode runs a headless turn with ``run_mode: "watch"``. The model checks
from the cursors in the ``<watch_state>`` block the worker put in the user message, then calls
``report`` once at the end. The scheduled-runs worker reads the call's input off the SSE stream
(the harness keeps every ``tool_use`` input), dedupes the items against the keys it has already
notified, and decides: a quiet run leaves nothing behind, a notifying run keeps its conversation
and writes one inbox row per new item.

Design notes
------------
* **Does nothing.** The body returns ``Recorded.`` and performs no I/O: the worker owns every
  write (state, inbox, unread dot), after the run, so a run that crashes writes nothing and the
  Runtime needs no new table access.
* **A constant spec.** Nothing in it depends on the user, the schedule or the turn, so it is pinned
  by a digest test. It is appended after every other injected family in ``_build_turn_tools``,
  so no other spec moves in ``toolConfig``.
* **Gated on the request, not the catalog.** Built only for a headless session whose request
  carries ``run_mode: "watch"`` while the Runtime's ``PROACTIVE_RUNS_ENABLED`` is on, so it is in
  neither ``INJECTED_TOOL_IDS`` nor ``KEY_DESCRIBED_INJECTED_TOOL_IDS``. The bounds below are told
  to the model; the worker enforces them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from strands import tool

#: The tool's name, as the worker finds it in the run's tool trace.
WATCH_REPORT_TOOL_NAME = "report"

#: What the tool tells the model; the worker reads the call's input, not this.
RECORDED = "Recorded."

#: Told to the model and enforced by the worker.
MAX_ITEMS = 10
MAX_TITLE_CHARS = 120
MAX_SUMMARY_CHARS = 300

_DESCRIPTION = (
    "You are on an unattended watcher run: nobody is reading this conversation. When you have "
    "finished checking, call report exactly once, as your last action. outcome nothing_new means "
    "nothing is new since the last run, and the user gets no notification. outcome notify sends "
    f"one notification per item: at most {MAX_ITEMS} items, title at most {MAX_TITLE_CHARS} "
    f"characters, summary at most {MAX_SUMMARY_CHARS}. Give each item a stable key (a message id "
    "or URL), so the same finding is never announced twice. state is carried to the next run: "
    "cursors (where to check from next time) and tracking (what you are keeping an eye on)."
)

_INPUT_SCHEMA: Dict[str, Any] = {
    "json": {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["nothing_new", "notify"]},
            "items": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Stable id of the finding"},
                        "title": {"type": "string", "maxLength": MAX_TITLE_CHARS},
                        "summary": {"type": "string", "maxLength": MAX_SUMMARY_CHARS},
                        "urgency": {"type": "string", "enum": ["now", "today", "fyi"]},
                    },
                    "required": ["key", "title"],
                },
            },
            "state": {
                "type": "object",
                "properties": {
                    "cursors": {"type": "object", "additionalProperties": {"type": "string"}},
                    "tracking": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "required": ["outcome"],
    }
}


def make_watch_report_tool():
    """A fresh ``report`` tool. It closes over nothing, so every instance has the same spec."""

    @tool(name=WATCH_REPORT_TOOL_NAME, description=_DESCRIPTION, inputSchema=_INPUT_SCHEMA)
    def report(
        outcome: Literal["nothing_new", "notify"],
        items: Optional[List[Dict[str, Any]]] = None,
        state: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Acknowledge the report. The worker reads the arguments from the stream."""
        return RECORDED

    return report


__all__ = ["RECORDED", "WATCH_REPORT_TOOL_NAME", "make_watch_report_tool"]
