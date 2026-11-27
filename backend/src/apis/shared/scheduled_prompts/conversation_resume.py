"""A watch run's pause answered in its conversation still saves its ``report`` (#2490).

docs/specs/answer-from-inbox.md §3.6. A pause answered from the bell is resumed by the
scheduled-runs worker, which reads the run's result and saves the resumed ``report`` through
``finish_resumed_run``. A pause answered in its conversation is resumed by ``/chat/stream``,
and nothing read that resume's outcome, so the watcher's cursors never moved and its next run
paused again on the same step.

The two halves, and why the hold is the token between them:

1. **The Runtime records the report.** When a watch run's resume finishes without pausing
   again, the Runtime writes ``watchResumeReport`` onto the session's ``META`` row **in the
   same write that clears** ``pausedTurn`` (``clear_paused_turn(watch_resume_report=…)``),
   after the stream. The Runtime can't tell a bell resume from a conversation one (the
   worker's request is the browser's) and doesn't know the schedule, so it records it every
   time.
2. **The worker applies it, once, at the hold check.** A watcher whose run paused carries a
   hold naming the paused session. A bell resume ends that hold in its own save
   (``save_watch_state(clear_pause=True)``), so nothing ever reads the record it left. A
   conversation resume leaves the hold, so the watcher's next tick finds the pause gone and
   the record there, and saves the report and ends the hold in **one** conditional write on
   the state version it read (``apply_conversation_resume``). The hold is gone after either
   save, so the report is saved exactly once whichever path answered, and a version that
   moved on writes nothing.

Like the bell's resume (settled question 3), this writes no ``agent_item`` rows: the user
was in the conversation. Stdlib and pydantic only, so the Runtime and the worker both import
it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, Optional

from .watch_run import decide_watch_outcome, next_watch_state
from .watch_state import WatchState, save_watch_state

logger = logging.getLogger(__name__)

#: The ``META`` attribute the Runtime writes and the worker reads.
WATCH_RESUME_REPORT_ATTR = "watchResumeReport"
#: The record's ceiling. ``report``'s schema bounds its own input (10 items of 120 + 300
#: characters, 20 cursors, 20 tracking entries) well under this; a record past it is dropped,
#: and the hold check then ends the hold as it did before.
MAX_RECORD_BYTES = 32 * 1024
_REPORT_TOOL = "report"


def report_from_messages(messages: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The input of the last ``report`` call in ``messages``, or ``None`` when it made none.

    Pass only the messages the resume appended, so a ``report`` made before the pause is not
    mistaken for the resumed run's: the bell's resume reads its own leg's calls the same way.
    """
    found: Optional[Dict[str, Any]] = None
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for block in message.get("content") or []:
            tool_use = block.get("toolUse") if isinstance(block, dict) else None
            if isinstance(tool_use, dict) and tool_use.get("name") == _REPORT_TOOL:
                found = tool_use.get("input") if isinstance(tool_use.get("input"), dict) else {}
    return found


def resume_report_record(report_input: Optional[Dict[str, Any]], finished_at: str) -> Optional[Dict[str, Any]]:
    """The ``watchResumeReport`` value for a finished watch-run resume, or ``None`` if too big."""
    record = {"report": report_input, "finishedAt": finished_at}
    try:
        size = len(json.dumps(record, default=str))
    except (TypeError, ValueError):
        return None
    if size > MAX_RECORD_BYTES:
        logger.warning("A watch run's resumed report is %d bytes; not recorded", size)
        return None
    return record


def apply_conversation_resume(
    user_id: str,
    schedule_id: str,
    label: str,
    state: WatchState,
    record: Dict[str, Any],
) -> bool:
    """Save a conversation resume's ``report`` and end the hold, in one conditional write.

    ``state`` is the version the worker read this tick; a write since then (another tick, a
    "Start over") makes this write nothing and return ``False``. Blocking, like
    ``save_watch_state``.
    """
    report = record.get("report") if isinstance(record.get("report"), dict) else None
    finished_at = record.get("finishedAt") if isinstance(record.get("finishedAt"), str) else ""
    # No final text is kept, and none is needed: without a report the outcome's only effect
    # would be an item keyed on the run, which is never added to `seen`.
    outcome = decide_watch_outcome(report, "", state.seen, run_id="conversation", label=label)
    return save_watch_state(
        user_id,
        schedule_id,
        next_watch_state(state, outcome, finished_at),
        expected_version=state.version,
        clear_pause=True,
    )


__all__ = [
    "MAX_RECORD_BYTES",
    "WATCH_RESUME_REPORT_ATTR",
    "apply_conversation_resume",
    "report_from_messages",
    "resume_report_record",
]
