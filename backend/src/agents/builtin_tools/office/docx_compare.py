"""Compare two Word documents and write the differences as tracked changes.

The engine behind ``compare_word_documents`` (``docs/specs/word-tracked-changes.md``
§3b). A counterparty often returns a clean copy with no tracked changes, and the
user asks what changed. :func:`compare` writes the revised document's wording
into a copy of the **original** as tracked changes by one author (default
``Counterparty``), so each change can be accepted or rejected in Word. Reject
All gives back the original; Accept All gives the revised wording.

**Alignment.** Each container (the body, then each table cell) is a sequence of
blocks: paragraphs, tables, and anything else at block level (a content
control), keyed by folded, whitespace-collapsed wording. ``difflib`` aligns the
two sequences; equal blocks are left alone. Inside each differing stretch,
blocks are paired by similarity in order (a weighted LCS): a paragraph that
differs from its counterpart by a few words is *reworded*, one with no
counterpart is *removed* or *added*. A stretch of one paragraph against one
paragraph is always a rewording, which is what a changed table cell is.

* **Reworded** paragraphs are diffed word by word (the redline engine's token
  rule, so ``receipt.`` → ``receipt, without setoff.`` marks only the new
  words), and each differing span becomes one ``w:del`` / ``w:ins`` pair in the
  original paragraph's own runs and formatting.
* **Removed** paragraphs have their runs and their paragraph mark deleted, so
  Accept All leaves no empty paragraph. Where no paragraph follows (the end of a
  cell or of the document, before a table) the mark stays, as in Word.
* **Added** paragraphs are §3a's tracked paragraph insertions, written before
  the next original paragraph, with the revised paragraph's own runs and
  formatting; its style and numbering ids are mapped to the original's through
  the paragraphs the two documents share.
* **Tables** with the same grid are compared cell by cell. A table whose rows or
  columns changed, an added or removed table, and a change this engine cannot
  mark (inside a hyperlink, field or content control) each become a comment
  saying so, and are listed as not marked.

Out of scope: formatting-only changes, headers, footers, footnotes, text boxes,
images and moves (a moved paragraph reads as removed in one place and added in
another). A document that already holds tracked changes is refused.

Built on the redline engine's guarantees: only the edited paragraphs are
re-serialized and spliced into the original's bytes, namespace-safe; every
edited paragraph's reject view is checked to equal the original, and here its
accept view is also checked to equal the revised wording. A failure of either
check is a bug and nothing is written. Stdlib only.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Set, Tuple
from xml.etree import ElementTree

from agents.builtin_tools.office.docx_redline import (
    _FOLD,
    _SPLITTABLE_RUN_CHILDREN,
    _W,
    Change,
    NewParagraph,
    RedlineError,
    _block_siblings,
    _comment_on_paragraph,
    _Deletion,
    _deletion_of,
    _Document,
    _ends_paragraph_run,
    _Extras,
    _Ids,
    _Insertion,
    _Located,
    _locked_at,
    _locked_within,
    _mark_is_revised,
    _nested_text,
    _open_document,
    _Paragraph,
    _paragraphs,
    _run_text,
    _segments,
    _short,
    _template_ppr,
    _template_rpr,
    _trim,
    _undeletable,
    _write,
)

DEFAULT_AUTHOR = "Counterparty"

#: Similarity a paragraph needs to its counterpart to be paired as a rewording
#: rather than one removed and one added (``SequenceMatcher.ratio``).
_PAIR_RATIO = 0.5
_TABLE_PAIR_RATIO = 0.3
#: A differing stretch larger than this many candidate pairs is not paired: it
#: is a rewrite, written as removals and additions.
_MAX_PAIRINGS = 2_500
#: How much of a revised table or paragraph a "not marked" comment quotes.
_QUOTE_CHARS = 500

#: Any of these in either document means it already holds tracked changes.
_REVISION_TAGS = frozenset(
    _W + name
    for name in (
        "ins", "del", "moveFrom", "moveTo", "moveFromRangeStart", "moveToRangeStart",
        "pPrChange", "rPrChange", "sectPrChange", "tblPrChange", "tblGridChange",
        "trPrChange", "tcPrChange", "tblPrExChange", "numberingChange",
        "cellIns", "cellDel", "cellMerge",
    )
)
_BLOCK_OTHER = frozenset(_W + name for name in ("sdt", "customXml", "altChunk"))


def _same_rpr(a: Optional[ElementTree.Element], b: Optional[ElementTree.Element]) -> bool:
    if a is None or b is None:
        return a is b
    return ElementTree.tostring(a) == ElementTree.tostring(b)


class NoDifferences(RedlineError):
    """The two documents have the same wording."""


@dataclass
class CompareResult:
    docx: bytes
    #: One line per marked change, in document order.
    changes: List[str] = field(default_factory=list)
    #: One line per difference left as a comment instead of tracked changes.
    unmarked: List[str] = field(default_factory=list)
    reworded: int = 0
    added: int = 0
    removed: int = 0


# ── Blocks ───────────────────────────────────────────────────────────────────


def _key(text: str) -> str:
    return " ".join(text.translate(_FOLD).split())


@dataclass
class _Block:
    kind: str  # "p", "tbl" or "other"
    element: ElementTree.Element
    key: str
    para: Optional[_Paragraph] = None


def _rows(table: ElementTree.Element) -> Optional[List[List[ElementTree.Element]]]:
    """The table's cells by row, or ``None`` when rows or cells are wrapped in
    markup (a content control) this engine does not compare inside."""
    rows: List[List[ElementTree.Element]] = []
    for child in table:
        if child.tag in (_W + "tblPr", _W + "tblGrid") or not isinstance(child.tag, str):
            continue
        if child.tag != _W + "tr":
            return None
        cells = []
        for cell in child:
            if cell.tag in (_W + "trPr", _W + "tblPrEx") or not isinstance(cell.tag, str):
                continue
            if cell.tag != _W + "tc":
                return None
            cells.append(cell)
        rows.append(cells)
    return rows


def _grid(table: ElementTree.Element) -> Optional[Tuple[Tuple[Tuple[str, str], ...], ...]]:
    """Each row's cells as ``(gridSpan, vMerge)``: equal grids compare cell by cell."""
    rows = _rows(table)
    if rows is None:
        return None
    shape = []
    for cells in rows:
        row = []
        for cell in cells:
            span = cell.find(f"{_W}tcPr/{_W}gridSpan")
            merge = cell.find(f"{_W}tcPr/{_W}vMerge")
            row.append((span.get(_W + "val", "1") if span is not None else "1",
                        (merge.get(_W + "val") or "continue") if merge is not None else ""))
        shape.append(tuple(row))
    return tuple(shape)


def _table_text(table: ElementTree.Element, lookup: Dict[int, _Paragraph]) -> str:
    """The table's wording, ``cell | cell / next row``, for keys and quotes."""
    rows = []
    for tr in table.iter(_W + "tr"):
        cells = []
        for tc in tr.findall(_W + "tc"):
            cells.append(" ".join(lookup[id(p)].text for p in tc.iter(_W + "p") if id(p) in lookup).strip())
        rows.append(" | ".join(cells))
    return " / ".join(rows)


def _blocks(container: ElementTree.Element, lookup: Dict[int, _Paragraph]) -> List[_Block]:
    blocks: List[_Block] = []
    for child in container:
        if child.tag == _W + "p":
            para = lookup[id(child)]
            blocks.append(_Block("p", child, _key(para.text), para))
        elif child.tag == _W + "tbl":
            blocks.append(_Block("tbl", child, "\x00tbl\x00" + _key(_table_text(child, lookup))))
        elif child.tag in _BLOCK_OTHER:
            blocks.append(_Block("other", child, "\x00other\x00" + _key(_nested_text(child))))
    return blocks


def _has_revisions(body: ElementTree.Element) -> bool:
    return any(node.tag in _REVISION_TAGS for node in body.iter())


# ── Pairing ──────────────────────────────────────────────────────────────────


def _similarity(a: str, b: str) -> float:
    words = SequenceMatcher(None, a.split(), b.split(), autojunk=False).ratio()
    if len(a) <= 300 and len(b) <= 300:
        return max(words, SequenceMatcher(None, a, b, autojunk=False).ratio())
    return words


def _pair(a: Sequence[_Block], b: Sequence[_Block], open_end: bool) -> List[Tuple[Optional[int], Optional[int]]]:
    """Events in document order: ``(i, j)`` paired, ``(i, None)`` removed,
    ``(None, j)`` added. ``open_end``: no paragraph follows this stretch."""
    n, m = len(a), len(b)
    if n * m > _MAX_PAIRINGS:
        return [(i, None) for i in range(n)] + [(None, j) for j in range(m)]

    def score(i: int, j: int) -> float:
        x, y = a[i], b[j]
        if x.kind != y.kind or x.kind == "other":
            return 0.0
        if n == m == 1:
            return 1.0
        threshold = _TABLE_PAIR_RATIO if x.kind == "tbl" else _PAIR_RATIO
        matcher = SequenceMatcher(None, x.key, y.key, autojunk=False)
        if matcher.real_quick_ratio() < threshold:
            return 0.0
        ratio = _similarity(x.key, y.key)
        return ratio if ratio >= threshold else 0.0

    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    move = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            options = [(best[i + 1][j], "del"), (best[i][j + 1], "ins")]
            s = score(i, j)
            if s:
                options.append((best[i + 1][j + 1] + s, "pair"))
            best[i][j], move[i][j] = max(options, key=lambda o: (o[0], o[1] == "pair"))
    events: List[Tuple[Optional[int], Optional[int]]] = []
    i = j = 0
    while i < n or j < m:
        if i == n:
            events.append((None, j))
            j += 1
        elif j == m:
            events.append((i, None))
            i += 1
        elif move[i][j] == "pair":
            events.append((i, j))
            i, j = i + 1, j + 1
        elif move[i][j] == "del":
            events.append((i, None))
            i += 1
        else:
            events.append((None, j))
            j += 1

    if open_end:
        # With no paragraph after the stretch, a removed paragraph keeps its
        # mark and an added one would take over the last original mark, which
        # leaves an empty paragraph on Accept All. Pair the last of each after
        # the last pairing instead: one rewording reads the same and is exact.
        tail = max((k for k, (x, y) in enumerate(events) if x is not None and y is not None), default=-1) + 1
        dels = [k for k in range(tail, len(events)) if events[k][1] is None and a[events[k][0]].kind == "p"]
        adds = [k for k in range(tail, len(events)) if events[k][0] is None and b[events[k][1]].kind == "p"]
        if dels and adds:
            d, k = dels[-1], adds[-1]
            pair = (events[d][0], events[k][1])
            events = [e for idx, e in enumerate(events) if idx not in (d, k)]
            events.insert(min(d, k), pair)
    return events


# ── Word-level diff of one paragraph ─────────────────────────────────────────


#: Alignment tokens: words, single punctuation marks and whitespace, so
#: ``manner.`` → ``manner consistent with industry standards.`` aligns ``manner``
#: and ``.`` rather than treating ``manner.`` as one word that changed.
_FINE_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")


def _norm_token(token: str) -> str:
    return " " if token.isspace() else token.translate(_FOLD)


def _word_edits(old: str, new: str) -> List[Tuple[int, int, str]]:
    """``(start, end, insert)`` spans of ``old`` to replace, left to right.

    Tokens are aligned finely, then each differing stretch is widened to whole
    words (both texts agree there, so the widening is the same on each side),
    stretches separated only by spaces and punctuation are joined, and the
    redline engine's ``_trim`` narrows each to the words that differ."""
    old_tokens, new_tokens = _FINE_TOKEN.findall(old), _FINE_TOKEN.findall(new)
    old_at, new_at = [0], [0]
    for token in old_tokens:
        old_at.append(old_at[-1] + len(token))
    for token in new_tokens:
        new_at.append(new_at[-1] + len(token))
    matcher = SequenceMatcher(
        lambda t: t == " ", [_norm_token(t) for t in old_tokens], [_norm_token(t) for t in new_tokens], autojunk=False
    )
    opcodes = matcher.get_opcodes()
    spans: List[List[int]] = []
    for index, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            continue
        floor = spans[-1][1] if spans else 0
        following = next((op for op in opcodes[index + 1 :] if op[0] != "equal"), None)
        ceiling = following[1] if following else len(old_tokens)

        def left_open() -> bool:  # does the stretch start inside a word?
            edge = (i1 < i2 and old_tokens[i1].isspace()) or (j1 < j2 and new_tokens[j1].isspace())
            return i1 > floor and not old_tokens[i1 - 1].isspace() and not edge

        def right_open() -> bool:
            edge = (i1 < i2 and old_tokens[i2 - 1].isspace()) or (j1 < j2 and new_tokens[j2 - 1].isspace())
            return i2 < ceiling and not old_tokens[i2].isspace() and not edge

        while left_open():
            i1, j1 = i1 - 1, j1 - 1
        while right_open():
            i2, j2 = i2 + 1, j2 + 1
        if spans and not re.search(r"\w", "".join(old_tokens[spans[-1][1] : i1])):
            spans[-1][1], spans[-1][3] = i2, j2
            continue
        spans.append([i1, i2, j1, j2])
    edits: List[Tuple[int, int, str]] = []
    for i1, i2, j1, j2 in spans:
        start = old_at[i1]
        removed, inserted = old[start : old_at[i2]], new[new_at[j1] : new_at[j2]]
        rel_start, rel_end, insert = _trim(removed, inserted)
        # Widening to whole words takes in a word's trailing punctuation:
        # "monthly." → "quarterly." marks only the word when both end alike.
        while rel_end > rel_start and insert and removed[rel_end - 1] == insert[-1] and insert[-1] in ".,;:!?":
            rel_end, insert = rel_end - 1, insert[:-1]
        if rel_start == rel_end and not insert:
            continue
        edits.append((start + rel_start, start + rel_end, insert))
    return edits


def _applied(old: str, edits: Sequence[Tuple[int, int, str]]) -> str:
    """``old`` with ``edits`` (left to right, not overlapping) applied."""
    for start, end, insert in reversed(edits):
        old = old[:start] + insert + old[end:]
    return old


def _describe(old: str, edits: Sequence[Tuple[int, int, str]]) -> str:
    parts = []
    for start, end, insert in edits[:3]:
        removed = old[start:end]
        if removed.strip() and insert.strip():
            parts.append(f'"{_short(removed, 40)}" → "{_short(insert, 40)}"')
        elif insert.strip():
            parts.append(f'added "{_short(insert, 40)}"')
        else:
            parts.append(f'deleted "{_short(removed, 40)}"')
    more = f" (+{len(edits) - 3} more)" if len(edits) > 3 else ""
    return "; ".join(parts) + more


def _accepted_text(paragraph: ElementTree.Element) -> str:
    """The paragraph's wording once its revisions are accepted. Read the way
    ``_segments`` reads it, so it compares with the revised paragraph's text.
    Every revision is this call's: a document with its own is refused."""
    parts: List[str] = []
    for child in paragraph:
        tag = child.tag
        if tag == _W + "pPr" or tag == _W + "del" or not isinstance(tag, str):
            continue
        if tag == _W + "ins":
            parts.extend(_run_text(run) for run in child if run.tag == _W + "r")
        elif tag == _W + "r":
            splittable = all(c.tag in _SPLITTABLE_RUN_CHILDREN for c in child)
            parts.append(_run_text(child) if splittable else "".join(t.text or "" for t in child if t.tag == _W + "t"))
        else:
            parts.append(_nested_text(child))
    return "".join(parts)


# ── Formatting copied from the revised document ──────────────────────────────


def _only_w(element: ElementTree.Element) -> ElementTree.Element:
    """Drop markup outside WordprocessingML: the original may not declare it."""
    for child in list(element):
        if not (isinstance(child.tag, str) and child.tag.startswith(_W)):
            element.remove(child)
        else:
            _only_w(child)
    for name in list(element.attrib):
        if name.startswith("{") and not name.startswith((_W, "{http://www.w3.org/XML/1998/namespace}")):
            del element.attrib[name]
    return element


def _ids_in(package_part: Optional[bytes], tag: str, attribute: str) -> Set[str]:
    if package_part is None:
        return set()
    try:
        root = ElementTree.fromstring(package_part)
    except ElementTree.ParseError:
        return set()
    return {node.get(_W + attribute) or "" for node in root.iter(_W + tag)}


# ── The comparison ───────────────────────────────────────────────────────────


@dataclass
class _Note:
    """A difference left as a comment, on the first candidate paragraph not removed."""

    candidates: List[ElementTree.Element]
    text: str
    line: str


class _Comparison:
    def __init__(self, original: _Document, revised: _Document) -> None:
        self.original = original
        paragraphs, self.units = _paragraphs(original.body)
        self.lookup = {id(p.element): p for p in paragraphs}
        rev_paragraphs, _ = _paragraphs(revised.body)
        self.rev_lookup = {id(p.element): p for p in rev_paragraphs}
        self.parents = {child: node for node in original.body.iter() for child in node}
        names = original.package.namelist()
        read = lambda name: original.package.read(name) if name in names else None  # noqa: E731
        self.styles = _ids_in(read("word/styles.xml"), "style", "styleId")
        self.numbering = _ids_in(read("word/numbering.xml"), "num", "numId")
        self.style_map: Dict[str, Counter] = {}
        self.num_map: Dict[str, Counter] = {}

        self.located: List[_Located] = []
        self.inserts: Dict[Tuple[int, str], Tuple[_Paragraph, List[_Paragraph]]] = {}
        self.removed: List[_Paragraph] = []
        self.removed_ids: Set[int] = set()
        self.additions: List[Tuple[List[_Block], int, List[_Paragraph], bool, List[ElementTree.Element]]] = []
        self.deletion_items: List[_Deletion] = []
        #: Deletions that take a staying paragraph's mark, by that paragraph.
        self.merged: Dict[int, _Deletion] = {}
        self.notes: List[_Note] = []
        #: Per paragraph: the revised wording its accept view must read as.
        self.expect: Dict[int, Tuple[_Paragraph, str]] = {}
        self.lines: List[Tuple[int, str]] = []  # (document order, line)
        self.order = {id(p.element): i for i, p in enumerate(paragraphs)}
        self.reworded = self.added = self.removed_count = 0

    # Walking ─────────────────────────────────────────────────────────────────

    def container(self, original: ElementTree.Element, revised: ElementTree.Element, cell: bool) -> None:
        a, b = _blocks(original, self.lookup), _blocks(revised, self.rev_lookup)
        matcher = SequenceMatcher(None, [x.key for x in a], [y.key for y in b], autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for x, y in zip(a[i1:i2], b[j1:j2]):
                    self.learn(x, y)
                continue
            open_end = i2 == len(a) or a[i2].kind != "p"
            pending: List[_Block] = []
            for i, j in _pair(a[i1:i2], b[j1:j2], open_end):
                if i is None:
                    pending.append(b[j1 + j])
                    continue
                self.add(a, i1 + i, pending, cell)
                pending = []
                if j is None:
                    self.remove(a, i1 + i, cell)
                else:
                    self.pair(a[i1 + i], b[j1 + j], cell)
            self.add(a, i2, pending, cell)

    def learn(self, x: _Block, y: _Block) -> None:
        if x.kind != "p" or y.kind != "p":
            return
        for path, mapping in ((f"{_W}pPr/{_W}pStyle", self.style_map), (f"{_W}pPr/{_W}numPr/{_W}numId", self.num_map)):
            ours, theirs = x.element.find(path), y.element.find(path)
            if ours is not None and theirs is not None:
                mapping.setdefault(theirs.get(_W + "val") or "", Counter())[ours.get(_W + "val") or ""] += 1

    def where(self, element: ElementTree.Element) -> int:
        return self.order.get(id(element), 0)

    def pair(self, x: _Block, y: _Block, cell: bool) -> None:
        if x.kind == "tbl":
            self.table(x, y)
            return
        self.learn(x, y)
        if x.key == y.key:
            return
        para, revised = x.para, y.para
        assert para is not None and revised is not None
        edits = _word_edits(para.text, revised.text)
        if not edits:
            return
        if _key(_applied(para.text, edits)) != _key(revised.text):  # a bug, never the user's input
            raise RedlineError(["The comparison would not read as the revised version once accepted, so nothing was saved."])
        segments = _segments(para.element)
        marked: List[Tuple[int, int, str]] = []
        locked: List[Tuple[int, int, str, str]] = []
        for start, end, insert in edits:
            reason = _locked_within(segments, start, end) or _locked_at(segments, start) or _locked_at(segments, end)
            if reason:
                locked.append((start, end, insert, reason))
            else:
                marked.append((start, end, insert))
        if locked:
            # The edits beside a hyperlink or field are still marked; the ones
            # that would cut into it are quoted in one comment on the paragraph.
            reasons = sorted({reason for *_, reason in locked})
            where = " and ".join(reasons)
            quoted = "; ".join(
                f'"{_short(para.text[start:end], 80)}" → "{_short(insert, 80)}"' for start, end, insert, _ in locked
            )
            self.note(
                [para.element],
                f"The revised version changes wording inside {where} here, which cannot be marked as a "
                f'tracked change: {_short(quoted, _QUOTE_CHARS)}. Revised paragraph: "{_short(revised.text, _QUOTE_CHARS)}"',
                f'Not marked: "{_short(para.text, 50)}" is reworded inside {where}; a comment quotes the revised wording.',
            )
        if not marked:
            return
        wanted = _applied(para.text, marked)
        edits = marked
        for start, end, insert in edits:
            change = Change(find=para.text[start:end], replace=insert)
            self.located.append(_Located(len(self.located), change, para, start, end, start, end, insert))
        self.expect[id(para.element)] = (para, wanted)
        self.reworded += 1
        prefix = "In a table: reworded" if cell else "Reworded"
        self.lines.append((self.where(para.element), f'{prefix} "{_short(para.text, 50)}": {_describe(para.text, edits)}'))

    def remove(self, a: Sequence[_Block], index: int, cell: bool) -> None:
        x = a[index]
        if x.kind != "p":
            what = "table" if x.kind == "tbl" else "content control"
            first = next(x.element.iter(_W + "p"), None)
            self.note(
                [first] if first is not None else [],
                f"The revised version removes this {what}; it is not marked as a tracked change.",
                f"Not marked: a {what} is removed in the revised version; a comment on it says so.",
            )
            return
        para = x.para
        assert para is not None
        reason = _undeletable(para.element)
        if reason:
            self.note(
                [para.element],
                f"The revised version removes this paragraph, but it {reason}, which cannot be "
                "marked as a tracked deletion.",
                f'Not marked: "{_short(para.text, 50)}" is removed, but it {reason}; a comment says so.',
            )
            return
        self.removed.append(para)
        self.removed_ids.add(id(para.element))
        self.expect[id(para.element)] = (para, "")
        self.removed_count += 1
        prefix = "In a table: removed" if cell else "Removed"
        self.lines.append((self.where(para.element), f'{prefix} "{_short(para.text, 60)}"'))

    def add(self, a: Sequence[_Block], gap: int, blocks: Sequence[_Block], cell: bool) -> None:
        """Write ``blocks`` (from the revised document) into the gap before ``a[gap]``."""
        if not blocks:
            return
        neighbours = [a[k] for k in (gap, gap - 1) if 0 <= k < len(a)]
        near = [n.element if n.kind == "p" else next(n.element.iter(_W + "p"), None) for n in neighbours]
        near = [e for e in near if e is not None]
        for block in blocks:
            if block.kind != "p":
                what = "table" if block.kind == "tbl" else "content control"
                wording = _table_text(block.element, self.rev_lookup) if block.kind == "tbl" else _nested_text(block.element)
                self.note(
                    near,
                    f'The revised version adds a {what} here, not marked as a tracked change: "{_short(wording, _QUOTE_CHARS)}"',
                    f"Not marked: a {what} is added in the revised version; a comment quotes it.",
                )
        paragraphs = [block.para for block in blocks if block.kind == "p" and block.para is not None]
        if paragraphs:
            self.additions.append((list(a), gap, paragraphs, cell, near))

    def anchor(self, a: Sequence[_Block], gap: int) -> Optional[Tuple[_Paragraph, str]]:
        """Where paragraphs added in the gap before ``a[gap]`` go: before the next
        paragraph that stays, else after the previous one that stays."""
        k = gap
        while k < len(a) and a[k].kind == "p" and id(a[k].element) in self.removed_ids:
            k += 1
        if k < len(a) and a[k].kind == "p" and a[k].para is not None:
            return a[k].para, "before"
        j = gap - 1
        while j >= 0 and a[j].kind == "p" and id(a[j].element) in self.removed_ids:
            j -= 1
        if j >= 0 and a[j].kind == "p" and a[j].para is not None:
            stays = a[j]
            if not (_ends_paragraph_run(stays.element, self.parents) and _mark_is_revised(stays.element)):
                return stays.para, "after"
        return None

    def place_additions(self) -> None:
        for a, gap, paragraphs, cell, near in self.additions:
            anchor = self.anchor(a, gap)
            if anchor is None:
                quoted = " / ".join(_short(p.text, 120) for p in paragraphs)
                self.note(
                    near,
                    f'The revised version adds wording here that cannot be marked as a tracked change: "{_short(quoted, _QUOTE_CHARS)}"',
                    f"Not marked: {len(paragraphs)} paragraph(s) added where no paragraph can hold them; a comment quotes them.",
                )
                continue
            para, position = anchor
            if position == "after" and id(para.element) in self.merged:
                # A deletion would take this paragraph's mark, and the engine
                # never inserts after such a paragraph: the deletion keeps its
                # own last mark instead.
                deletion = self.merged.pop(id(para.element))
                deletion.marks, deletion.merge_into = deletion.paragraphs[:-1], None
            self.inserts.setdefault((id(para.element), position), (para, []))[1].extend(paragraphs)
            self.added += len(paragraphs)
            count = "a paragraph" if len(paragraphs) == 1 else f"{len(paragraphs)} paragraphs"
            quoted = ", ".join(f'"{_short(p.text, 40)}"' for p in paragraphs[:3]) + (" …" if len(paragraphs) > 3 else "")
            prefix = "In a table: added" if cell else "Added"
            order = self.where(para.element) + (0.5 if position == "after" else -0.5)
            self.lines.append((order, f'{prefix} {count} {position} "{_short(para.text, 50)}": {quoted}'))

    def deletions(self) -> None:
        """The removed paragraphs as the redline engine's own deletions, one per
        run of neighbouring paragraphs, so each loses its mark as
        ``redline_word_document``'s ``delete`` would (§3c)."""
        runs: List[List[_Paragraph]] = []
        for para in sorted(self.removed, key=lambda p: self.where(p.element)):
            siblings = _block_siblings(para.element, self.parents)
            if runs:
                last = runs[-1][-1].element
                if self.parents[last] is self.parents[para.element]:
                    at = siblings.index(para.element)
                    if at > 0 and siblings[at - 1] is last:
                        runs[-1].append(para)
                        continue
            runs.append([para])
        for index, run in enumerate(runs):
            change = Change(find=run[0].text, delete="paragraph")
            deletion, problem = _deletion_of(index, change, run, self.lookup, self.parents)
            if deletion is None:
                # Nothing can take the last mark's place: its wording is struck
                # and the mark stays, as Word does at the end of a cell.
                deletion = _Deletion(index, change, run, run[:-1], None, None, run[-1])
            if deletion.merge_into is not None:
                self.merged[id(deletion.merge_into.element)] = deletion
            self.deletion_items.append(deletion)

    def table(self, x: _Block, y: _Block) -> None:
        grid = _grid(x.element)
        if grid is None or grid != _grid(y.element):
            first = next(x.element.iter(_W + "p"), None)
            wording = _table_text(y.element, self.rev_lookup)
            self.note(
                [first] if first is not None else [],
                "The revised version changes this table's rows or columns, so its changes are not marked "
                f'as tracked changes. Revised table: "{_short(wording, _QUOTE_CHARS)}"',
                f'Not marked: the table starting "{_short(_table_text(x.element, self.lookup), 40)}" has '
                "different rows or columns; a comment quotes the revised table.",
            )
            return
        for ours, theirs in zip(_rows(x.element) or [], _rows(y.element) or []):
            for cell, other in zip(ours, theirs):
                self.container(cell, other, cell=True)

    def note(self, candidates: Sequence[ElementTree.Element], text: str, line: str) -> None:
        self.notes.append(_Note(list(candidates), text, line))

    # Writing ────────────────────────────────────────────────────────────────

    def new_paragraph(self, revised: _Paragraph) -> NewParagraph:
        """The revised paragraph's runs, with formatting the original can hold."""
        runs: List[Tuple[str, Optional[ElementTree.Element]]] = []
        for segment in _segments(revised.element):
            if not segment.text:
                continue
            source = segment.element if segment.element.tag == _W + "r" else next(segment.element.iter(_W + "r"), None)
            rpr = _template_rpr(source)
            if rpr is not None:
                rpr = _only_w(rpr)
                style = rpr.find(_W + "rStyle")
                if style is not None and (style.get(_W + "val") or "") not in self.styles:
                    rpr.remove(style)
            if runs and _same_rpr(runs[-1][1], rpr):
                runs[-1] = (runs[-1][0] + segment.text, runs[-1][1])
            else:
                runs.append((segment.text, rpr))
        return NewParagraph(revised.text, runs=tuple(runs))

    def revised_like(self, revised: _Paragraph) -> _Paragraph:
        """A copy of the revised paragraph whose ``w:pPr`` names the original's
        styles and lists, so ``_template_ppr`` copies formatting that resolves."""
        element = ElementTree.Element(_W + "p")
        ppr = _only_w(_template_ppr(revised.element))
        for path, known, mapping in (
            (f"{_W}pStyle", self.styles, self.style_map),
            (f"{_W}numPr/{_W}numId", self.numbering, self.num_map),
        ):
            node = ppr.find(path)
            if node is None:
                continue
            value = node.get(_W + "val") or ""
            if value in mapping:
                node.set(_W + "val", mapping[value].most_common(1)[0][0])
            elif value not in known:
                # An id the original does not define would resolve to nothing.
                ppr.remove(node if path == f"{_W}pStyle" else ppr.find(f"{_W}numPr"))
        if len(ppr):
            element.append(ppr)
        return _Paragraph(element, -1, revised.text, revised.text.translate(_FOLD))

    def insertions(self) -> Tuple[List[_Insertion], Dict[Tuple[int, str], List[str]]]:
        items: List[_Insertion] = []
        expected: Dict[Tuple[int, str], List[str]] = {}
        for index, ((_, position), (anchor, revised)) in enumerate(self.inserts.items()):
            change = Change(find="", insert=position, paragraphs=tuple(self.new_paragraph(r) for r in revised))
            items.append(_Insertion(index, change, anchor, [self.revised_like(r) for r in revised]))
            expected[(anchor.unit, position)] = [_key(r.text) for r in revised]
        return items, expected

    def placed_notes(self) -> Dict[int, Tuple[ElementTree.Element, List[str]]]:
        """Notes by paragraph, each on its first candidate that is not removed."""
        placed: Dict[int, Tuple[ElementTree.Element, List[str]]] = {}
        for note in self.notes:
            host = next(
                (
                    c for c in note.candidates
                    if id(c) not in self.removed_ids and id(c) not in self.merged and id(c) in self.lookup
                ),
                None,
            )
            if host is not None:
                placed.setdefault(id(host), (host, []))[1].append(note.text)
        return placed


def compare(
    original: bytes,
    revised: bytes,
    author: str = DEFAULT_AUTHOR,
    now: Optional[datetime] = None,
) -> CompareResult:
    """Write ``revised``'s differences from ``original`` into a copy of
    ``original`` as tracked changes by ``author``."""
    try:
        ours = _open_document(original)
    except RedlineError as exc:
        raise RedlineError([f"Original: {p}" for p in exc.problems])
    with ours.package:
        try:
            theirs = _open_document(revised)
        except RedlineError as exc:
            raise RedlineError([f"Revised: {p}" for p in exc.problems])
        with theirs.package:
            problems = [
                f"{label} already contains tracked changes. Accept or reject them in Word first, "
                "or read it to see its changes."
                for label, doc in (("The original", ours), ("The revised version", theirs))
                if _has_revisions(doc.body)
            ]
            if problems:
                raise RedlineError(problems)
            comparison = _Comparison(ours, theirs)
            comparison.container(ours.body, theirs.body, cell=False)
            comparison.deletions()
            comparison.place_additions()

        placed = comparison.placed_notes()
        unmarked = [n.line for n in comparison.notes]
        if not (comparison.located or comparison.inserts or comparison.removed or placed):
            if unmarked:
                raise RedlineError(unmarked)
            raise NoDifferences(["The two documents have the same wording."])

        insertions, expected_inserts = comparison.insertions()
        more_dirty = {comparison.lookup[k].unit for k in placed}

        def apply_more(ids: _Ids, comments: List[Tuple[str, str]], extras: _Extras) -> None:
            for element, texts in placed.values():
                _comment_on_paragraph(element, "\n".join(texts), ids, comments)
            # Accept view: every marked paragraph must read as the revised one.
            wrong = [
                para.text
                for para, wanted in comparison.expect.values()
                if _key(_accepted_text(para.element)) != _key(wanted)
            ]
            for (unit, position), wanted in expected_inserts.items():
                before, after = extras.get(unit, ([], []))
                built = before if position == "before" else after
                if [_key(_accepted_text(p)) for p in built] != wanted:
                    wrong.append(f"{position} unit {unit}")
            if wrong:  # a bug, never the user's input
                raise RedlineError(["The comparison would not read as the revised version once accepted, so nothing was saved."])

        result = _write(
            ours, comparison.units, comparison.located, insertions, comparison.deletion_items, comparison.parents,
            author, now,
            more_dirty=sorted(more_dirty), apply_more=apply_more,
        )
    lines = [line for _, line in sorted(comparison.lines, key=lambda x: x[0])]
    return CompareResult(
        result.docx, lines, unmarked, comparison.reworded, comparison.added, comparison.removed_count
    )


__all__ = ["CompareResult", "DEFAULT_AUTHOR", "NoDifferences", "compare"]
