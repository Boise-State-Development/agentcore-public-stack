"""Applying maintenance ops to a file's items (Shared Projects 2.6, §4.6 step 5).

Ops are addressed by anchor and carry each source's text from when they were
planned. An op applies only while all of its sources still read that way, so
approving a proposal against a file edited since the run keeps the edit and
skips just the ops it touched. Pure functions: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..format import Item
from ..models import MaintenanceOp

# Why each op type takes an item out, as the archive records it.
ARCHIVE_REASONS = {"merge": "merged", "supersede": "superseded", "prune": "pruned"}


@dataclass
class CompactionResult:
    """The file after the ops that could apply, and what happened to the rest.

    ``archive`` maps every anchor that left the file to ``(reason, superseded_by)``
    for its archive row. ``applied`` and ``skipped`` index into the op list.
    """

    items: Tuple[Item, ...]
    applied: List[int] = field(default_factory=list)
    skipped: List[int] = field(default_factory=list)
    archive: Dict[str, Tuple[str, Optional[str]]] = field(default_factory=dict)


def op_applies(op: MaintenanceOp, current: Dict[str, str], pinned: Set[str], consumed: Set[str]) -> bool:
    """Whether ``op`` can still apply to a file whose items are ``current`` (anchor → text)."""
    for source in op.sources:
        if current.get(source.anchor) != source.text or source.anchor in consumed:
            return False
    # A pin set since the run still protects its item: it may not leave the file.
    return not (set(op.removed) & pinned)


def apply_ops(
    items: Sequence[Item],
    ops: Sequence[MaintenanceOp],
    *,
    pinned: Sequence[str] = (),
    selected: Optional[Sequence[int]] = None,
) -> CompactionResult:
    """Apply the ``selected`` ops (all when None), in order, to ``items``.

    A merge puts its text on the kept item, where that item stood; the other
    sources leave. A supersede removes the older item and leaves the newer one
    alone. A prune removes its item. An op that no longer applies is skipped;
    an index outside ``ops`` raises ``IndexError``.
    """
    chosen = list(range(len(ops))) if selected is None else list(dict.fromkeys(selected))
    for index in chosen:
        if index < 0 or index >= len(ops):
            raise IndexError(index)
    current = {i.anchor: i.text for i in items if i.anchor}
    pins = set(pinned)
    consumed: Set[str] = set()
    rewritten: Dict[str, str] = {}
    result = CompactionResult(items=tuple(items))
    for index in chosen:
        op = ops[index]
        if not op_applies(op, current, pins, consumed):
            result.skipped.append(index)
            continue
        consumed.update(op.anchors)
        result.applied.append(index)
        if op.type == "merge":
            rewritten[op.keep or op.anchors[0]] = op.text or ""
            survivor: Optional[str] = op.keep
        elif op.type == "supersede":
            survivor = op.anchors[1]
        else:
            survivor = None
        for anchor in op.removed:
            result.archive[anchor] = (ARCHIVE_REASONS[op.type], survivor)
    result.items = tuple(
        Item(text=rewritten.get(i.anchor or "", i.text), anchor=i.anchor)
        for i in items
        if (i.anchor or "") not in result.archive
    )
    return result
