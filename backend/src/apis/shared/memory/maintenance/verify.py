"""The deterministic verifier for a maintenance plan (Shared Projects §4.6 step 4).

The planner is a model, so nothing it proposes reaches a reviewer unchecked.
Every op is checked against the file it was planned for, and one that fails is
dropped with a stable ``code`` rather than repaired:

- **Shape.** Known type, the right number of distinct items, text for a merge.
  A ``why`` that cites the planner's item numbers is cleared: the reviewer
  never sees them.
- **Identity.** Every item exists, and no item is used by two ops.
- **Pins.** A pinned item never leaves the file. A merge may rewrite one pinned
  item, which keeps its anchor; two pinned items can't merge.
- **A merge says nothing new and loses nothing.** It is for items that repeat
  each other, so its text must carry exactly the numbers and dates, links,
  URLs, emails and code spans of its sources, mention no name they don't and
  drop none they do, keep what any source's reason says (its content words,
  with or without its "because"), and be no longer than the sources together.
  These are cheap token checks, not a judge of
  meaning: they block invented and dropped facts, which are the failures that
  cost a team something, and they accept the plain rewording a merge needs.
- **A supersede goes forward.** The replacing item may not be older than the
  one it replaces, when provenance says when each was written.
- **A prune is only for the past.** The item must carry a date, and every date
  in it must have passed.

Pure functions: no I/O.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..format import Item, MemoryFormatError, check_item_text, extract_links
from ..models import DroppedOp, ItemProvenance, MaintenanceOp, MaintenanceVerification, OpSource


@dataclass(frozen=True)
class PlannedChange:
    """One change as the planner returned it. ``ids`` are 1-based positions in the file."""

    type: str
    ids: Tuple[int, ...]
    text: Optional[str] = None
    reason: Optional[str] = None
    why: str = ""


_NUMBER_RE = re.compile(r"\d+(?:[.,:/-]\d+)*")
_THOUSANDS_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_URL_RE = re.compile(r"https?://[^\s)>\]]+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CODE_RE = re.compile(r"`([^`]+)`")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’-]*")
_RATIONALE_RE = re.compile(
    r"\b(because|since|so that|due to|in order to|to avoid|as required by|which is why)\b", re.IGNORECASE
)
_SENTENCE_BREAK = re.compile(r"[.!?:;]\s*$")
# The planner sees items numbered; a reason that cites a number means nothing to the reviewer.
_ITEM_NUMBER_RE = re.compile(r"(?:\bitems?\b|#)\s*\[?\d", re.IGNORECASE)

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ISO_DAY_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_ISO_MONTH_RE = re.compile(r"\b(\d{4})-(\d{2})\b(?!-\d)")
_NAMED_DAY_RE = re.compile(rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.IGNORECASE)
_DAY_NAMED_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES})\.?,?\s+(\d{{4}})\b", re.IGNORECASE)
_NAMED_MONTH_RE = re.compile(rf"\b({_MONTH_NAMES})\.?\s+(\d{{4}})\b", re.IGNORECASE)


def _numbers(text: str) -> Set[str]:
    found = set()
    for token in _NUMBER_RE.findall(text):
        found.add(token.replace(",", "") if _THOUSANDS_RE.fullmatch(token) else token)
    return found


def _references(text: str) -> Set[str]:
    return set(_URL_RE.findall(text)) | {e.lower() for e in _EMAIL_RE.findall(text)} | set(_CODE_RE.findall(text))


def _links(text: str) -> Set[str]:
    return {name.lower() for name in extract_links(text)}


def _words(text: str) -> Set[str]:
    return {w.lower() for w in _WORD_RE.findall(text)}


def _names(text: str) -> Set[str]:
    """Capitalized words that don't open a sentence or a line: a rough cut at proper nouns."""
    names = set()
    for line in text.split("\n"):
        previous = ""
        for match in _WORD_RE.finditer(line):
            word = match.group(0)
            opens = not previous or _SENTENCE_BREAK.search(line[: match.start()]) is not None
            if word[0].isupper() and not opens:
                names.add(word.lower())
            previous = word
    return names


def _last_day(year: int, month: int) -> Optional[date]:
    if not 1 <= month <= 12:
        return None
    return date(year, month, calendar.monthrange(year, month)[1])


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def dates_in(text: str) -> List[date]:
    """Every date the text names, a month standing for its last day."""
    found: List[Optional[date]] = []
    consumed = text
    for m in _ISO_DAY_RE.finditer(text):
        found.append(_safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    consumed = _ISO_DAY_RE.sub(" ", consumed)
    for m in _ISO_MONTH_RE.finditer(consumed):
        found.append(_last_day(int(m.group(1)), int(m.group(2))))
    for m in _NAMED_DAY_RE.finditer(consumed):
        found.append(_safe_date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))))
    consumed = _NAMED_DAY_RE.sub(" ", consumed)
    for m in _DAY_NAMED_RE.finditer(consumed):
        found.append(_safe_date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))))
    consumed = _DAY_NAMED_RE.sub(" ", consumed)
    for m in _NAMED_MONTH_RE.finditer(consumed):
        found.append(_last_day(int(m.group(2)), _MONTHS[m.group(1).lower()]))
    return [d for d in found if d is not None]


def _union(texts: Iterable[str], extract) -> Set[str]:
    out: Set[str] = set()
    for t in texts:
        out |= extract(t)
    return out


def _check_merge(text: str, sources: Sequence[str]) -> Optional[Tuple[str, str]]:
    """``(code, detail)`` for the first rule a merge breaks, or None."""
    if len(text) > sum(len(s) for s in sources):
        return "grew", "The merged item is longer than the items it replaces."
    for code, extract, label in (
        ("number", _numbers, "numbers or dates"),
        ("link", _links, "links"),
        ("reference", _references, "URLs, emails or code"),
    ):
        before, after = _union(sources, extract), extract(text)
        if after - before:
            return f"new_{code}", f"It adds {label} the sources don't have: {', '.join(sorted(after - before))}."
        if before - after:
            return f"lost_{code}", f"It drops {label} the sources have: {', '.join(sorted(before - after))}."
    source_words = _union(sources, _words)
    new_names = _names(text) - source_words
    if new_names:
        return "new_name", f"It mentions names the sources don't: {', '.join(sorted(new_names))}."
    lost_names = _union(sources, _names) - _words(text)
    if lost_names:
        return "lost_name", f"It drops names the sources have: {', '.join(sorted(lost_names))}."
    merged_words = _words(text)
    for source in sources:
        for clause in _reason_clauses(source):
            missing = clause - merged_words if clause else set()
            if missing or (not clause and not _RATIONALE_RE.search(text)):
                return "lost_rationale", "A source gives a reason and the merged item doesn't keep it."
    return None


def _reason_clauses(text: str) -> List[Set[str]]:
    """The content words of each reason a text gives: what follows "because", "so that" and the like.

    A merge keeps a reason when it keeps what the reason says, with or without
    the word that introduced it: "…in groups of 50; larger batches hit the rate
    limit" keeps "…, because larger batches hit the rate limit".
    """
    clauses = []
    for match in _RATIONALE_RE.finditer(text):
        rest = re.split(r"[.;!?]", text[match.end():], maxsplit=1)[0]
        clauses.append({w for w in _words(rest) if len(w) >= 4})
    return clauses


def _written_at(provenance: Optional[ItemProvenance]) -> str:
    if provenance is None:
        return ""
    return provenance.updated_at or provenance.added_at or ""


def verify_plan(
    changes: Sequence[PlannedChange],
    items: Sequence[Item],
    *,
    pinned: Sequence[str] = (),
    provenance: Optional[Dict[str, ItemProvenance]] = None,
    today: date,
) -> Tuple[List[MaintenanceOp], MaintenanceVerification]:
    """Check each planned change in order. Returns the ops that passed and the report."""
    pins = set(pinned)
    prov = provenance or {}
    report = MaintenanceVerification(planned=len(changes))
    kept: List[MaintenanceOp] = []
    consumed: Set[str] = set()

    def drop(change: PlannedChange, code: str, detail: str = "") -> None:
        report.dropped.append(DroppedOp(type=change.type, code=code, detail=detail))

    for change in changes:
        if change.type not in ("merge", "supersede", "prune"):
            drop(change, "unknown_type", f"'{change.type}' is not a change maintenance makes.")
            continue
        expected = {"supersede": 2, "prune": 1}.get(change.type)
        ids = list(change.ids)
        if len(set(ids)) != len(ids) or (expected is not None and len(ids) != expected) or (
            change.type == "merge" and len(ids) < 2
        ):
            drop(change, "bad_shape", "The change names the wrong number of items.")
            continue
        if any(i < 1 or i > len(items) or not items[i - 1].anchor for i in ids):
            drop(change, "unknown_item", "The change names an item the file doesn't have.")
            continue
        sources = [OpSource(anchor=items[i - 1].anchor or "", text=items[i - 1].text) for i in ids]
        anchors = [s.anchor for s in sources]
        if set(anchors) & consumed:
            drop(change, "overlap", "Another change already uses one of these items.")
            continue

        why = " ".join((change.why or "").split())[:300]
        op = MaintenanceOp(type=change.type, sources=sources, why="" if _ITEM_NUMBER_RE.search(why) else why)
        if change.type == "merge":
            pinned_sources = [a for a in anchors if a in pins]
            if len(pinned_sources) > 1:
                drop(change, "pinned", "Two pinned items can't be merged.")
                continue
            if not (change.text or "").strip():
                drop(change, "missing_text", "A merge needs the item that replaces them.")
                continue
            try:
                text = check_item_text(change.text or "")
            except MemoryFormatError as exc:
                drop(change, "invalid_text", str(exc))
                continue
            failure = _check_merge(text, [s.text for s in sources])
            if failure:
                drop(change, *failure)
                continue
            op.text = text
            op.keep = pinned_sources[0] if pinned_sources else anchors[0]
        elif change.type == "supersede":
            if anchors[0] in pins:
                drop(change, "pinned", "The item it replaces is pinned.")
                continue
            old_at, new_at = _written_at(prov.get(anchors[0])), _written_at(prov.get(anchors[1]))
            if old_at and new_at and new_at < old_at:
                drop(change, "not_newer", "The replacing item was written before the one it replaces.")
                continue
        else:
            if change.reason != "expired":
                drop(change, "bad_reason", "Maintenance prunes only items whose dates have passed.")
                continue
            if anchors[0] in pins:
                drop(change, "pinned", "The item is pinned.")
                continue
            dates = dates_in(sources[0].text)
            if not dates or max(dates) >= today:
                drop(change, "not_expired", "The item has no date, or a date that hasn't passed.")
                continue
            op.reason = "expired"
        consumed.update(anchors)
        kept.append(op)

    report.kept = len(kept)
    return kept, report
