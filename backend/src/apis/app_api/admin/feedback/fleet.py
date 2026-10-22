"""Fleet-level feedback attribution — response-feedback spec §7 (the
"config-dimension attribution" the spec calls the prize) and the compaction
spec §7.2 ("outcome signal joined to compaction").

The question this answers is deliberately not *"what is our quality score"*.
Spec §9 forbids that number existing, because an absolute rate over a
self-selected few percent of turns is noise that reads like a KPI. What it
answers is **"does the down-thumb rate differ between config arms we chose?"**
— model, agent switch, distance from a compaction cut, document turn class.

Three rules from §9, enforced here rather than promised:

* **Every arm carries its own ``n``.** A rate without one is not reportable.
* **Arms below the coverage floor are flagged** (:data:`DEFAULT_MINIMUM_N`),
  so a one-of-one arm renders greyed rather than as a finding. The spec's
  open question 3 said to pick that number before the panel exists; this is
  the pick, and it is an env-tunable constant rather than a magic literal.
* **No single-number rate exists at the top level.** ``totals`` carries up
  and down counts and nothing derives a headline score from them. That
  absence is the design.

Everything here is a pure function over rows that already passed the
content-free projections, so no conversation content can reach it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from apis.app_api.admin.costs.service import turn_class

#: Below this many thumbs an arm's rate is not reportable. Picked, not
#: derived: 20 is the point where one extra down-thumb moves the rate by
#: five points rather than fifty, which is the coarsest resolution worth
#: showing an admin. Tune per environment with ``FEEDBACK_ARM_MINIMUM_N``.
DEFAULT_MINIMUM_N = 20

#: Buckets for "how far was this call from the last compaction cut" — the
#: §7.2 question. Distance is in *model calls*, not turns, because the ``C#``
#: rows are per call and a turn may be several of them.
COMPACTION_BUCKETS: Tuple[str, ...] = ("never", "same call", "1-3 calls", "4+ calls")

#: The ``compactionEvents`` kind that marks a cut reaching the model: the
#: first call sent on the sliced history (a restore-time slice or a parked
#: cut promoted at the head of a turn). ``checkpoint`` is only the decision —
#: it lands on the call whose input triggered it, which the model answered on
#: the full history — so counting it would put a pre-cut answer in the
#: "same call" arm.
COMPACTION_APPLIED_KIND = "applied"


def minimum_n() -> int:
    """The coverage floor, env-tunable."""
    raw = os.environ.get("FEEDBACK_ARM_MINIMUM_N", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MINIMUM_N
    except ValueError:
        return DEFAULT_MINIMUM_N
    return max(1, value)


@dataclass
class ArmCounts:
    """One arm of one dimension: the thumbs that landed on calls with it."""

    up: int = 0
    down: int = 0

    @property
    def n(self) -> int:
        return self.up + self.down

    def add(self, value: int) -> None:
        if value == 1:
            self.up += 1
        elif value == -1:
            self.down += 1

    def to_dict(self, floor: int) -> Dict[str, Any]:
        n = self.n
        return {
            "up": self.up,
            "down": self.down,
            "n": n,
            # Rate is None below the floor rather than a number nobody should
            # quote — the UI has nothing to render misleadingly.
            "downRate": round(self.down / n, 4) if n >= floor and n else None,
            "belowFloor": n < floor,
        }


@dataclass
class SessionIndex:
    """One session's cost rows, indexed for the joins this module makes."""

    #: messageId -> the last ``C#`` row carrying it (a turn's tool round trips
    #: share a messageId; the last row has the fullest context, matching the
    #: per-session profile's own rule).
    by_message: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    #: messageId -> that row's position in the session's chronological order.
    position: Dict[int, int] = field(default_factory=dict)
    #: Ordered positions of the calls a compaction cut first reached.
    compaction_positions: List[int] = field(default_factory=list)


def index_session(records: Sequence[Dict[str, Any]]) -> SessionIndex:
    """Index one session's ``C#`` rows, assumed chronological (the storage
    reader queries the GSI with ``ScanIndexForward=True``)."""
    index = SessionIndex()
    for position, record in enumerate(records):
        message_id = _as_int(record.get("messageId"))
        if message_id is not None:
            index.by_message[message_id] = record
            index.position[message_id] = position
        events = record.get("compactionEvents")
        if isinstance(events, list) and any(
            isinstance(e, dict) and e.get("kind") == COMPACTION_APPLIED_KIND for e in events
        ):
            index.compaction_positions.append(position)
    return index


def calls_since_compaction(index: SessionIndex, message_id: int) -> str:
    """Which :data:`COMPACTION_BUCKETS` bucket this message sits in.

    "never" means no cut had happened in this session at or before the call —
    the control arm the §7.2 question needs, and the reason a session that
    was never compacted still contributes to the comparison.
    """
    position = index.position.get(message_id)
    if position is None:
        return "never"
    prior = [p for p in index.compaction_positions if p <= position]
    if not prior:
        return "never"
    distance = position - prior[-1]
    if distance == 0:
        return "same call"
    if distance <= 3:
        return "1-3 calls"
    return "4+ calls"


def build_fleet_report(
    thumbs: Sequence[Dict[str, Any]],
    records_by_session: Dict[str, Sequence[Dict[str, Any]]],
    *,
    window: Dict[str, Any],
    floor: Optional[int] = None,
    truncated: bool = False,
    sessions_omitted: int = 0,
) -> Dict[str, Any]:
    """Aggregate thumbs into per-dimension arms.

    ``thumbs`` are explicit feedback rows (implicit signals never carry the
    queue keys, so they cannot appear here). ``records_by_session`` holds the
    ``C#`` rows of the sessions that were joined — a thumb whose session is
    absent, or whose message has no cost row, counts toward the totals and
    toward ``coverage.unjoined``, never silently into an arm.
    """
    floor = floor if floor is not None else minimum_n()
    indexes = {sid: index_session(rows) for sid, rows in records_by_session.items()}

    totals = ArmCounts()
    reasons: Dict[str, int] = {}
    arms: Dict[str, Dict[str, ArmCounts]] = {
        "model": {},
        "agentSwitch": {},
        "callsSinceCompaction": {},
        "turnClass": {},
    }
    joined = 0
    sessions_seen = set()

    for row in thumbs:
        if row.get("signal") not in (None, "explicit"):
            continue
        value = _as_int(row.get("value"))
        if value not in (1, -1):
            continue
        totals.add(value)
        session_id = str(row.get("sessionId") or "")
        if session_id:
            sessions_seen.add(session_id)
        if value == -1:
            reason = row.get("reason")
            key = reason if isinstance(reason, str) and reason else "unspecified"
            reasons[key] = reasons.get(key, 0) + 1

        message_id = _as_int(row.get("messageId"))
        index = indexes.get(session_id)
        record = index.by_message.get(message_id) if index and message_id is not None else None
        if record is None:
            continue
        joined += 1

        model_id = (record.get("modelInfo") or {}).get("modelId")
        if model_id:
            arms["model"].setdefault(str(model_id), ArmCounts()).add(value)
        switched = "switched agent" if record.get("agentSwitched") else "same agent"
        arms["agentSwitch"].setdefault(switched, ArmCounts()).add(value)
        arms["callsSinceCompaction"].setdefault(
            calls_since_compaction(index, message_id), ArmCounts()
        ).add(value)
        klass = turn_class(record)
        if klass is not None:
            arms["turnClass"].setdefault(klass, ArmCounts()).add(value)

    n_thumbs = totals.n
    return {
        "window": window,
        # Counts, never a derived headline rate — see the module docstring.
        "totals": {"up": totals.up, "down": totals.down, "thumbs": n_thumbs},
        "coverage": {
            "thumbs": n_thumbs,
            "joined": joined,
            "unjoined": n_thumbs - joined,
            "joinRate": round(joined / n_thumbs, 4) if n_thumbs else None,
            "sessionsWithFeedback": len(sessions_seen),
            "sessionsJoined": len(records_by_session),
            "sessionsOmitted": sessions_omitted,
            "truncated": truncated,
        },
        "arms": {
            dimension: _render_arm(counts, floor, dimension)
            for dimension, counts in arms.items()
        },
        "reasons": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        "minimumN": floor,
    }


def _render_arm(counts: Dict[str, ArmCounts], floor: int, dimension: str) -> List[Dict[str, Any]]:
    """Arms as a list, ordered so a reader compares like with like: the
    compaction dimension keeps its natural distance order, everything else
    leads with the largest sample."""
    if dimension == "callsSinceCompaction":
        ordered: Iterable[str] = [k for k in COMPACTION_BUCKETS if k in counts]
    else:
        ordered = sorted(counts, key=lambda k: (-counts[k].n, k))
    return [{"key": key, **counts[key].to_dict(floor)} for key in ordered]


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
