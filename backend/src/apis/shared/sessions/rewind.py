"""Rewind state for a conversation: which stored messages a rewind has hidden.

``docs/specs/conversation-rewind-and-fork.md`` §4. AgentCore Memory events are
append-only, and message ids are positional (``msg-{sessionId}-{index}``), with
cost rows, feedback, ``displayText`` and tool summaries all keyed on that
index. So a rewind never deletes anything. It records a **span** of physical
indices on the session's metadata row (the same item that holds
``compaction``), and every reader skips what a span covers.

Two coordinate systems, and the whole module is about keeping them apart:

- **Physical** index: position in ``list_messages`` order. What message ids,
  ``GET /messages`` and the per-turn message-index base use. Never reused.
- **Logical** index: position once spanned messages are removed. What the
  agent sees, and what compaction's ``checkpoint`` and ``truncation_anchor``
  are expressed in.

A new span always runs from the cut to the current physical end, so a rewind
never changes the logical index of any message below its cut. That is what
keeps compaction state valid across any number of rewinds.

Pure: no I/O here. app-api writes the state (``rewind_service``), and both
``GET /messages`` and the Runtime's session manager read it from the row they
already load.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# The row attribute. Top-level on the static ``S#`` session row.
REWIND_ATTRIBUTE = "rewind"

# Per-rewind records kept for the warm agent (``cuts_since``). A container
# that missed more rewinds than this falls back to the conservative path; the
# spans themselves are never trimmed.
_MAX_CUTS = 20


@dataclass(frozen=True)
class RewindCut:
    """One rewind, as the warm agent needs to replay it.

    ``cut`` is the logical length the conversation has right after the rewind
    (the logical index the rewound user message held). ``visible_before`` is
    the logical length just before it, so a live list can tell whether it is
    in the state this cut was taken against.
    """

    epoch: int
    cut: int
    visible_before: int

    def to_dict(self) -> Dict[str, int]:
        return {"epoch": self.epoch, "cut": self.cut, "visibleBefore": self.visible_before}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RewindCut":
        return cls(
            epoch=int(data.get("epoch", 0)),
            cut=int(data.get("cut", 0)),
            visible_before=int(data.get("visibleBefore", 0)),
        )


@dataclass(frozen=True)
class RewindState:
    """The ``rewind`` attribute of a session row. ``epoch`` 0 means never rewound."""

    epoch: int = 0
    spans: Tuple[Tuple[int, int], ...] = ()
    cuts: Tuple[RewindCut, ...] = field(default=())
    updated_at: Optional[str] = None

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "epoch": self.epoch,
            "spans": [[start, end] for start, end in self.spans],
            "cuts": [c.to_dict() for c in self.cuts],
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RewindState":
        """Parse a stored attribute. Anything malformed reads as "never rewound"
        for that part rather than raising, because a reader that raised here
        would take ``GET /messages`` or a turn down with it."""
        if not isinstance(data, dict):
            return cls()
        spans: List[Tuple[int, int]] = []
        for raw in data.get("spans") or []:
            try:
                start, end = int(raw[0]), int(raw[1])
            except (TypeError, ValueError, IndexError):
                continue
            if 0 <= start < end:
                spans.append((start, end))
        cuts: List[RewindCut] = []
        for raw in data.get("cuts") or []:
            if isinstance(raw, dict):
                try:
                    cuts.append(RewindCut.from_dict(raw))
                except (TypeError, ValueError):
                    continue
        try:
            epoch = int(data.get("epoch") or 0)
        except (TypeError, ValueError):
            epoch = 0
        return cls(
            epoch=epoch,
            spans=_merge_spans(spans),
            cuts=tuple(sorted(cuts, key=lambda c: c.epoch)),
            updated_at=data.get("updatedAt") if isinstance(data.get("updatedAt"), str) else None,
        )

    @classmethod
    def from_row(cls, row: Optional[Dict[str, Any]]) -> "RewindState":
        return cls.from_dict((row or {}).get(REWIND_ATTRIBUTE))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return bool(self.spans)

    def is_hidden(self, physical: int) -> bool:
        return any(start <= physical < end for start, end in self.spans)

    def logical_index(self, physical: int) -> int:
        """Logical index of a physical position: how many visible messages precede it."""
        hidden = 0
        for start, end in self.spans:
            if start >= physical:
                break
            hidden += min(end, physical) - start
        return physical - hidden

    def visible(self, items: Iterable[Any]) -> List[Tuple[int, Any]]:
        """``(physical_index, item)`` for every item a span does not cover."""
        return [(i, item) for i, item in enumerate(items) if not self.is_hidden(i)]

    def cuts_since(self, epoch: int) -> List[RewindCut]:
        """The rewinds after ``epoch``, oldest first. Empty when nothing is newer."""
        return [c for c in self.cuts if c.epoch > epoch]

    def covers_epochs_since(self, epoch: int) -> bool:
        """Whether ``cuts`` still holds every rewind after ``epoch`` (it is bounded)."""
        needed = self.epoch - epoch
        return needed <= 0 or len(self.cuts_since(epoch)) == needed

    # ------------------------------------------------------------------
    # The one mutation
    # ------------------------------------------------------------------

    def with_cut(self, physical_cut: int, physical_end: int, *, updated_at: Optional[str] = None) -> "RewindState":
        """The state after rewinding to just before ``physical_cut``.

        ``physical_end`` is the session's current stored message count. The new
        span is always ``[physical_cut, physical_end)``: everything from the cut
        to the end, which is what keeps logical indices below the cut stable.
        """
        if not 0 <= physical_cut < physical_end:
            raise ValueError(f"cut {physical_cut} is outside [0, {physical_end})")
        epoch = self.epoch + 1
        cut = RewindCut(
            epoch=epoch,
            cut=self.logical_index(physical_cut),
            visible_before=self.logical_index(physical_end),
        )
        return RewindState(
            epoch=epoch,
            spans=_merge_spans([*self.spans, (physical_cut, physical_end)]),
            cuts=(*self.cuts, cut)[-_MAX_CUTS:],
            updated_at=updated_at,
        )


def _merge_spans(spans: Iterable[Tuple[int, int]]) -> Tuple[Tuple[int, int], ...]:
    """Sorted, with overlapping and touching spans merged."""
    merged: List[List[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def parse_message_index(session_id: str, message_id: str) -> Optional[int]:
    """The physical index in ``msg-{session_id}-{index}``, or None if it is not one of this session's ids."""
    prefix = f"msg-{session_id}-"
    if not message_id.startswith(prefix):
        return None
    tail = message_id[len(prefix):]
    if not tail.isdigit():
        return None
    return int(tail)


def is_rewind_target(message: Any) -> bool:
    """Whether a stored message is a valid cut point: a user message with text and no tool results.

    The same rule as ``TurnBasedSessionManager._find_valid_cutoff_indices``. A
    tool-result message is a user-role message too, and so is a mid-turn steer
    (a text block riding on one); neither starts a turn, so cutting before one
    would leave a dangling tool use.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    has_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        if "toolResult" in block:
            return False
        if isinstance(block.get("text"), str) and block["text"].strip():
            has_text = True
    return has_text
