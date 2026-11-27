"""A quiet watcher's run, the pure half (docs/specs/quiet-watcher.md §3.3).

The scheduled-runs worker does the I/O; this module decides. It builds the
``<watch_state>`` block that goes at the head of the run's user message, reads
the run's ``report`` call, dedupes its items against the keys the watcher has
already notified about, and works out the state to save. No AWS, no clock.

The block lives in the user message only, never the system prompt, so a
watcher's system prompt and ``toolConfig`` are the same on every run. It is
deterministic for a given state: cursors are sorted by name, and the seen keys
are the newest :data:`SEEN_IN_PROMPT`, oldest first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .watch_state import MAX_TEXT_CHARS, WatchState, append_seen

#: How many of the newest seen keys the prompt shows the model.
SEEN_IN_PROMPT = 50
#: The most items one run notifies about.
MAX_NOTIFY_ITEMS = 10
#: The longest inbox title kept.
MAX_TITLE_CHARS = 120
URGENCIES = ("now", "today", "fyi")
DEFAULT_URGENCY = "fyi"

_INSTRUCTION = (
    "This is an unattended watcher run. Check for anything new since the cursors above (with no "
    "cursors, everything is new). Report only findings whose keys are not in seen. Call the report "
    "tool exactly once, at the end: outcome nothing_new when nothing is new, otherwise notify with "
    "one item per new finding. Put the cursors and tracking the next run should start from in state."
)


@dataclass
class WatchItem:
    """One finding the run notifies about."""

    key: str
    title: str
    urgency: str = DEFAULT_URGENCY


@dataclass
class WatchOutcome:
    """What the worker does with a completed watch run."""

    #: ``True``: keep the conversation, mark it unread, write one inbox row per item.
    notify: bool
    items: List[WatchItem] = field(default_factory=list)
    #: Keys to append to ``seen``: the reported keys already seen (so they stay
    #: fresh) and the new ones notified. Never the overflow past the cap, which
    #: was not announced.
    seen_keys: List[str] = field(default_factory=list)
    #: The run's ``report.state``, or ``None`` for each part it didn't give, in
    #: which case the previous value is kept.
    cursors: Optional[Dict[str, Any]] = None
    tracking: Optional[List[Any]] = None


def build_watch_prompt(prompt_text: str, state: Optional[WatchState]) -> str:
    """The run's user message: the ``<watch_state>`` block, then the schedule's prompt."""
    block: Dict[str, Any] = {
        "lastRunAt": state.last_run_at if state is not None else None,
        "cursors": dict(sorted(state.cursors.items())) if state is not None else {},
        "tracking": list(state.tracking) if state is not None else [],
        "seen": list(state.seen[-SEEN_IN_PROMPT:]) if state is not None else [],
    }
    # "<" escaped (still valid JSON), so text the model wrote into its own state
    # on an earlier run can never close the block.
    rendered = json.dumps(block, ensure_ascii=False, separators=(", ", ": ")).replace("<", "\\u003c")
    return f"<watch_state>\n{rendered}\n{_INSTRUCTION}\n</watch_state>\n\n{prompt_text}"


def _key(value: Any) -> str:
    # Clamped exactly as ``WatchState`` stores a seen key, so a key compares equal to its stored form.
    return str(value).strip()[:MAX_TEXT_CHARS] if value is not None else ""


def _items(raw: Any, label: str) -> List[WatchItem]:
    """The report's items that have a key, deduped by key, in the order given."""
    if not isinstance(raw, list):
        return []
    items: List[WatchItem] = []
    keys = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = _key(entry.get("key"))
        if not key or key in keys:
            continue
        keys.add(key)
        title = " ".join(str(entry.get("title") or "").split()) or label
        urgency = entry.get("urgency")
        items.append(
            WatchItem(
                key=key,
                title=title[:MAX_TITLE_CHARS],
                urgency=urgency if urgency in URGENCIES else DEFAULT_URGENCY,
            )
        )
    return items


def _keep_for_writes(outcome: WatchOutcome, write_count: int) -> WatchOutcome:
    """A quiet outcome made not quiet by the run's writes; anything else unchanged."""
    if write_count <= 0 or outcome.notify:
        return outcome
    return WatchOutcome(
        notify=True,
        items=[],
        seen_keys=outcome.seen_keys,
        cursors=outcome.cursors,
        tracking=outcome.tracking,
    )


def decide_watch_outcome(
    report_input: Optional[Dict[str, Any]],
    final_message: str,
    seen: List[str],
    *,
    run_id: str,
    label: str,
    write_count: int = 0,
) -> WatchOutcome:
    """Apply the spec's outcome table to a completed run (:func:`_decide_from_report`).

    A run that made a ``write``-class call (``write_count``, the action audit's count,
    docs/specs/proactive-trust.md §7.3) is never quiet: it keeps its conversation even
    when it reported ``nothing_new``, with no inbox row unless it reported items. An
    ``unclassified`` call changes nothing.
    """
    outcome = _decide_from_report(report_input, final_message, seen, run_id=run_id, label=label)
    return _keep_for_writes(outcome, write_count)


def _decide_from_report(
    report_input: Optional[Dict[str, Any]],
    final_message: str,
    seen: List[str],
    *,
    run_id: str,
    label: str,
) -> WatchOutcome:
    """The outcome table, by what the run reported.

    - ``report(nothing_new)``: quiet.
    - ``report(notify)``: notify for the items whose keys are not in ``seen``
      (at most :data:`MAX_NOTIFY_ITEMS`); quiet when there are none. The model's
      word isn't trusted: the dedupe is here.
    - no ``report`` but some final text: notify, one item keyed on the run and
      titled with the schedule's label, so a finding is never swallowed.
    - no ``report``, no text: quiet.
    """
    if report_input is None:
        if final_message and final_message.strip():
            item = WatchItem(key=f"run:{run_id}", title=label[:MAX_TITLE_CHARS])
            # The run key can never recur, so it is not added to seen.
            return WatchOutcome(notify=True, items=[item])
        return WatchOutcome(notify=False)

    state = report_input.get("state") if isinstance(report_input.get("state"), dict) else {}
    cursors = state.get("cursors") if isinstance(state.get("cursors"), dict) else None
    tracking = state.get("tracking") if isinstance(state.get("tracking"), list) else None

    if report_input.get("outcome") == "nothing_new":
        return WatchOutcome(notify=False, cursors=cursors, tracking=tracking)

    already = set(seen)
    reported = _items(report_input.get("items"), label)
    fresh = [item for item in reported if item.key not in already][:MAX_NOTIFY_ITEMS]
    fresh_keys = {item.key for item in fresh}
    seen_keys = [item.key for item in reported if item.key in already or item.key in fresh_keys]
    return WatchOutcome(
        notify=bool(fresh),
        items=fresh,
        seen_keys=seen_keys,
        cursors=cursors,
        tracking=tracking,
    )


def next_watch_state(previous: Optional[WatchState], outcome: WatchOutcome, finished_at: str) -> WatchState:
    """The state to save after the run. ``WatchState`` clamps every bound."""
    prior = previous if previous is not None else WatchState()
    return WatchState(
        cursors=outcome.cursors if outcome.cursors is not None else prior.cursors,
        tracking=outcome.tracking if outcome.tracking is not None else prior.tracking,
        seen=append_seen(prior.seen, outcome.seen_keys),
        last_run_at=finished_at or None,
    )


__all__ = [
    "MAX_NOTIFY_ITEMS",
    "SEEN_IN_PROMPT",
    "WatchItem",
    "WatchOutcome",
    "build_watch_prompt",
    "decide_watch_outcome",
    "next_watch_state",
]
