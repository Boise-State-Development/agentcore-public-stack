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

Matching treats curly and straight quotes, dash variants and non-breaking spaces
as equal, and any run of whitespace as one, because a model rarely reproduces a
contract's typography exactly. It matches whole words: ``pay`` never matches
inside ``payable``.

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
class Change:
    """One requested edit. ``replace=""`` deletes; ``replace=None`` leaves the wording."""

    find: str
    replace: Optional[str] = None
    comment: Optional[str] = None
    occurrence: Optional[int] = None


@dataclass
class RedlineResult:
    docx: bytes
    #: One line per change, in request order, for the tool's summary.
    applied: List[str] = field(default_factory=list)


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


def _locate(changes: Sequence[Change], paragraphs: Sequence[_Paragraph]) -> List[_Located]:
    problems: List[str] = []
    located: List[_Located] = []
    for index, change in enumerate(changes):
        label = f"Change {index + 1}"
        if not isinstance(change.find, str) or not change.find.strip():
            problems.append(f"{label}: `find` is empty.")
            continue
        if change.replace is None and not (change.comment or "").strip():
            problems.append(f"{label}: give `replace` (new wording, \"\" to delete) or `comment`.")
            continue
        pattern = _pattern(change.find)
        matches = [(p, m) for p in paragraphs for m in pattern.finditer(p.folded)]
        quoted = f'"{_short(change.find)}"'
        if not matches:
            if "\n" in change.find.strip():
                problems.append(f"{label}: {quoted} spans paragraphs. A change must stay inside one paragraph.")
                continue
            loose = re.compile(pattern.pattern, re.IGNORECASE)
            near = next((m.group(0) for p in paragraphs for m in [loose.search(p.folded)] if m), None)
            hint = f' It does appear with different capitalization: "{_short(near)}".' if near else (
                " Copy the wording exactly as it appears in a single paragraph."
            )
            problems.append(f"{label}: {quoted} was not found.{hint}")
            continue
        if change.occurrence is None and len(matches) > 1:
            problems.append(
                f"{label}: {quoted} appears {len(matches)} times. Pass `occurrence` "
                f"(1–{len(matches)}) or a longer `find` that appears once."
            )
            continue
        number = change.occurrence or 1
        if not 1 <= number <= len(matches):
            problems.append(f"{label}: `occurrence` is {number}, but {quoted} appears {len(matches)} time(s).")
            continue
        paragraph, match = matches[number - 1]
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
                    problems.append(f"{label}: `replace` is the same wording as `find`.")
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
            problems.append(f"{label}: {quoted} runs into {reason}, which this tool cannot change.")
            continue
        located.append(_Located(index, change, paragraph, start, end, edit_start, edit_end, insert))

    by_paragraph: Dict[int, List[_Located]] = {}
    for item in located:
        by_paragraph.setdefault(id(item.paragraph.element), []).append(item)
    for items in by_paragraph.values():
        items.sort(key=lambda x: (x.edit_start, x.edit_end))
        for before, after in zip(items, items[1:]):
            touches = after.edit_start < before.edit_end or (
                after.edit_start == before.edit_end and (
                    before.edit_start == before.edit_end or after.edit_start == after.edit_end
                )
            )
            if touches:
                problems.append(f"Change {after.index + 1} overlaps change {before.index + 1}.")
    if problems:
        raise RedlineError(problems)
    return located


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
) -> str:
    """``xml`` with each dirty outermost paragraph replaced by its edited form."""
    spans = _unit_spans(xml, namespaces.w)
    if len(spans) != len(units):
        raise RedlineError(["The document's paragraph markup could not be matched up, so it was left unchanged."])

    pieces: List[Tuple[int, int, str]] = []
    for unit in sorted(set(dirty)):
        start, end = spans[unit]
        source = xml[start:end]
        local = _declarations(source)
        if _DEFAULT_XMLNS.search(source) or any(namespaces.bound.get(p) != u for p, u in local):
            raise RedlineError(["A paragraph to change uses namespace markup this tool cannot write back."])
        # A declaration that sat inside the paragraph is lost on parsing; put
        # it back on the paragraph, with any prefix the root does not declare.
        probe = _Writer(namespaces.prefixes)
        probe.write(units[unit], [], tail=False)
        declare = {p for p, _ in local} | (probe.used - set(namespaces.root))
        extra = "".join(f' xmlns:{p}="{_escape_attr(namespaces.bound[p])}"' for p in sorted(declare))
        out: List[str] = []
        _Writer(namespaces.prefixes).write(units[unit], out, extra, tail=False)
        pieces.append((start, end, "".join(out)))
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
) -> RedlineResult:
    """Apply ``changes`` to a .docx as tracked changes by ``author``."""
    if not changes:
        raise RedlineError(["No changes were given."])
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
        located = _locate(changes, paragraphs)

        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids = _Ids(_max_id(package) + 1, author, stamp)
        comments: List[Tuple[str, str]] = []
        summaries: Dict[int, str] = {}
        # Right to left within a paragraph, so earlier offsets stay valid.
        for item in sorted(located, key=lambda x: (x.paragraph.unit, -x.edit_start, -x.start)):
            summaries[item.index] = _apply(item, ids, comments)

        new_xml = _serialize_units(xml, namespaces, units, [item.paragraph.unit for item in located])
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
        return RedlineResult(buffer.getvalue(), [summaries[i] for i in sorted(summaries)])


__all__ = ["Change", "RedlineError", "RedlineResult", "redline"]
