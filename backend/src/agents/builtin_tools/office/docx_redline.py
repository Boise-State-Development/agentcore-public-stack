"""Write Word tracked changes and comments into a .docx.

The engine behind ``redline_word_document`` (``docs/specs/word-tracked-changes.md``
§3). A change is ``find`` text in one paragraph plus new wording (``replace``),
a comment, or both. Each one becomes a real revision: the old wording moves into
``w:del`` (its ``w:t`` renamed ``w:delText``), the new wording is a run inside
``w:ins``, both carry an author and a date, and a comment is anchored with
``w:commentRangeStart`` / ``w:commentRangeEnd`` and a ``w:commentReference`` run.
Word shows each one in the Reviewing pane, and Accept All gives the new text.

Two rules keep the redline readable:

* **Only the words that differ are marked.** ``within thirty (30) days`` →
  ``within ninety (90) days`` deletes ``thirty (30)`` and inserts ``ninety
  (90)``. A replacement that only adds wording at a word boundary becomes a pure
  insertion, and one that only removes wording a pure deletion.
* **All or nothing.** Every change is located before any is applied. A miss, an
  ambiguous match, two changes that overlap, or a match that would split
  existing tracked changes, a hyperlink, a field or an embedded object raises
  :class:`RedlineError` with one line per problem, and nothing is written.

**Unplaced changes as comments** (``if_unplaced="comment"``, spec §3b). Instead
of failing, each change the *document* defeats (wording not found, ambiguous, an
occurrence out of range, a run into locked markup, an overlap, a ``like`` that
names no single paragraph, an insertion point that cannot take one) becomes an
"ACTION NEEDED —" comment on the paragraph it most likely meant, and the rest
are applied exactly as ``"fail"`` would apply them on their own: same markup,
same revision and comment ids, because the comments are written last. A change
that overlaps one already placed is the later one in request order. A problem
that is the *caller's* (an empty ``find``, neither ``replace`` nor ``comment``,
a malformed insertion) still fails the call, since no document would place it.

Matching treats curly and straight quotes, dash variants and non-breaking spaces
as equal, and any run of whitespace as one, because a model rarely reproduces a
contract's typography exactly. It matches whole words: ``pay`` never matches
inside ``payable``.

**Whole new paragraphs** (``Change.insert``, spec §3a). A change can instead add
paragraphs before or after the one ``find`` names: a new numbered section, a
list item, a clause. Each new paragraph copies the paragraph properties
(``w:pPr``: style, ``w:numPr`` numbering, indentation, spacing) of the paragraph
its ``like`` wording names, or of the anchor, so a paragraph like a numbered one
takes the list's next number. Its runs copy that paragraph's run formatting: the
body wording takes its dominant run's ``w:rPr``, and an optional ``lead`` (an
inline heading such as ``Survival.``) its first worded run's, which is how a
bold lead-in stays bold. The runs sit in ``w:ins`` and the paragraph mark is
itself a revision (``w:pPr/w:rPr/w:ins``), so Reject All removes the paragraph
whole rather than leaving an empty one. After the last paragraph of a table
cell, of the document, or before a table there is no following paragraph mark
to merge into, so the anchor's own mark becomes the inserted one and the last
new paragraph keeps the anchor's formatting (which ``_locate`` enforces).

**Whole paragraphs deleted** (``Change.delete``, spec §3c). A change can instead
delete the paragraph ``find`` names, or with ``through`` every paragraph from
it to a later one in the same container: a lettered item, a whole section from
heading to last paragraph. Every run moves into ``w:del`` and each paragraph
mark is a deletion too (``w:pPr/w:rPr/w:del``), so Accept All removes the
paragraphs outright and the list or sections after them renumber, rather than
leaving an empty paragraph that still holds its number. The paragraph after the
deletion keeps its own mark, so its formatting is untouched. Where no paragraph
follows (the end of a table cell or of the document, or just before a table),
the last mark has to stay: the deletion then takes the mark of the paragraph
before it instead, which is only right when that paragraph is formatted the
same, so otherwise the call refuses. A container whose every paragraph is
deleted keeps one empty paragraph, as Word requires.

Before anything is saved, every edited paragraph is checked: with this call's
insertions dropped and its deletions restored, paragraph marks included, its
wording and paragraph properties must equal what they were, and every deleted
paragraph must hold no wording outside ``w:del``. A failure is a bug, and
nothing is written.

**Only the edited paragraphs are re-serialized.** ``word/document.xml`` is parsed
once to find text, but the output is the original bytes with each edited
outermost ``w:p`` spliced back in. ElementTree cannot be trusted to write a whole
part: it renames namespace prefixes and drops declarations that no element uses,
and ``mc:Ignorable`` names prefixes by those declarations, so Word would refuse
the file. The paragraph serializer here writes every prefix as the document
spells it and redeclares, on the paragraph, any prefix whose declaration sat
inside it. Every other part of the package is copied unchanged, apart from the
comments part, its content-type override and its relationship, which are added
when missing.

Stdlib only. ``python-docx`` has no revision API and is not a backend
dependency, and this needs no Code Interpreter round trip.
"""

from __future__ import annotations

import copy
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree
from xml.sax.saxutils import escape

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{W_NS}}}"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"

_DOCUMENT_PART = "word/document.xml"
_DOCUMENT_RELS = "word/_rels/document.xml.rels"
_CONTENT_TYPES = "[Content_Types].xml"
_COMMENTS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
_COMMENTS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"

#: Children a run may hold and still be split. Anything else (a field code, a
#: drawing, a footnote reference) locks the run: it is read, but never cut.
_SPLITTABLE_RUN_CHILDREN = frozenset(
    _W + name
    for name in ("rPr", "t", "tab", "br", "cr", "noBreakHyphen", "softHyphen", "lastRenderedPageBreak")
)
#: What each run child contributes to the paragraph's visible text.
_ATOM_TEXT = {_W + "tab": "\t", _W + "br": "\n", _W + "cr": "\n", _W + "noBreakHyphen": "-"}

#: Paragraph children whose wording is visible but which a change may not cut into.
_LOCKED_CONTAINERS = {
    _W + "ins": "an existing tracked change",
    _W + "moveTo": "an existing tracked change",
    _W + "hyperlink": "a hyperlink",
    _W + "fldSimple": "a field",
    _W + "smartTag": "a smart tag",
    _W + "customXml": "custom XML markup",
    _W + "sdt": "a content control",
    _W + "bdo": "bidirectional text",
    _W + "dir": "bidirectional text",
}
#: Paragraph children with no visible wording that a change may not span.
_LOCKED_INVISIBLE = {
    _W + "del": "an existing tracked change",
    _W + "moveFrom": "an existing tracked change",
}
#: Descendants not followed when reading a locked child's wording.
_TEXT_STOP = frozenset({_W + "p", _W + "del", _W + "moveFrom", _MC_FALLBACK, _W + "drawing", _W + "pict", _W + "object"})

#: Typography a model rarely reproduces, folded one character to one so match
#: offsets in the folded text are offsets in the original.
_FOLD = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
        " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    }
)

_TAG = re.compile(r"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")
_NAME = re.compile(r"^</?\s*([\w.:-]+)")
_XMLNS = re.compile(r"\sxmlns:([\w.-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_DEFAULT_XMLNS = re.compile(r"\sxmlns\s*=")
_W_ID = re.compile(r"\bw:id\s*=\s*[\"'](-?\d+)[\"']")
_WORD_TOKEN = re.compile(r"\S+|\s+")


class RedlineError(Exception):
    """The changes could not be applied. ``problems`` has one line per failure."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class NewParagraph:
    """A paragraph a change inserts.

    ``like`` is wording from the paragraph whose formatting it copies (default:
    the change's anchor). ``lead`` is an optional inline heading written before
    ``text`` in the formatting of that paragraph's first worded run.
    """

    text: str
    like: Optional[str] = None
    lead: Optional[str] = None


@dataclass(frozen=True)
class Change:
    """One requested edit. ``replace=""`` deletes; ``replace=None`` leaves the wording.

    With ``insert`` (``"after"`` or ``"before"``), ``find`` names the anchor
    paragraph and ``paragraphs`` are added next to it as tracked insertions;
    ``replace`` must then be ``None``.

    With ``delete="paragraph"``, the paragraph ``find`` names is deleted whole,
    mark included, and with ``through`` (wording of a later paragraph in the
    same container) every paragraph from it through that one.
    """

    find: str
    replace: Optional[str] = None
    comment: Optional[str] = None
    occurrence: Optional[int] = None
    insert: Optional[str] = None
    paragraphs: Tuple[NewParagraph, ...] = ()
    delete: Optional[str] = None
    through: Optional[str] = None


@dataclass
class RedlineResult:
    docx: bytes
    #: One line per change, in request order, for the tool's summary.
    applied: List[str] = field(default_factory=list)
    #: With ``if_unplaced="comment"``: one line per change left as an ACTION
    #: NEEDED comment instead, in request order.
    commented: List[str] = field(default_factory=list)


#: What ``redline(if_unplaced=...)`` accepts.
IF_UNPLACED = ("fail", "comment")


# ── Paragraph text ───────────────────────────────────────────────────────────


@dataclass
class _Segment:
    """One direct child of a paragraph and the span of visible text it holds."""

    element: ElementTree.Element
    start: int
    end: int
    text: str
    #: Why this child may not be cut into, or ``None`` for a splittable run.
    locked: Optional[str]

    @property
    def is_run(self) -> bool:
        return self.locked is None and self.element.tag == _W + "r"


def _run_text(run: ElementTree.Element) -> str:
    parts: List[str] = []
    for child in run:
        if child.tag == _W + "t":
            parts.append(child.text or "")
        else:
            parts.append(_ATOM_TEXT.get(child.tag, ""))
    return "".join(parts)


def _nested_text(node: ElementTree.Element) -> str:
    """Visible wording inside a locked child, not following into nested paragraphs."""
    parts: List[str] = []
    for child in node:
        if child.tag in _TEXT_STOP:
            continue
        if child.tag == _W + "r":
            parts.append("".join(t.text or "" for t in child if t.tag == _W + "t"))
        else:
            parts.append(_nested_text(child))
    return "".join(parts)


def _segments(paragraph: ElementTree.Element) -> List[_Segment]:
    segments: List[_Segment] = []
    pos = 0
    for child in paragraph:
        tag = child.tag
        if tag == _W + "pPr" or not isinstance(tag, str):
            continue
        if tag == _W + "r":
            if all(c.tag in _SPLITTABLE_RUN_CHILDREN for c in child):
                text, locked = _run_text(child), None
            else:
                text = "".join(t.text or "" for t in child if t.tag == _W + "t")
                locked = "a field or embedded object"
        elif tag in _LOCKED_CONTAINERS:
            text, locked = _nested_text(child), _LOCKED_CONTAINERS[tag]
        elif tag in _LOCKED_INVISIBLE:
            text, locked = "", _LOCKED_INVISIBLE[tag]
        else:
            # Bookmarks, proofing marks, comment ranges, math: no wording to cut.
            text, locked = "", None
        segments.append(_Segment(child, pos, pos + len(text), text, locked))
        pos += len(text)
    return segments


def _text(segments: Sequence[_Segment]) -> str:
    return "".join(seg.text for seg in segments)


def _locked_at(segments: Sequence[_Segment], offset: int) -> Optional[str]:
    """Why a run boundary cannot be made at ``offset``, if it cannot."""
    for seg in segments:
        if seg.locked and seg.start < offset < seg.end:
            return seg.locked
    return None


def _locked_within(segments: Sequence[_Segment], start: int, end: int) -> Optional[str]:
    """Why ``[start, end)`` cannot be marked as a revision, if it cannot."""
    for seg in segments:
        if not seg.locked:
            continue
        if seg.end > seg.start and seg.start < end and seg.end > start:
            return seg.locked
        if seg.end == seg.start and start < seg.start < end:
            return seg.locked
    return None


# ── Locating changes ─────────────────────────────────────────────────────────


@dataclass
class _Paragraph:
    element: ElementTree.Element
    unit: int  # index of its outermost paragraph, the unit that is re-serialized
    text: str
    folded: str


@dataclass
class _Located:
    index: int  # position in the request
    change: Change
    paragraph: _Paragraph
    start: int  # the find span
    end: int
    edit_start: int  # the span actually marked, after trimming to the words that differ
    edit_end: int
    insert: str


@dataclass
class _Miss:
    """A change that could not be placed.

    ``problem`` is the line ``"fail"`` mode reports. ``reason`` is the one plain
    sentence an ACTION NEEDED comment gives a reader of the document, and
    ``paragraph`` is where that comment goes. ``caller`` marks a problem with
    the request itself, which fails the call in every mode.
    """

    index: int
    problem: str
    reason: str = ""
    paragraph: Optional[_Paragraph] = None
    caller: bool = False


def _paragraphs(body: ElementTree.Element) -> Tuple[List[_Paragraph], List[ElementTree.Element]]:
    """Every paragraph worth matching, and the outermost paragraphs in order."""
    paragraphs: List[_Paragraph] = []
    units: List[ElementTree.Element] = []

    def walk(node: ElementTree.Element, unit: Optional[int], in_fallback: bool) -> None:
        for child in node:
            if not isinstance(child.tag, str):
                continue
            child_unit = unit
            if child.tag == _W + "p":
                if unit is None:
                    units.append(child)
                    child_unit = len(units) - 1
                if not in_fallback:
                    text = _text(_segments(child))
                    paragraphs.append(_Paragraph(child, child_unit, text, text.translate(_FOLD)))
            walk(child, child_unit, in_fallback or child.tag == _MC_FALLBACK)

    walk(body, None, False)
    return paragraphs, units


def _pattern(find: str) -> Optional[re.Pattern]:
    """Whitespace-insensitive, and whole-word at each end that is a word character,
    so ``pay`` does not match inside ``payable``."""
    words = find.translate(_FOLD).split()
    if not words:
        return None
    body = r"\s+".join(re.escape(word) for word in words)
    head = r"(?<!\w)" if words[0][0].isalnum() else ""
    tail = r"(?!\w)" if words[-1][-1].isalnum() else ""
    return re.compile(head + body + tail)


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _same_token(a: str, b: str) -> bool:
    if a.isspace() and b.isspace():
        return True
    return a.translate(_FOLD) == b.translate(_FOLD)


def _clean_boundary(left: Optional[str], piece: str, right: Optional[str]) -> bool:
    """True when ``piece`` sits between ``left`` and ``right`` without splitting a word."""
    if not piece:
        return False
    if left and left[-1].isalnum() and piece[0].isalnum():
        return False
    if right and right[0].isalnum() and piece[-1].isalnum():
        return False
    return True


def _trim(old: str, new: str) -> Tuple[int, int, str]:
    """``(start, end, insert)``: the part of ``old`` to delete and what replaces it.

    Common leading and trailing words are kept. When what is left differs only
    by wording added or removed at a word boundary, the edit narrows to that.
    """
    old_tokens = _WORD_TOKEN.findall(old)
    new_tokens = _WORD_TOKEN.findall(new)
    head = 0
    while head < min(len(old_tokens), len(new_tokens)) and _same_token(old_tokens[head], new_tokens[head]):
        head += 1
    tail = 0
    while (
        tail < min(len(old_tokens), len(new_tokens)) - head
        and _same_token(old_tokens[-1 - tail], new_tokens[-1 - tail])
    ):
        tail += 1
    start = sum(len(t) for t in old_tokens[:head])
    end = len(old) - sum(len(t) for t in old_tokens[len(old_tokens) - tail :])
    insert = "".join(new_tokens[head : len(new_tokens) - tail])
    removed = old[start:end]
    if removed and insert:
        # "receipt." → "receipt, without setoff.": only ", without setoff" is new.
        lead = 0
        while lead < min(len(removed), len(insert)) and _same_token(removed[lead], insert[lead]):
            lead += 1
        trail = 0
        while (
            trail < min(len(removed), len(insert)) - lead
            and _same_token(removed[-1 - trail], insert[-1 - trail])
        ):
            trail += 1
        kept_before, kept_after = old[: start + lead], old[end - trail :]
        removed_core = removed[lead : len(removed) - trail]
        insert_core = insert[lead : len(insert) - trail]
        if not removed_core and _clean_boundary(kept_before, insert_core, kept_after):
            return start + lead, start + lead, insert_core
        if not insert_core and _clean_boundary(kept_before, removed_core, kept_after):
            return start + lead, end - trail, ""
    return start, end, insert


@dataclass
class _Insertion:
    """A located ``insert`` change: its anchor and each new paragraph's format source."""

    index: int
    change: Change
    anchor: _Paragraph
    likes: List[_Paragraph]


#: Siblings that end a run of paragraphs: an inserted paragraph mark has to be
#: followed by a paragraph for Reject All to merge it away.
_BLOCK_TAGS = frozenset(_W + name for name in ("p", "tbl", "sdt", "sectPr", "customXml", "altChunk"))


_WORD = re.compile(r"\w+")


def _likely_paragraph(find: str, paragraphs: Sequence[_Paragraph]) -> Optional[_Paragraph]:
    """The paragraph a ``find`` that matched nowhere most likely meant.

    In order: a match ignoring capitalization; the first line of a ``find``
    that spans paragraphs, matched the same way; the paragraph holding most of
    its words (at least half, the first such on a tie); the first paragraph
    with any wording.
    """
    worded = [p for p in paragraphs if p.text.strip()]
    if not worded:
        return paragraphs[0] if paragraphs else None
    lines = [line for line in find.split("\n") if line.strip()]
    for wording in [find] + lines[:1]:
        pattern = _pattern(wording)
        if pattern is None:
            continue
        loose = re.compile(pattern.pattern, re.IGNORECASE)
        hit = next((p for p in worded if loose.search(p.folded)), None)
        if hit is not None:
            return hit
    words = {w for w in _WORD.findall(find.translate(_FOLD).lower())}
    words = {w for w in words if len(w) >= 3} or words
    if words:
        scores = [len(words & set(_WORD.findall(p.folded.lower()))) for p in worded]
        best = max(scores)
        if best and best * 2 >= len(words):
            return worded[scores.index(best)]
    return worded[0]


def _match(
    find: str, occurrence: Optional[int], paragraphs: Sequence[_Paragraph], index: int, misses: List[_Miss]
) -> Optional[Tuple[_Paragraph, "re.Match[str]"]]:
    """The one paragraph and span ``find`` names, or ``None`` with the miss recorded."""
    label = f"Change {index + 1}"
    pattern = _pattern(find)
    matches = [(p, m) for p in paragraphs for m in pattern.finditer(p.folded)]
    quoted = f'"{_short(find)}"'
    if not matches:
        likely = _likely_paragraph(find, paragraphs)
        if "\n" in find.strip():
            misses.append(_Miss(
                index,
                f"{label}: {quoted} spans paragraphs. A change must stay inside one paragraph.",
                "The wording to change spans more than one paragraph, and a change must stay inside one.",
                likely,
            ))
            return None
        loose = re.compile(pattern.pattern, re.IGNORECASE)
        near = next((m.group(0) for p in paragraphs for m in [loose.search(p.folded)] if m), None)
        hint = f' It does appear with different capitalization: "{_short(near)}".' if near else (
            " Copy the wording exactly as it appears in a single paragraph."
        )
        reason = (
            "The wording to change appears here only with different capitalization."
            if near
            else "The wording to change was not found in the document."
        )
        misses.append(_Miss(index, f"{label}: {quoted} was not found.{hint}", reason, likely))
        return None
    if occurrence is None and len(matches) > 1:
        misses.append(_Miss(
            index,
            f"{label}: {quoted} appears {len(matches)} times. Pass `occurrence` "
            f"(1–{len(matches)}) or a longer `find` that appears once.",
            f"The wording to change appears {len(matches)} times, so which one was meant is unclear; "
            "this comment is on the first.",
            matches[0][0],
        ))
        return None
    number = occurrence or 1
    if not 1 <= number <= len(matches):
        misses.append(_Miss(
            index,
            f"{label}: `occurrence` is {number}, but {quoted} appears {len(matches)} time(s).",
            f"The change asked for occurrence {number} of the wording, but it appears "
            f"{len(matches)} time(s); this comment is on the {'last' if number > len(matches) else 'first'}.",
            matches[-1 if number > len(matches) else 0][0],
        ))
        return None
    return matches[number - 1]


def _ends_paragraph_run(anchor: ElementTree.Element, parents: Dict[ElementTree.Element, ElementTree.Element]) -> Optional[str]:
    """What follows ``anchor`` when it is not another paragraph, else ``None``."""
    parent = parents[anchor]
    siblings = list(parent)
    following = next((s for s in siblings[siblings.index(anchor) + 1 :] if s.tag in _BLOCK_TAGS), None)
    if following is not None and following.tag == _W + "p":
        return None
    if following is not None and following.tag == _W + "tbl":
        return "comes right before a table"
    if following is not None and following.tag == _W + "sdt":
        return "comes right before a content control"
    if parent.tag == _W + "tc":
        return "is the last paragraph of its table cell"
    if parent.tag == _W + "body":
        return "is the last paragraph of the document"
    return "is the last paragraph of its container"


def _mark_is_revised(paragraph: ElementTree.Element) -> bool:
    ppr = paragraph.find(_W + "pPr")
    rpr = ppr.find(_W + "rPr") if ppr is not None else None
    marks = (_W + "ins", _W + "del", _W + "moveFrom", _W + "moveTo", _W + "rPrChange")
    return ppr is not None and (
        ppr.find(_W + "sectPr") is not None
        or ppr.find(_W + "pPrChange") is not None
        or (rpr is not None and any(c.tag in marks for c in rpr))
    )


def _locate_insertion(
    index: int,
    change: Change,
    paragraphs: Sequence[_Paragraph],
    misses: List[_Miss],
) -> Optional[_Insertion]:
    label = f"Change {index + 1}"
    before = len(misses)

    def caller(problem: str) -> None:
        misses.append(_Miss(index, problem, caller=True))

    if change.insert not in ("after", "before"):
        caller(f'{label}: `insert` must be "after" or "before".')
    if change.replace is not None:
        caller(f"{label}: give `replace` or `insert`, not both.")
    if not change.paragraphs:
        caller(f"{label}: `insert` needs `paragraphs`, the wording of each new paragraph.")
    for number, new in enumerate(change.paragraphs, 1):
        if not isinstance(new.text, str) or not new.text.strip():
            caller(f"{label}: new paragraph {number} has no `text`.")
        if new.lead is not None and not new.lead.strip():
            caller(f"{label}: new paragraph {number} has an empty `lead`.")
    if len(misses) > before:
        return None

    found = _match(change.find, change.occurrence, paragraphs, index, misses)
    target = found[0] if found is not None else _likely_paragraph(change.find, paragraphs)
    likes: List[_Paragraph] = []
    for number, new in enumerate(change.paragraphs, 1):
        if new.like is None:
            if found is not None:
                likes.append(found[0])
            continue
        pattern = _pattern(new.like) if isinstance(new.like, str) else None
        hosts = [p for p in paragraphs if pattern is not None and pattern.search(p.folded)]
        quoted = f'"{_short(new.like)}"'
        if not hosts:
            misses.append(_Miss(
                index,
                f"{label}: `like` {quoted} for new paragraph {number} was not found. Copy wording "
                "from the paragraph whose formatting it should have.",
                f"The paragraph whose formatting new paragraph {number} should copy was not found.",
                target,
            ))
        elif len(hosts) > 1:
            misses.append(_Miss(
                index,
                f"{label}: `like` {quoted} for new paragraph {number} appears in {len(hosts)} paragraphs. "
                "Use wording from only one.",
                f"The wording naming the paragraph whose formatting new paragraph {number} should copy "
                f"appears in {len(hosts)} paragraphs.",
                target,
            ))
        else:
            likes.append(hosts[0])
    if found is None or len(misses) > before:
        return None
    return _Insertion(index, change, found[0], likes)


def _check_insertion_groups(
    insertions: Sequence[_Insertion],
    parents: Dict[ElementTree.Element, ElementTree.Element],
    misses: List[_Miss],
) -> None:
    """Where an "after" group has no following paragraph to merge into, its last
    new paragraph inherits the anchor's mark, so it must keep the anchor's format.
    At most one miss per group: its last change."""
    for (_, position), group in _insertion_groups(insertions).items():
        if position != "after":
            continue
        anchor = group[0].anchor.element
        reason = _ends_paragraph_run(anchor, parents)
        if reason is None:
            continue
        last = group[-1]
        quoted = f'"{_short(last.anchor.text)}"'
        if _mark_is_revised(anchor):
            misses.append(_Miss(
                last.index,
                f"Change {last.index + 1}: {quoted} {reason} and its paragraph mark is already a "
                "tracked change or a section break, so nothing can be inserted after it. Insert before "
                "a nearby paragraph instead.",
                f"This paragraph {reason} and its paragraph mark is already a tracked change or a "
                "section break, so nothing can be inserted after it.",
                last.anchor,
            ))
        elif last.likes[-1].element is not anchor:
            misses.append(_Miss(
                last.index,
                f"Change {last.index + 1}: {quoted} {reason}, so the last new paragraph after it must "
                "keep that paragraph's own formatting. Drop `like` on the last new paragraph.",
                f"This paragraph {reason}, so the last new paragraph after it must keep this "
                "paragraph's formatting, and the change asked for another paragraph's.",
                last.anchor,
            ))


def _insertion_groups(insertions: Sequence[_Insertion]) -> Dict[Tuple[int, str], List[_Insertion]]:
    """Insertions keyed by (anchor, position), in request order within each."""
    groups: Dict[Tuple[int, str], List[_Insertion]] = {}
    for item in sorted(insertions, key=lambda x: x.index):
        groups.setdefault((id(item.anchor.element), item.change.insert or ""), []).append(item)
    return groups


@dataclass
class _Deletion:
    """A located ``delete`` change: the paragraphs it deletes and whose marks go."""

    index: int
    change: Change
    paragraphs: List[_Paragraph]
    #: Paragraphs whose mark becomes a deletion, in document order. Usually all
    #: of ``paragraphs``; where the last one's mark has to stay, its mark is
    #: kept and ``merge_into``'s is taken instead.
    marks: List[_Paragraph]
    merge_into: Optional[_Paragraph]
    #: Every paragraph of its container is deleted, so one empty one stays.
    empties: Optional[str]
    #: The paragraph that holds the change's comment: the next one, or where
    #: the last mark stays, the last deleted one, whose mark survives.
    keeper: _Paragraph


#: Paragraph children a deleted paragraph may hold besides its runs: markers
#: with no wording, which stay where they are.
_DELETABLE_MARKERS = frozenset(
    _W + name
    for name in ("bookmarkStart", "bookmarkEnd", "proofErr", "commentRangeStart", "commentRangeEnd", "permStart", "permEnd")
)


def _undeletable(paragraph: ElementTree.Element) -> Optional[str]:
    """Why ``paragraph`` cannot be deleted whole, if it cannot."""
    ppr = paragraph.find(_W + "pPr")
    if ppr is not None and ppr.find(_W + "sectPr") is not None:
        return "ends a document section (it holds a section break)"
    if _mark_is_revised(paragraph):
        return "has a paragraph mark or formatting that is already a tracked change"
    for seg in _segments(paragraph):
        tag = seg.element.tag
        if seg.locked:
            return f"holds {seg.locked}"
        if tag != _W + "r" and tag not in _DELETABLE_MARKERS:
            return "holds content this tool cannot delete, such as an equation"
    return None


def _block_siblings(element: ElementTree.Element, parents: Dict[ElementTree.Element, ElementTree.Element]) -> List[ElementTree.Element]:
    return [s for s in parents[element] if s.tag in _BLOCK_TAGS and s.tag != _W + "sectPr"]


def _canonical_ppr(paragraph: ElementTree.Element, first_id: Optional[int] = None) -> str:
    """``paragraph``'s properties as comparable text: whitespace and an empty
    mark ``w:rPr`` ignored, and with ``first_id`` this call's mark revisions too."""
    source = paragraph.find(_W + "pPr")
    if source is None:
        return ""
    ppr = copy.deepcopy(source)
    rpr = ppr.find(_W + "rPr")
    if rpr is not None:
        if first_id is not None:
            for mark in [c for c in rpr if c.tag in (_W + "ins", _W + "del") and _is_ours(c, first_id)]:
                rpr.remove(mark)
        if len(rpr) == 0 and not rpr.attrib:
            ppr.remove(rpr)
    if len(ppr) == 0 and not ppr.attrib:
        return ""
    for node in ppr.iter():
        if node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None
    ppr.tail = None
    return ElementTree.tostring(ppr, encoding="unicode")


def _locate_deletion(
    index: int,
    change: Change,
    paragraphs: Sequence[_Paragraph],
    parents: Dict[ElementTree.Element, ElementTree.Element],
    misses: List[_Miss],
) -> Optional[_Deletion]:
    label = f"Change {index + 1}"
    before = len(misses)

    def caller(problem: str) -> None:
        misses.append(_Miss(index, problem, caller=True))

    if change.delete != "paragraph":
        caller(f'{label}: `delete` must be "paragraph".')
    if change.replace is not None:
        caller(f'{label}: give `replace` or `delete`, not both. To strike only some wording, use `replace: ""`.')
    if change.insert is not None or change.paragraphs:
        caller(f"{label}: give `insert` or `delete`, not both.")
    if change.through is not None and (not isinstance(change.through, str) or _pattern(change.through) is None):
        caller(f"{label}: `through` is empty.")
    if len(misses) > before:
        return None
    found = _match(change.find, change.occurrence, paragraphs, index, misses)
    if found is None:
        return None
    first = found[0]

    def miss(problem: str, reason: str) -> None:
        misses.append(_Miss(index, f"{label}: {problem}", reason, first))

    known = {id(p.element): p for p in paragraphs}
    siblings = _block_siblings(first.element, parents)
    start = siblings.index(first.element)
    end = start
    if change.through is not None:
        pattern = _pattern(change.through)
        quoted = f'"{_short(change.through)}"'
        crossed: Optional[str] = None
        target: Optional[int] = None
        for at in range(start + 1, len(siblings)):
            sibling = siblings[at]
            if sibling.tag != _W + "p":
                crossed = crossed or ("a table" if sibling.tag == _W + "tbl" else "a content control")
                continue
            candidate = known.get(id(sibling))
            if candidate is not None and pattern.search(candidate.folded):
                target = at
                break
        if target is None:
            elsewhere = any(pattern.search(p.folded) for p in paragraphs)
            where = (
                " It appears only earlier, or in another table cell or text box." if elsewhere
                else " Copy wording from the last paragraph to delete."
            )
            miss(
                f'`through` {quoted} was not found in a paragraph after "{_short(first.text)}" '
                f"in the same part of the document.{where}",
                "The last paragraph to delete, named by `through`, was not found after this one.",
            )
            return None
        if crossed:
            miss(
                f"{crossed} lies between \"{_short(first.text)}\" and `through` {quoted}. Delete the "
                "paragraphs on each side of it in separate changes.",
                f"{crossed[0].upper() + crossed[1:]} lies between this paragraph and the last one to delete.",
            )
            return None
        end = target
    run: List[_Paragraph] = []
    for sibling in siblings[start : end + 1]:
        paragraph = known.get(id(sibling))
        reason = _undeletable(sibling) if paragraph is not None else "is not a paragraph this tool can read"
        if reason:
            text = paragraph.text if paragraph is not None else ""
            miss(
                f'the paragraph "{_short(text)}" {reason}, which this tool cannot delete.',
                f"A paragraph to delete {reason}, which this tool cannot delete.",
            )
            return None
        run.append(paragraph)

    last = run[-1]
    reason = _ends_paragraph_run(last.element, parents)
    if reason is None:
        return _Deletion(index, change, run, list(run), None, None, known[id(siblings[end + 1])])
    # No paragraph follows to take over, so the last mark stays.
    previous = siblings[start - 1] if start > 0 else None
    following = siblings[end + 1] if end + 1 < len(siblings) else None
    quoted_last = f'"{_short(last.text)}"'
    if previous is None and following is None:
        container = {_W + "tc": "table cell", _W + "body": "document", _W + "sdtContent": "content control"}.get(
            parents[last.element].tag, "text box"
        )
        return _Deletion(index, change, run, run[:-1], None, container, last)
    keep = (
        f"{quoted_last} {reason}, so its paragraph mark has to stay, and the deletion can only "
        "end there by taking the mark of the paragraph before it instead."
    )
    keep_reason = (
        f"The last paragraph to delete {reason}, so its paragraph mark has to stay, and the paragraph "
        "before the deletion cannot take its place"
    )
    merge = known.get(id(previous)) if previous is not None and previous.tag == _W + "p" else None
    if merge is None:
        what = "nothing" if previous is None else ("a table" if previous.tag == _W + "tbl" else "a content control")
        miss(
            f"{keep} Here {what} comes before it. Strike the wording with `replace: \"\"` instead.",
            f"{keep_reason}: {what} comes before it.",
        )
        return None
    if _mark_is_revised(merge.element):
        miss(
            f"{keep} That paragraph's mark is already a tracked change or a section break. Strike the "
            "wording with `replace: \"\"` instead.",
            f"{keep_reason}: its mark is already a tracked change or a section break.",
        )
        return None
    if _canonical_ppr(merge.element) != _canonical_ppr(last.element):
        miss(
            f'{keep} That paragraph, "{_short(merge.text)}", is formatted differently, so it would take '
            f"{quoted_last}'s formatting. Strike the wording with `replace: \"\"` instead, or start the "
            "deletion after a paragraph formatted like the last one deleted.",
            f"{keep_reason}: it is formatted differently and would take the deleted paragraph's formatting.",
        )
        return None
    return _Deletion(index, change, run, [merge] + run[:-1], merge, None, last)


@dataclass
class _Conflict:
    """Two changes that cannot both be made. ``index`` is the later in request
    order, the one a partial call leaves out; ``paragraph`` is where its ACTION
    NEEDED comment goes."""

    index: int
    other: int
    problem: str
    reason: str
    paragraph: _Paragraph


def _deletion_conflicts(
    deletions: Sequence[_Deletion],
    located: Sequence[_Located],
    insertions: Sequence[_Insertion],
) -> List[_Conflict]:
    """Every pair of changes a deletion cannot be made alongside, later change first in each."""
    conflicts: List[_Conflict] = []
    owner: Dict[int, _Deletion] = {}
    for item in sorted(deletions, key=lambda x: x.index):
        for paragraph in item.paragraphs:
            other = owner.setdefault(id(paragraph.element), item)
            if other is not item:
                conflicts.append(_Conflict(
                    item.index, other.index,
                    f"Change {item.index + 1} deletes a paragraph change {other.index + 1} also deletes.",
                    f"It deletes a paragraph another change (change {other.index + 1}) also deletes.",
                    item.paragraphs[0],
                ))
                break
    # Paragraphs whose mark a deletion deletes but whose wording it keeps.
    merged = {id(d.merge_into.element): d for d in deletions if d.merge_into is not None}
    for item in located:
        deletion = owner.get(id(item.paragraph.element))
        if deletion is not None:
            if item.index > deletion.index:
                problem = (
                    f"Change {item.index + 1} edits wording in a paragraph change {deletion.index + 1} deletes. "
                    "Drop one of them."
                )
            else:
                problem = (
                    f"Change {deletion.index + 1} deletes a paragraph change {item.index + 1} edits. "
                    "Drop one of them."
                )
            conflicts.append(_Conflict(
                max(item.index, deletion.index), min(item.index, deletion.index), problem,
                "It changes a paragraph another change deletes.", item.paragraph,
            ))
            continue
        deletion = merged.get(id(item.paragraph.element))
        if deletion is not None and (item.change.comment or "").strip():
            # A comment in a paragraph whose mark is deleted keeps it alive in
            # LibreOffice as an empty paragraph once the deletion is accepted.
            conflicts.append(_Conflict(
                max(item.index, deletion.index), min(item.index, deletion.index),
                f"Change {item.index + 1}'s comment would sit in \"{_short(item.paragraph.text)}\", whose "
                f"paragraph mark change {deletion.index + 1} deletes. Drop the comment or one of the changes.",
                "Its comment would sit in a paragraph whose mark another change deletes.",
                item.paragraph,
            ))
    for item in insertions:
        deletion = owner.get(id(item.anchor.element))
        if deletion is not None:
            if item.index > deletion.index:
                problem = (
                    f"Change {item.index + 1} inserts next to a paragraph change {deletion.index + 1} deletes. "
                    "Anchor it on a paragraph that stays."
                )
            else:
                problem = (
                    f"Change {deletion.index + 1} deletes the paragraph change {item.index + 1} inserts next to. "
                    "Anchor the insertion on a paragraph that stays."
                )
            conflicts.append(_Conflict(
                max(item.index, deletion.index), min(item.index, deletion.index), problem,
                "It inserts next to a paragraph another change deletes.", item.anchor,
            ))
            continue
        deletion = merged.get(id(item.anchor.element))
        if deletion is not None and (item.change.insert == "after" or (item.change.comment or "").strip()):
            conflicts.append(_Conflict(
                max(item.index, deletion.index), min(item.index, deletion.index),
                f"Change {item.index + 1} inserts next to \"{_short(item.anchor.text)}\", whose paragraph "
                f"mark change {deletion.index + 1} deletes. Insert before the next paragraph that stays.",
                "It inserts next to a paragraph whose mark another change deletes.",
                item.anchor,
            ))
    for item in deletions:
        if item.merge_into is None:
            continue
        other = owner.get(id(item.merge_into.element))
        if other is not None and other is not item:
            conflicts.append(_Conflict(
                max(item.index, other.index), min(item.index, other.index),
                f"Change {item.index + 1} and change {other.index + 1} delete neighbouring paragraphs that end "
                "where no paragraph follows. Delete them in one change with `through`.",
                "It deletes paragraphs next to another change's deletion where no paragraph follows.",
                item.paragraphs[0],
            ))
    return sorted(conflicts, key=lambda c: (c.index, c.other))


def _surviving(paragraph: _Paragraph, deletions: Sequence[_Deletion]) -> _Paragraph:
    """``paragraph``, or where its mark is deleted the paragraph that takes over:
    a comment anchored in a paragraph whose mark is deleted survives Accept All
    in LibreOffice and keeps an empty paragraph alive."""
    takes_over = {id(p.element): d.keeper for d in deletions for p in d.marks}
    for _ in range(len(takes_over) + 1):
        nxt = takes_over.get(id(paragraph.element))
        if nxt is None:
            return paragraph
        paragraph = nxt
    return paragraph  # pragma: no cover - keepers never form a cycle


def _touches(a: _Located, b: _Located) -> bool:
    """Whether two located edits in one paragraph overlap or meet where one is empty."""
    before, after = sorted((a, b), key=lambda x: (x.edit_start, x.edit_end))
    return after.edit_start < before.edit_end or (
        after.edit_start == before.edit_end and (
            before.edit_start == before.edit_end or after.edit_start == after.edit_end
        )
    )


def _locate(
    changes: Sequence[Change],
    paragraphs: Sequence[_Paragraph],
    parents: Optional[Dict[ElementTree.Element, ElementTree.Element]] = None,
    partial: bool = False,
) -> Tuple[List[_Located], List[_Insertion], List[_Deletion], List[_Miss]]:
    """Every change located, or :class:`RedlineError` with one line per problem.

    With ``partial``, a change the document defeats comes back as a
    :class:`_Miss` instead, and only the caller's own problems raise.
    """
    misses: List[_Miss] = []
    located: List[_Located] = []
    insertions: List[_Insertion] = []
    deletions: List[_Deletion] = []
    for index, change in enumerate(changes):
        label = f"Change {index + 1}"
        if not isinstance(change.find, str) or not change.find.strip():
            misses.append(_Miss(index, f"{label}: `find` is empty.", caller=True))
            continue
        if change.delete is not None or change.through is not None:
            if parents is None:  # pragma: no cover - redline() always passes them
                raise ValueError("deletions need the paragraph parent map")
            if change.delete is None:
                misses.append(_Miss(index, f'{label}: `through` needs `delete: "paragraph"`.', caller=True))
                continue
            deletion = _locate_deletion(index, change, paragraphs, parents, misses)
            if deletion is not None:
                deletions.append(deletion)
            continue
        if change.insert is not None or change.paragraphs:
            if parents is None:  # pragma: no cover - redline() always passes them
                raise ValueError("insertions need the paragraph parent map")
            item = _locate_insertion(index, change, paragraphs, misses)
            if item is not None:
                insertions.append(item)
            continue
        if change.replace is None and not (change.comment or "").strip():
            misses.append(_Miss(
                index, f"{label}: give `replace` (new wording, \"\" to delete) or `comment`.", caller=True
            ))
            continue
        found = _match(change.find, change.occurrence, paragraphs, index, misses)
        if found is None:
            continue
        paragraph, match = found
        quoted = f'"{_short(change.find)}"'
        start, end = match.span()
        if change.replace is None:
            edit_start, edit_end, insert = start, end, ""
        else:
            old = paragraph.text[start:end]
            if old.translate(_FOLD).split() == change.replace.translate(_FOLD).split():
                if (change.comment or "").strip():
                    edit_start, edit_end, insert = start, end, ""
                    change = Change(change.find, None, change.comment, change.occurrence)
                else:
                    misses.append(_Miss(index, f"{label}: `replace` is the same wording as `find`.", caller=True))
                    continue
            else:
                rel_start, rel_end, insert = _trim(old, change.replace)
                edit_start, edit_end = start + rel_start, start + rel_end
                if not insert and edit_end > edit_start:
                    edit_start, edit_end = _absorb_space(paragraph.text, edit_start, edit_end)
        segments = _segments(paragraph.element)
        if change.replace is None:
            reason = _locked_at(segments, edit_start) or _locked_at(segments, edit_end)
        else:
            reason = _locked_within(segments, edit_start, edit_end) or _locked_at(segments, edit_start) or (
                _locked_at(segments, edit_end)
            )
        if reason:
            misses.append(_Miss(
                index,
                f"{label}: {quoted} runs into {reason}, which this tool cannot change.",
                f"The wording to change runs into {reason}, which this tool cannot change.",
                paragraph,
            ))
            continue
        located.append(_Located(index, change, paragraph, start, end, edit_start, edit_end, insert))

    if partial:
        callers = [m.problem for m in misses if m.caller]
        if callers:
            raise RedlineError(callers)
        dropped: set = set()
        for conflict in _deletion_conflicts(deletions, located, insertions):
            if conflict.index in dropped or conflict.other in dropped:
                continue
            dropped.add(conflict.index)
            misses.append(_Miss(conflict.index, conflict.problem, conflict.reason, conflict.paragraph))
        located = [item for item in located if item.index not in dropped]
        insertions = [item for item in insertions if item.index not in dropped]
        deletions = [item for item in deletions if item.index not in dropped]
        located = _drop_overlaps(located, misses)
        while insertions and parents is not None:
            found: List[_Miss] = []
            _check_insertion_groups(insertions, parents, found)
            if not found:
                break
            misses.extend(found)
            gone = {m.index for m in found}
            insertions = [item for item in insertions if item.index not in gone]
        return located, insertions, deletions, misses

    by_paragraph: Dict[int, List[_Located]] = {}
    for item in located:
        by_paragraph.setdefault(id(item.paragraph.element), []).append(item)
    for items in by_paragraph.values():
        items.sort(key=lambda x: (x.edit_start, x.edit_end))
        for before, after in zip(items, items[1:]):
            if _touches(before, after):
                misses.append(_Miss(after.index, f"Change {after.index + 1} overlaps change {before.index + 1}."))
    if insertions and parents is not None:
        _check_insertion_groups(insertions, parents, misses)
    for conflict in _deletion_conflicts(deletions, located, insertions):
        misses.append(_Miss(conflict.index, conflict.problem))
    if misses:
        raise RedlineError([m.problem for m in misses])
    return located, insertions, deletions, []


def _drop_overlaps(located: Sequence[_Located], misses: List[_Miss]) -> List[_Located]:
    """``located`` less every edit that touches one kept before it in request
    order, each recorded as a miss: the later change is the one left out."""
    kept: List[_Located] = []
    for item in sorted(located, key=lambda x: x.index):
        clash = next(
            (k for k in kept if k.paragraph.element is item.paragraph.element and _touches(k, item)), None
        )
        if clash is None:
            kept.append(item)
            continue
        misses.append(_Miss(
            item.index,
            f"Change {item.index + 1} overlaps change {clash.index + 1}.",
            f"It overlaps another change to the same wording (change {clash.index + 1}), which was made instead.",
            item.paragraph,
        ))
    return kept


def _absorb_space(text: str, start: int, end: int) -> Tuple[int, int]:
    """Take one neighbouring space into a deletion so the sentence keeps single spacing."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if after == " " and (before == " " or not before):
        return start, end + 1
    if before == " " and (not after or after in ".,;:!?)"):
        return start - 1, end
    return start, end


# ── Editing a paragraph ──────────────────────────────────────────────────────


def _preserve(t: ElementTree.Element) -> None:
    t.set(f"{{{_XML_NS}}}space", "preserve")


def _split_run(paragraph: ElementTree.Element, run: ElementTree.Element, cut: int) -> None:
    """Split ``run`` so a run boundary falls ``cut`` characters into it."""
    rpr = run.find(_W + "rPr")
    pos = 0
    moved: List[ElementTree.Element] = []
    for child in [c for c in run if c is not rpr]:
        width = len(child.text or "") if child.tag == _W + "t" else len(_ATOM_TEXT.get(child.tag, ""))
        if pos >= cut and width:
            moved.append(child)
        elif pos < cut < pos + width:
            tail = ElementTree.Element(child.tag, dict(child.attrib))
            tail.text = child.text[cut - pos :]
            child.text = child.text[: cut - pos]
            _preserve(child)
            _preserve(tail)
            moved.append(tail)
        elif moved:
            moved.append(child)
        pos += width
    second = ElementTree.Element(run.tag, dict(run.attrib))
    if rpr is not None:
        second.append(copy.deepcopy(rpr))
    for child in moved:
        if child in list(run):
            run.remove(child)
        second.append(child)
    second.tail, run.tail = run.tail, None
    paragraph.insert(list(paragraph).index(run) + 1, second)


def _boundary(paragraph: ElementTree.Element, offset: int) -> None:
    for seg in _segments(paragraph):
        if seg.is_run and seg.start < offset < seg.end:
            _split_run(paragraph, seg.element, offset - seg.start)
            return


def _template_rpr(run: Optional[ElementTree.Element]) -> Optional[ElementTree.Element]:
    rpr = run.find(_W + "rPr") if run is not None else None
    if rpr is None:
        return None
    rpr = copy.deepcopy(rpr)
    for change in rpr.findall(_W + "rPrChange"):
        rpr.remove(change)
    return rpr


def _new_run(text: str, rpr: Optional[ElementTree.Element]) -> ElementTree.Element:
    run = ElementTree.Element(_W + "r")
    if rpr is not None:
        run.append(rpr)
    for i, line in enumerate(text.split("\n")):
        if i:
            ElementTree.SubElement(run, _W + "br")
        for j, piece in enumerate(line.split("\t")):
            if j:
                ElementTree.SubElement(run, _W + "tab")
            if piece:
                t = ElementTree.SubElement(run, _W + "t")
                t.text = piece
                _preserve(t)
    return run


@dataclass
class _Ids:
    next_id: int
    author: str
    date: str

    def take(self) -> str:
        value = str(self.next_id)
        self.next_id += 1
        return value

    def revision(self, tag: str) -> ElementTree.Element:
        return ElementTree.Element(
            _W + tag, {_W + "id": self.take(), _W + "author": self.author, _W + "date": self.date}
        )


def _apply(item: _Located, ids: _Ids, comments: List[Tuple[str, str]]) -> str:
    """Write one located change into its paragraph. Returns its summary line."""
    paragraph = item.paragraph.element
    text = item.paragraph.text
    is_edit = item.change.replace is not None
    start, end = (item.edit_start, item.edit_end) if is_edit else (item.start, item.end)
    _boundary(paragraph, end)
    _boundary(paragraph, start)
    segments = _segments(paragraph)
    children = list(paragraph)

    inside = [s for s in segments if s.start >= start and s.end <= end and s.end > s.start]
    region: List[ElementTree.Element] = []
    template: Optional[ElementTree.Element] = None
    if is_edit and inside:
        template = inside[0].element
        group: Optional[ElementTree.Element] = None
        first = children.index(inside[0].element)
        last = children.index(inside[-1].element)
        for child in children[first : last + 1]:
            if child.tag != _W + "r":
                group = None
                continue
            if group is None:
                group = ids.revision("del")
                paragraph.insert(list(paragraph).index(child), group)
                region.append(group)
            paragraph.remove(child)
            for t in child.iter(_W + "t"):
                t.tag = _W + "delText"
            group.append(child)
    elif not is_edit:
        region = [s.element for s in inside] or []

    if is_edit and item.insert:
        if template is None:
            before = [s for s in segments if s.is_run and s.end == start and s.end > s.start]
            after = [s for s in segments if s.is_run and s.start == start and s.end > s.start]
            template = (before[-1] if before else after[0] if after else None)
            template = template.element if template is not None else None
        ins = ids.revision("ins")
        ins.append(_new_run(item.insert, _template_rpr(template)))
        if region:
            anchor_index = list(paragraph).index(region[-1]) + 1
        else:
            text_children = [s for s in segments if s.end > s.start]
            ending = [s for s in text_children if s.end <= start]
            if ending:
                anchor_index = list(paragraph).index(ending[-1].element) + 1
            elif text_children:
                anchor_index = list(paragraph).index(text_children[0].element)
            else:
                anchor_index = len(paragraph)
        paragraph.insert(anchor_index, ins)
        region.append(ins)

    comment = (item.change.comment or "").strip()
    if comment and region:
        comment_id = ids.take()
        paragraph.insert(
            list(paragraph).index(region[0]), ElementTree.Element(_W + "commentRangeStart", {_W + "id": comment_id})
        )
        end_index = list(paragraph).index(region[-1]) + 1
        paragraph.insert(end_index, ElementTree.Element(_W + "commentRangeEnd", {_W + "id": comment_id}))
        reference = ElementTree.Element(_W + "r")
        ElementTree.SubElement(reference, _W + "commentReference", {_W + "id": comment_id})
        paragraph.insert(end_index + 1, reference)
        comments.append((comment_id, comment))

    removed = text[start:end] if is_edit else ""
    parts = []
    if removed:
        parts.append(f'deleted "{_short(removed)}"')
    if item.insert:
        parts.append(f'inserted "{_short(item.insert)}"')
    if comment and is_edit:
        parts.append("with a comment on the change")
    elif comment:
        parts.append(f'commented on "{_short(text[item.start:item.end])}"')
    return f"{item.index + 1}. " + ", ".join(parts)


def _comment_on_paragraph(
    paragraph: ElementTree.Element,
    comment: str,
    ids: _Ids,
    comments: List[Tuple[str, str]],
    point: bool = False,
) -> None:
    """Anchor ``comment`` on the whole of ``paragraph``: its range opens after the
    paragraph properties and closes, with the reference run, at its end.

    With ``point``, for a paragraph whose wording this call deletes but whose
    mark survives (§3c), the range is empty and sits at the end, after the
    deleted wording: Word drops a comment whose whole range is deleted text when
    the deletion is accepted, and a point after it survives in both Word and
    LibreOffice."""
    comment_id = ids.take()
    start_at = len(paragraph) if point else (1 if paragraph.find(_W + "pPr") is not None else 0)
    paragraph.insert(start_at, ElementTree.Element(_W + "commentRangeStart", {_W + "id": comment_id}))
    paragraph.append(ElementTree.Element(_W + "commentRangeEnd", {_W + "id": comment_id}))
    reference = ElementTree.SubElement(paragraph, _W + "r")
    ElementTree.SubElement(reference, _W + "commentReference", {_W + "id": comment_id})
    comments.append((comment_id, comment))


# ── Unplaced changes as comments ─────────────────────────────────────────────

ACTION_NEEDED = "ACTION NEEDED —"


def _action_needed(change: Change, reason: str) -> str:
    """The comment that stands in for a change that could not be placed: the
    reason, then everything the change asked for, in full."""
    lines = [f"{ACTION_NEEDED} {reason}", f'Find: "{change.find.strip()}"']
    if change.paragraphs:
        where = change.insert if change.insert in ("after", "before") else "after"
        lines.append(f"Insert {where} that paragraph:")
        for new in change.paragraphs:
            lead = (new.lead or "").strip()
            lines.append(f'"{lead + " " if lead else ""}{new.text.strip()}"')
    elif change.delete is not None or change.through is not None:
        through = (change.through or "").strip()
        lines.append(
            f'Delete that paragraph and every one after it through the one containing: "{through}"'
            if through else "Delete that paragraph."
        )
    elif change.replace is not None:
        lines.append(f'Replace with: "{change.replace}"' if change.replace.strip() else "Delete it.")
    note = (change.comment or "").strip()
    if note:
        lines.append(f"Comment: {note}")
    return "\n".join(lines)


def _first_miss_per_change(misses: Sequence[_Miss]) -> List[_Miss]:
    """One miss per change, the first recorded, in request order."""
    first: Dict[int, _Miss] = {}
    for miss in misses:
        first.setdefault(miss.index, miss)
    return [first[i] for i in sorted(first)]


# ── Inserting whole paragraphs ───────────────────────────────────────────────

_MARK_REVISIONS = frozenset(_W + name for name in ("ins", "del", "moveFrom", "moveTo", "rPrChange"))


def _template_ppr(paragraph: ElementTree.Element) -> ElementTree.Element:
    """A copy of ``paragraph``'s properties for a new paragraph: style, numbering,
    indentation and spacing kept; a section break and any revision dropped."""
    source = paragraph.find(_W + "pPr")
    ppr = copy.deepcopy(source) if source is not None else ElementTree.Element(_W + "pPr")
    for child in [c for c in ppr if c.tag in (_W + "sectPr", _W + "pPrChange")]:
        ppr.remove(child)
    rpr = ppr.find(_W + "rPr")
    if rpr is not None:
        for child in [c for c in rpr if c.tag in _MARK_REVISIONS]:
            rpr.remove(child)
    numbering = ppr.find(_W + "numPr")
    if numbering is not None:
        for child in [c for c in numbering if c.tag == _W + "ins"]:
            numbering.remove(child)
    return ppr


def _mark_inserted(ppr: ElementTree.Element, mark: ElementTree.Element) -> None:
    """Record the paragraph mark ``ppr`` belongs to as a tracked revision
    (``mark`` is a ``w:ins`` or a ``w:del``)."""
    rpr = ppr.find(_W + "rPr")
    if rpr is None:
        rpr = ElementTree.Element(_W + "rPr")
        # rPr precedes a section break and the property-change record.
        later = [i for i, c in enumerate(ppr) if c.tag in (_W + "sectPr", _W + "pPrChange")]
        ppr.insert(later[0] if later else len(ppr), rpr)
    rpr.insert(0, mark)


def _run_templates(paragraph: ElementTree.Element) -> Tuple[Optional[ElementTree.Element], Optional[ElementTree.Element]]:
    """``(lead, body)`` runs to copy formatting from: the first run with a letter
    in it (a bold "Invoices." lead-in) and the run holding the most wording."""
    runs = [s for s in _segments(paragraph) if s.is_run and s.text.strip()]
    if not runs:
        return None, None
    lead = next((s for s in runs if any(ch.isalpha() for ch in s.text)), runs[0])
    body = max(runs, key=lambda s: len(s.text))
    return lead.element, body.element


def _new_paragraph(
    new: NewParagraph, like: ElementTree.Element, ppr: ElementTree.Element, ids: _Ids
) -> ElementTree.Element:
    paragraph = ElementTree.Element(_W + "p")
    if len(ppr):
        paragraph.append(ppr)
    lead_run, body_run = _run_templates(like)
    ins = ids.revision("ins")
    text = new.text
    if new.lead:
        ins.append(_new_run(new.lead, _template_rpr(lead_run)))
        if not (new.lead[-1].isspace() or text[0].isspace()):
            text = " " + text
    ins.append(_new_run(text, _template_rpr(body_run)))
    paragraph.append(ins)
    return paragraph


def _apply_insertions(
    insertions: Sequence[_Insertion],
    parents: Dict[ElementTree.Element, ElementTree.Element],
    units: Sequence[ElementTree.Element],
    extras: Dict[int, Tuple[List[ElementTree.Element], List[ElementTree.Element]]],
    ids: _Ids,
    comments: List[Tuple[str, str]],
) -> Dict[int, str]:
    """Add every inserted paragraph next to its anchor. Returns summary lines by index."""
    summaries: Dict[int, str] = {}
    for (_, position), group in _insertion_groups(insertions).items():
        anchor = group[0].anchor.element
        adopt_mark = position == "after" and _ends_paragraph_run(anchor, parents) is not None
        # Taken before the anchor's own mark is touched.
        anchor_ppr = _template_ppr(anchor)
        built: List[ElementTree.Element] = []
        for item in group:
            made: List[ElementTree.Element] = []
            for number, (new, like) in enumerate(zip(item.change.paragraphs, item.likes), 1):
                last_of_group = item is group[-1] and number == len(item.change.paragraphs)
                if adopt_mark and last_of_group:
                    # No paragraph follows, so this one keeps the anchor's mark
                    # and the anchor's mark becomes the inserted one.
                    made.append(_new_paragraph(new, like.element, copy.deepcopy(anchor_ppr), ids))
                    continue
                ppr = _template_ppr(like.element)
                _mark_inserted(ppr, ids.revision("ins"))
                made.append(_new_paragraph(new, like.element, ppr, ids))
            comment = (item.change.comment or "").strip()
            if comment:
                # On the anchor, never inside the new paragraphs: a comment in
                # inserted text survives Reject All in LibreOffice and keeps an
                # empty paragraph alive where the insertion was.
                _comment_on_paragraph(anchor, comment, ids, comments)
            count = len(made)
            what = "a paragraph" if count == 1 else f"{count} paragraphs"
            line = f'{item.index + 1}. inserted {what} {position} "{_short(item.anchor.text)}"'
            summaries[item.index] = line + (", with a comment on that paragraph" if comment else "")
            built.extend(made)
        if adopt_mark:
            own = anchor.find(_W + "pPr")
            if own is None:
                own = ElementTree.Element(_W + "pPr")
                anchor.insert(0, own)
            _mark_inserted(own, ids.revision("ins"))

        unit = group[0].anchor.unit
        if units[unit] is anchor:
            # The anchor is itself re-serialized from its source span, so the
            # new paragraphs are written beside it rather than into the tree.
            before, after = extras.setdefault(unit, ([], []))
            (after if position == "after" else before).extend(built)
        else:
            parent = parents[anchor]
            at = list(parent).index(anchor) + (1 if position == "after" else 0)
            for offset, element in enumerate(built):
                parent.insert(at + offset, element)
    return summaries


# ── Deleting whole paragraphs ────────────────────────────────────────────────


def _strike_runs(paragraph: ElementTree.Element, ids: _Ids) -> None:
    """Move every run of ``paragraph`` into ``w:del``, one per unbroken stretch."""
    group: Optional[ElementTree.Element] = None
    for child in list(paragraph):
        if child.tag != _W + "r":
            group = None
            continue
        if group is None:
            group = ids.revision("del")
            paragraph.insert(list(paragraph).index(child), group)
        paragraph.remove(child)
        for t in child.iter(_W + "t"):
            t.tag = _W + "delText"
        group.append(child)


def _struck(deletions: Sequence[_Deletion]) -> set:
    """Ids of every paragraph element whose wording a deletion strikes."""
    return {id(p.element) for item in deletions for p in item.paragraphs}


def _apply_deletions(deletions: Sequence[_Deletion], ids: _Ids, comments: List[Tuple[str, str]]) -> Dict[int, str]:
    """Delete every located paragraph, runs and marks. Returns summary lines by index."""
    summaries: Dict[int, str] = {}
    for item in deletions:
        for paragraph in item.paragraphs:
            _strike_runs(paragraph.element, ids)
        for paragraph in item.marks:
            ppr = paragraph.element.find(_W + "pPr")
            if ppr is None:
                ppr = ElementTree.Element(_W + "pPr")
                paragraph.element.insert(0, ppr)
            _mark_inserted(ppr, ids.revision("del"))
    # Comments after every deletion is struck, so none wraps wording a later
    # deletion strikes. Each goes in a paragraph whose mark survives, never in
    # one whose mark is deleted: LibreOffice keeps a comment's anchor when it
    # accepts the deletion around it, and with it an empty paragraph that
    # still holds its number, which is what deleting the mark is for.
    struck = _struck(deletions)
    for item in deletions:
        comment = (item.change.comment or "").strip()
        if comment:
            keeper = item.keeper.element
            _comment_on_paragraph(keeper, comment, ids, comments, point=id(keeper) in struck)
        count = len(item.paragraphs)
        if count == 1:
            line = f'{item.index + 1}. deleted the paragraph "{_short(item.paragraphs[0].text)}"'
        else:
            line = (
                f'{item.index + 1}. deleted {count} paragraphs, "{_short(item.paragraphs[0].text, 40)}" '
                f'through "{_short(item.paragraphs[-1].text, 40)}"'
            )
        if comment:
            stays = item.keeper not in item.paragraphs and item.keeper.text
            line += f' and commented on "{_short(item.keeper.text, 40)}"' if stays else ", with a comment"
        if item.empties:
            line += f" (the {item.empties} keeps one empty paragraph, as Word requires)"
        summaries[item.index] = line
    return summaries


# ── Checking the result ──────────────────────────────────────────────────────

#: Stands for a paragraph mark in :func:`_paragraph_view`.
_MARK = "¶"


def _is_ours(revision: ElementTree.Element, first_id: int) -> bool:
    revision_id = revision.get(_W + "id") or ""
    return revision_id.lstrip("-").isdigit() and int(revision_id) >= first_id


def _rejected_wording(node: ElementTree.Element, first_id: int) -> str:
    """``node``'s wording with this call's insertions dropped and deletions kept,
    each nested paragraph followed by its mark as :func:`_paragraph_view` reads it."""
    parts: List[str] = []
    for child in node:
        if not isinstance(child.tag, str) or child.tag == _W + "pPr":
            continue
        if child.tag in (_W + "ins", _W + "moveTo") and _is_ours(child, first_id):
            continue
        if child.tag == _W + "p":
            parts.append(_paragraph_view(child, first_id))
            continue
        if child.tag in (_W + "t", _W + "delText"):
            parts.append(child.text or "")
        elif child.tag in _ATOM_TEXT:
            parts.append(_ATOM_TEXT[child.tag])
        parts.append(_rejected_wording(child, first_id))
    return "".join(parts)


def _paragraph_view(paragraph: ElementTree.Element, first_id: int) -> str:
    """``paragraph`` as Reject All leaves it: its wording, then its mark and
    properties. A mark this call inserted is rejected, so the wording runs on
    into the next paragraph; a mark this call deleted is restored."""
    wording = _rejected_wording(paragraph, first_id)
    rpr = paragraph.find(f"{_W}pPr/{_W}rPr")
    if rpr is not None and any(c.tag == _W + "ins" and _is_ours(c, first_id) for c in rpr):
        return wording
    return wording + _MARK + _canonical_ppr(paragraph, first_id)


def _live_wording(node: ElementTree.Element) -> str:
    """Wording Accept All would keep: everything outside ``w:del``."""
    parts: List[str] = []
    for child in node:
        if not isinstance(child.tag, str) or child.tag in (_W + "del", _W + "moveFrom", _W + "pPr"):
            continue
        if child.tag == _W + "t":
            parts.append(child.text or "")
        elif child.tag in _ATOM_TEXT:
            parts.append(_ATOM_TEXT[child.tag])
        parts.append(_live_wording(child))
    return "".join(parts)


# ── Serializing a paragraph ──────────────────────────────────────────────────


def _escape_attr(value: str) -> str:
    return escape(value, {'"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"})


class _Writer:
    def __init__(self, prefixes: Dict[str, str]) -> None:
        self.prefixes = prefixes  # uri → prefix
        self.used: set = set()

    def name(self, qualified: str) -> str:
        if not qualified.startswith("{"):
            return qualified
        uri, local = qualified[1:].split("}", 1)
        if uri == _XML_NS:
            return f"xml:{local}"
        prefix = self.prefixes.get(uri)
        if prefix is None:
            raise RedlineError([f"The document uses a namespace this tool cannot write back ({uri})."])
        self.used.add(prefix)
        return f"{prefix}:{local}"

    def write(self, element: ElementTree.Element, out: List[str], extra: str = "", tail: bool = True) -> None:
        if element.tag is ElementTree.Comment:
            out.append(f"<!--{element.text or ''}-->")
        elif element.tag is ElementTree.ProcessingInstruction:
            out.append(f"<?{element.text or ''}?>")
        else:
            tag = self.name(element.tag)
            attrs = "".join(f' {self.name(k)}="{_escape_attr(v)}"' for k, v in element.attrib.items())
            if len(element) == 0 and not element.text:
                out.append(f"<{tag}{extra}{attrs}/>")
            else:
                out.append(f"<{tag}{extra}{attrs}>")
                if element.text:
                    out.append(escape(element.text))
                for child in element:
                    self.write(child, out)
                out.append(f"</{tag}>")
        if tail and element.tail:
            out.append(escape(element.tail))


def _declarations(xml: str) -> List[Tuple[str, str]]:
    return [(m.group(1), m.group(2) if m.group(2) is not None else m.group(3)) for m in _XMLNS.finditer(xml)]


def _unit_spans(xml: str, w_prefix: str) -> List[Tuple[int, int]]:
    """Source spans of every outermost ``w:p``, in document order."""
    spans: List[Tuple[int, int]] = []
    depth = 0
    start = 0
    name = f"{w_prefix}:p"
    for match in _TAG.finditer(xml):
        tag = match.group(0)
        found = _NAME.match(tag)
        if not found or found.group(1) != name:
            continue
        if tag.startswith("</"):
            depth -= 1
            if depth == 0:
                spans.append((start, match.end()))
        elif tag.endswith("/>"):
            if depth == 0:
                spans.append((match.start(), match.end()))
        else:
            if depth == 0:
                start = match.start()
            depth += 1
    return spans


@dataclass
class _Namespaces:
    """How the document spells its namespaces."""

    root: Dict[str, str]  # prefix → uri, as declared on the root element
    bound: Dict[str, str]  # prefix → uri, first declaration anywhere
    prefixes: Dict[str, str]  # uri → prefix to write it with
    w: str  # the prefix of the main WordprocessingML namespace

    @classmethod
    def of(cls, xml: str) -> "_Namespaces":
        root_tag = next(m.group(0) for m in _TAG.finditer(xml) if not m.group(0).startswith(("<?", "<!")))
        root = dict(_declarations(root_tag))
        bound = dict(root)
        prefixes = {uri: prefix for prefix, uri in reversed(list(root.items()))}
        for prefix, uri in _declarations(xml):
            prefixes.setdefault(uri, prefix)
            bound.setdefault(prefix, uri)
        w = prefixes.get(W_NS)
        if w is None:
            raise RedlineError(["This is not a Word document body this tool can edit."])
        return cls(root, bound, prefixes, w)


def _serialize_units(
    xml: str,
    namespaces: _Namespaces,
    units: Sequence[ElementTree.Element],
    dirty: Sequence[int],
    extras: Optional[Dict[int, Tuple[List[ElementTree.Element], List[ElementTree.Element]]]] = None,
) -> str:
    """``xml`` with each dirty outermost paragraph replaced by its edited form,
    and any new paragraph from ``extras`` written before or after it."""
    spans = _unit_spans(xml, namespaces.w)
    if len(spans) != len(units):
        raise RedlineError(["The document's paragraph markup could not be matched up, so it was left unchanged."])

    def declared(element: ElementTree.Element, local: Sequence[Tuple[str, str]]) -> str:
        # A declaration that sat inside the paragraph is lost on parsing; put
        # it back on the paragraph, with any prefix the root does not declare.
        probe = _Writer(namespaces.prefixes)
        probe.write(element, [], tail=False)
        declare = {p for p, _ in local} | (probe.used - set(namespaces.root))
        return "".join(f' xmlns:{p}="{_escape_attr(namespaces.bound[p])}"' for p in sorted(declare))

    def written(element: ElementTree.Element, local: Sequence[Tuple[str, str]] = ()) -> str:
        out: List[str] = []
        _Writer(namespaces.prefixes).write(element, out, declared(element, local), tail=False)
        return "".join(out)

    pieces: List[Tuple[int, int, str]] = []
    for unit in sorted(set(dirty)):
        start, end = spans[unit]
        source = xml[start:end]
        local = _declarations(source)
        if _DEFAULT_XMLNS.search(source) or any(namespaces.bound.get(p) != u for p, u in local):
            raise RedlineError(["A paragraph to change uses namespace markup this tool cannot write back."])
        before, after = (extras or {}).get(unit, ([], []))
        text = "".join(written(p) for p in before) + written(units[unit], local) + "".join(
            written(p) for p in after
        )
        pieces.append((start, end, text))
    for start, end, text in reversed(pieces):
        xml = xml[:start] + text + xml[end:]
    return xml


# ── The package ──────────────────────────────────────────────────────────────


def _initials(author: str) -> str:
    return "".join(word[0] for word in author.split()[:3]).upper() or "R"


def _comments_part(package: zipfile.ZipFile) -> Tuple[str, Optional[str]]:
    """The comments part's name, and the relationship XML it already has, if any."""
    try:
        rels = package.read(_DOCUMENT_RELS).decode("utf-8")
    except KeyError:
        return "word/comments.xml", None
    for rel in re.finditer(r"<Relationship\b[^>]*>", rels):
        if _COMMENTS_REL_TYPE in rel.group(0):
            target = re.search(r"\bTarget\s*=\s*[\"']([^\"']+)[\"']", rel.group(0))
            if target:
                return "word/" + target.group(1).lstrip("/").removeprefix("word/"), rels
    return "word/comments.xml", rels


def _comment_xml(comment_id: str, author: str, date: str, text: str, w: str) -> str:
    paragraphs = "".join(
        f'<{w}:p><{w}:r><{w}:t xml:space="preserve">{escape(line)}</{w}:t></{w}:r></{w}:p>'
        for line in (text.split("\n") or [""])
    )
    return (
        f'<{w}:comment {w}:id="{comment_id}" {w}:author="{_escape_attr(author)}" '
        f'{w}:date="{date}" {w}:initials="{_escape_attr(_initials(author))}">{paragraphs}</{w}:comment>'
    )


def _max_id(package: zipfile.ZipFile) -> int:
    highest = 0
    for name in package.namelist():
        if not (name.startswith("word/") and name.endswith(".xml")):
            continue
        try:
            xml = package.read(name).decode("utf-8")
        except UnicodeDecodeError:
            continue
        for match in _W_ID.finditer(xml):
            highest = max(highest, int(match.group(1)))
    return highest


def redline(
    docx_bytes: bytes,
    changes: Sequence[Change],
    author: str,
    now: Optional[datetime] = None,
    if_unplaced: str = "fail",
) -> RedlineResult:
    """Apply ``changes`` to a .docx as tracked changes by ``author``.

    ``if_unplaced="comment"`` writes each change the document defeats as an
    ACTION NEEDED comment and applies the rest (module docstring).
    """
    if not changes:
        raise RedlineError(["No changes were given."])
    if if_unplaced not in IF_UNPLACED:
        raise RedlineError(['`if_unplaced` must be "fail" or "comment".'])
    partial = if_unplaced == "comment"
    try:
        package = zipfile.ZipFile(io.BytesIO(docx_bytes))
    except (zipfile.BadZipFile, ValueError):
        raise RedlineError(["The file is not a Word document."])

    with package:
        try:
            xml = package.read(_DOCUMENT_PART).decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            raise RedlineError(["The file has no readable Word document body."])
        parser = ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True, insert_pis=True))
        try:
            root = ElementTree.fromstring(xml, parser=parser)
        except ElementTree.ParseError as exc:
            raise RedlineError([f"The document body could not be parsed: {exc}"])
        body = root.find(_W + "body")
        if body is None:
            raise RedlineError(["The file has no Word document body."])

        namespaces = _Namespaces.of(xml)
        paragraphs, units = _paragraphs(body)
        structural = any(
            c.insert is not None or c.paragraphs or c.delete is not None or c.through is not None for c in changes
        )
        parents = {child: node for node in body.iter() for child in node} if structural else None
        located, insertions, deletions, misses = _locate(changes, paragraphs, parents, partial)
        unplaced = _first_miss_per_change(misses)
        if any(miss.paragraph is None for miss in unplaced):  # pragma: no cover - a body with no paragraph
            raise RedlineError(["The document has no paragraph to put a comment on, so nothing was saved."])
        # No comment, placed or ACTION NEEDED, may sit in a paragraph whose mark
        # this call deletes: it would keep that paragraph alive (§3c).
        for item in deletions:
            item.keeper = _surviving(item.keeper, deletions)
        for miss in unplaced:
            miss.paragraph = _surviving(miss.paragraph, deletions)

        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids = _Ids(_max_id(package) + 1, author, stamp)
        first_id = ids.next_id
        dirty = sorted(
            {item.paragraph.unit for item in located}
            | {item.anchor.unit for item in insertions}
            | {p.unit for item in deletions for p in item.paragraphs + item.marks}
            | {item.keeper.unit for item in deletions if (item.change.comment or "").strip()}
            | {miss.paragraph.unit for miss in unplaced if miss.paragraph is not None}
        )
        wording = {unit: _paragraph_view(units[unit], first_id) for unit in dirty}
        comments: List[Tuple[str, str]] = []
        summaries: Dict[int, str] = {}
        # Right to left within a paragraph, so earlier offsets stay valid.
        for item in sorted(located, key=lambda x: (x.paragraph.unit, -x.edit_start, -x.start)):
            summaries[item.index] = _apply(item, ids, comments)
        summaries.update(_apply_deletions(deletions, ids, comments))
        extras: Dict[int, Tuple[List[ElementTree.Element], List[ElementTree.Element]]] = {}
        if insertions and parents is not None:
            summaries.update(_apply_insertions(insertions, parents, units, extras, ids, comments))
        # Last, so every placed change takes the ids it would take on its own.
        commented: List[str] = []
        struck = _struck(deletions)
        for miss in unplaced:
            change = changes[miss.index]
            _comment_on_paragraph(
                miss.paragraph.element,
                _action_needed(change, miss.reason),
                ids,
                comments,
                point=id(miss.paragraph.element) in struck,
            )
            commented.append(f'{miss.index + 1}. "{_short(change.find)}": {miss.reason}')

        def rejected_view(unit: int) -> str:
            before, after = extras.get(unit, ([], []))
            return "".join(_paragraph_view(p, first_id) for p in [*before, units[unit], *after])

        # Reject All must give back every edited paragraph, marks and
        # properties included, and Accept All must keep nothing of a deleted one.
        if any(_live_wording(p.element) for item in deletions for p in item.paragraphs):  # a bug
            raise RedlineError(["The redline would have kept wording it marked as deleted, so nothing was saved."])
        if any(rejected_view(unit) != wording[unit] for unit in dirty):  # a bug, never the user's input
            raise RedlineError(["The redline would have changed wording it did not mark, so nothing was saved."])

        new_xml = _serialize_units(xml, namespaces, units, dirty, extras)
        try:
            ElementTree.fromstring(new_xml)
        except ElementTree.ParseError as exc:  # pragma: no cover - a bug, never the user's input
            raise RedlineError([f"The edited document failed to parse ({exc}), so nothing was saved."])

        replaced: Dict[str, bytes] = {_DOCUMENT_PART: new_xml.encode("utf-8")}
        added: Dict[str, bytes] = {}
        if comments:
            part_name, rels = _comments_part(package)
            if part_name in package.namelist():
                existing = package.read(part_name).decode("utf-8")
                w = next((p for p, u in _declarations(existing) if u == W_NS), namespaces.w)
                entries = "".join(_comment_xml(cid, author, stamp, text, w) for cid, text in comments)
                close = existing.rfind(f"</{w}:comments>")
                if close < 0:
                    raise RedlineError(["The document's comments part could not be extended."])
                replaced[part_name] = (existing[:close] + entries + existing[close:]).encode("utf-8")
            else:
                w = namespaces.w
                entries = "".join(_comment_xml(cid, author, stamp, text, w) for cid, text in comments)
                added[part_name] = (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                    f'<{w}:comments xmlns:{w}="{W_NS}">{entries}</{w}:comments>'
                ).encode("utf-8")
            target = part_name.removeprefix("word/")
            if rels is None:
                added[_DOCUMENT_RELS] = (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    f'<Relationship Id="rIdComments" Type="{_COMMENTS_REL_TYPE}" Target="{target}"/>'
                    "</Relationships>"
                ).encode("utf-8")
            elif _COMMENTS_REL_TYPE not in rels:
                taken = set(re.findall(r"\bId\s*=\s*[\"']([^\"']+)[\"']", rels))
                rid = next(f"rIdComments{n or ''}" for n in range(len(taken) + 2) if f"rIdComments{n or ''}" not in taken)
                close = rels.rfind("</Relationships>")
                relationship = f'<Relationship Id="{rid}" Type="{_COMMENTS_REL_TYPE}" Target="{target}"/>'
                replaced[_DOCUMENT_RELS] = (rels[:close] + relationship + rels[close:]).encode("utf-8")
            content_types = package.read(_CONTENT_TYPES).decode("utf-8")
            if f'PartName="/{part_name}"' not in content_types:
                close = content_types.rfind("</Types>")
                override = f'<Override PartName="/{part_name}" ContentType="{_COMMENTS_CONTENT_TYPE}"/>'
                replaced[_CONTENT_TYPES] = (content_types[:close] + override + content_types[close:]).encode("utf-8")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as target_zip:
            for info in package.infolist():
                data = replaced.get(info.filename)
                target_zip.writestr(info, data if data is not None else package.read(info.filename))
            for name, data in added.items():
                target_zip.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        return RedlineResult(buffer.getvalue(), [summaries[i] for i in sorted(summaries)], commented)


__all__ = ["ACTION_NEEDED", "IF_UNPLACED", "Change", "NewParagraph", "RedlineError", "RedlineResult", "redline"]
