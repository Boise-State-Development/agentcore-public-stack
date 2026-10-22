"""Make Word tracked changes visible to knowledge-base ingestion.

Neither ingestion engine understands revision markup, and they fail differently:

* **Managed** knowledge bases parse the raw ``.docx`` with Bedrock's own parser,
  which reads every text run and drops the markup around it. Deleted and inserted
  wording come out concatenated with no markers — "net thirty (30) dayspayable in
  full within ninety (90) days" — so a redline reads as if nothing was changed.
* **Legacy** ingestion converts through Docling, which reads paragraphs with
  python-docx. ``Paragraph.text`` and ``iter_inner_content()`` only see ``w:r`` and
  ``w:hyperlink`` *direct* children of ``w:p``, and every revised run sits one level
  down inside ``w:ins`` / ``w:del``, so both sides of a change silently vanish.

:func:`annotate_tracked_changes` rewrites the document so the change is ordinary
text: each revised span becomes a run reading ``[deleted: …]`` or
``[inserted: …]``, one note paragraph at the top of the body names the authors,
and Word comments are inlined at their anchor as ``[comment by …: …]``. The rest
of the package is untouched, so Docling still applies its own parsing — headings,
tables, lists — to the rewritten file. The managed engine is instead given
:func:`annotated_text`, the rewritten document as plain text, because its parser
does not read the markers verbatim (see that function).

A document with no tracked changes returns ``None`` and the caller keeps the
original bytes, so every other upload ingests exactly as it did before this
module existed. Comments alone do not trigger the rewrite for the same reason.

Moves (``w:moveFrom`` / ``w:moveTo``) are a deletion at the old place and an
insertion at the new one, and are marked that way. Formatting-only revisions
(``w:rPrChange`` and friends) change no wording and are left alone.

Stdlib only, at module scope and inside functions: this package is copied into
size-constrained Lambda images that carry no XML library beyond the standard one
(``tests/architecture/test_kb_backend_boundary.py``). The rewrite therefore works
on the serialized XML rather than through ``ElementTree``, whose serializer
renames namespace prefixes and drops the declarations ``mc:Ignorable`` refers to.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree
from xml.sax.saxutils import escape, unescape

#: The WordprocessingML MIME type, as Bedrock's ``byteContent`` names it.
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: Revision containers, and the label their wording is shown under.
_CONTAINER_LABELS = {
    "w:ins": "inserted",
    "w:moveTo": "inserted",
    "w:del": "deleted",
    "w:moveFrom": "deleted",
}

#: Text elements whose content is wording. ``w:delText`` is what a deleted run
#: holds instead of ``w:t``; it is renamed so a parser that reads only ``w:t``
#: (python-docx, and so Docling) still sees it.
_TEXT_ELEMENTS = frozenset({"w:t", "w:delText"})
_RENAMES = {"w:delText": "w:t", "w:delInstrText": "w:instrText"}
_RENAME_CLOSE = {name: f"</{target}>" for name, target in _RENAMES.items()}

#: Story parts: the body, headers, footers, footnotes, endnotes and comments.
_STORY_PART = re.compile(r"^word/[^/]+\.xml$")

#: One markup token. Quoted attribute values may legally contain ``>``.
_TAG = re.compile(r"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")
_NAME = re.compile(r"^</?\s*([\w.:-]+)")
_AUTHOR = re.compile(r"\bw:author\s*=\s*(\"[^\"]*\"|'[^']*')")
_ID = re.compile(r"\bw:id\s*=\s*(\"[^\"]*\"|'[^']*')")
_BODY_OPEN = re.compile(r"<w:body(?:\s[^>]*)?>")


def _run(text: str) -> str:
    """A plain run carrying ``text`` verbatim."""
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _attr(pattern: re.Pattern, tag: str) -> Optional[str]:
    match = pattern.search(tag)
    return unescape(match.group(1)[1:-1], {"&quot;": '"', "&apos;": "'"}) if match else None


class _Frame:
    """Output buffered for one open revision container."""

    __slots__ = ("name", "author", "label", "parts", "has_text")

    def __init__(self, name: str, author: Optional[str]) -> None:
        self.name = name
        self.author = author
        self.label = _CONTAINER_LABELS[name]
        self.parts: List[str] = []
        self.has_text = False


def _rewrite_part(
    xml: str,
    comments: Dict[str, Tuple[str, str]],
    authors: List[str],
) -> Tuple[str, bool]:
    """Rewrite one story part. Returns ``(xml, changed_any_wording)``.

    ``authors`` is appended to in first-seen order, so the note at the top of the
    document is deterministic for a given file.
    """
    out: List[str] = []
    frames: List[_Frame] = []
    elements: List[str] = []  # open element names, outermost first
    pending_comments: List[str] = []
    changed = False

    def emit(piece: str) -> None:
        (frames[-1].parts if frames else out).append(piece)

    pos = 0
    for match in _TAG.finditer(xml):
        if match.start() > pos:
            text = xml[pos : match.start()]
            if frames and elements and elements[-1] in _TEXT_ELEMENTS and text.strip():
                for frame in frames:
                    frame.has_text = True
            emit(text)
        pos = match.end()

        tag = match.group(0)
        if tag.startswith(("<?", "<!")):
            emit(tag)
            continue
        name_match = _NAME.match(tag)
        if not name_match:
            emit(tag)
            continue
        name = name_match.group(1)
        closing = tag.startswith("</")
        self_closing = tag.endswith("/>")

        if closing:
            if elements and elements[-1] == name:
                elements.pop()
            if frames and frames[-1].name == name:
                frame = frames.pop()
                body = "".join(frame.parts)
                if frame.has_text:
                    changed = True
                    if frame.author and frame.author not in authors:
                        authors.append(frame.author)
                    emit(_run(f"[{frame.label}: ") + body + _run("]"))
                elif frame.label == "inserted":
                    # An inserted picture or field with no wording: keep it, unwrapped.
                    emit(body)
                continue
            emit(_RENAME_CLOSE.get(name, tag) if frames else tag)
            if name == "w:r" and pending_comments:
                for comment_id in pending_comments:
                    author, text = comments[comment_id]
                    emit(_run(f" [comment by {author}: {text}]"))
                pending_comments.clear()
            continue

        parent = elements[-1] if elements else ""
        is_container = (
            name in _CONTAINER_LABELS
            and not self_closing
            # w:ins / w:del inside a property element (w:rPr, w:trPr, ...) marks a
            # paragraph mark or table row as revised. It holds no wording and
            # must stay where it is.
            and not parent.endswith("Pr")
        )
        if is_container:
            frames.append(_Frame(name, _attr(_AUTHOR, tag)))
            elements.append(name)
            continue

        if name == "w:commentReference":
            comment_id = _attr(_ID, tag)
            if comment_id in comments:
                pending_comments.append(comment_id)

        if frames and name in _RENAMES:
            tag = tag.replace(name, _RENAMES[name], 1)
        emit(tag)
        if not self_closing:
            elements.append(name)

    emit(xml[pos:])
    # Unbalanced input: never lose wording, just its markers. Outer frames hold
    # the earlier content, so they are flushed first.
    out.extend("".join(frame.parts) for frame in frames)
    return "".join(out), changed


def _read_comments(package: zipfile.ZipFile) -> Dict[str, Tuple[str, str]]:
    """Comment id → (author, text) from ``word/comments.xml``, if it has any."""
    try:
        raw = package.read("word/comments.xml")
    except KeyError:
        return {}
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return {}
    comments: Dict[str, Tuple[str, str]] = {}
    for comment in root.iter(f"{{{_W_NS}}}comment"):
        comment_id = comment.get(f"{{{_W_NS}}}id")
        if comment_id is None:
            continue
        paragraphs = [
            "".join(t.text or "" for t in paragraph.iter(f"{{{_W_NS}}}t")).strip()
            for paragraph in comment.iter(f"{{{_W_NS}}}p")
        ]
        text = " ".join(p for p in paragraphs if p)
        if text:
            author = comment.get(f"{{{_W_NS}}}author") or "unknown"
            comments[comment_id] = (author, text)
    return comments


def _note(authors: List[str], has_comments: bool) -> str:
    by = f" by {', '.join(authors)}" if authors else ""
    note = (
        f"Note: this document contains tracked changes{by}. Deleted wording is "
        f"shown as [deleted: ...] and inserted wording as [inserted: ...]; reading "
        f"it with insertions kept and deletions removed gives the proposed text."
    )
    if has_comments:
        note += " Reviewer comments are shown as [comment by <author>: ...]."
    return f"<w:p>{_run(note)}</w:p>"


def annotate_tracked_changes(docx_bytes: bytes) -> Optional[bytes]:
    """Return a copy of the document with tracked changes written out as text.

    ``None`` when there is nothing to annotate — no tracked change with wording in
    it, or bytes that are not a Word package at all. The caller then uses the
    original bytes, unchanged.
    """
    try:
        package = zipfile.ZipFile(io.BytesIO(docx_bytes))
    except (zipfile.BadZipFile, ValueError):
        return None

    with package:
        names = package.namelist()
        if "word/document.xml" not in names:
            return None

        comments = _read_comments(package)
        authors: List[str] = []
        rewritten: Dict[str, str] = {}
        any_changed = False
        used_comments = False
        for name in names:
            if not _STORY_PART.match(name) or name == "word/comments.xml":
                continue
            try:
                xml = package.read(name).decode("utf-8")
            except UnicodeDecodeError:
                continue
            if "<w:ins" not in xml and "<w:del" not in xml and "<w:move" not in xml and not (
                comments and "<w:commentReference" in xml
            ):
                continue
            new_xml, changed = _rewrite_part(xml, comments, authors)
            any_changed = any_changed or changed
            if new_xml != xml:
                rewritten[name] = new_xml
                used_comments = used_comments or "[comment by " in new_xml

        if not any_changed:
            return None

        document = rewritten["word/document.xml"] if "word/document.xml" in rewritten else (
            package.read("word/document.xml").decode("utf-8")
        )
        body = _BODY_OPEN.search(document)
        if body:
            note = _note(authors, used_comments)
            rewritten["word/document.xml"] = document[: body.end()] + note + document[body.end() :]

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as target:
            for info in package.infolist():
                data = (
                    rewritten[info.filename].encode("utf-8")
                    if info.filename in rewritten
                    else package.read(info.filename)
                )
                target.writestr(info, data)
        return buffer.getvalue()


# ── Plain text ───────────────────────────────────────────────────────────────
_W = f"{{{_W_NS}}}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: List[str] = []

    def walk(node: ElementTree.Element) -> None:
        for child in node:
            if child.tag == _MC_FALLBACK:
                # The same content as the mc:Choice beside it, for older readers.
                continue
            if child.tag == f"{_W}t":
                parts.append(child.text or "")
            elif child.tag == f"{_W}tab":
                parts.append("\t")
            elif child.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
            elif child.tag == f"{_W}noBreakHyphen":
                parts.append("-")
            else:
                walk(child)

    walk(paragraph)
    return "".join(parts).strip()


def _block_text(container: ElementTree.Element, out: List[str]) -> None:
    """Paragraphs in order; a table becomes one ``cell | cell`` line per row."""
    for child in container:
        if child.tag == f"{_W}p":
            text = _paragraph_text(child)
            if text:
                out.append(text)
        elif child.tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells = []
                for cell in row.findall(f"{_W}tc"):
                    lines: List[str] = []
                    _block_text(cell, lines)
                    cells.append(" ".join(lines))
                if any(cells):
                    out.append(" | ".join(cells))
        elif child.tag == f"{_W}sdt":
            content = child.find(f"{_W}sdtContent")
            if content is not None:
                _block_text(content, out)


def document_text(docx_bytes: bytes) -> str:
    """The body text of a Word package, then its footnotes and endnotes.

    Paragraphs are separated by a blank line. Headers and footers are left out:
    they repeat on every page and rarely carry the terms a reader asks about.
    """
    lines: List[str] = []
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as package:
        names = set(package.namelist())
        body = ElementTree.fromstring(package.read("word/document.xml")).find(f"{_W}body")
        if body is not None:
            _block_text(body, lines)
        for part_name, tag in (("word/footnotes.xml", "footnote"), ("word/endnotes.xml", "endnote")):
            if part_name not in names:
                continue
            for note in ElementTree.fromstring(package.read(part_name)).iter(f"{_W}{tag}"):
                if note.get(f"{_W}type") in ("separator", "continuationSeparator", "continuationNotice"):
                    continue
                note_lines: List[str] = []
                _block_text(note, note_lines)
                if note_lines:
                    lines.append(f"[{tag}: {' '.join(note_lines)}]")
    return "\n\n".join(lines)


def annotated_text(docx_bytes: bytes) -> Optional[str]:
    """The document as plain text with its tracked changes written out.

    ``None`` exactly when :func:`annotate_tracked_changes` returns ``None``.

    Text rather than the rewritten package, for the managed engine: a knowledge
    base provisioned with image extraction enabled — every one this platform
    creates — parses an uploaded ``.docx`` through a model, and that model treats
    ``[deleted: ...]`` as an instruction rather than as text. Measured on dev: it
    dropped "Idaho" from ``[deleted: Idaho]``, cut ``[deleted: Ada County,
    Idaho]`` to ``[deleted: Ada County, o]`` and swallowed the note's ``...]``.
    Inline text is chunked as given and came back verbatim.
    """
    annotated = annotate_tracked_changes(docx_bytes)
    if annotated is None:
        return None
    return document_text(annotated)


__all__ = ["DOCX_MIME_TYPE", "annotate_tracked_changes", "annotated_text", "document_text"]
