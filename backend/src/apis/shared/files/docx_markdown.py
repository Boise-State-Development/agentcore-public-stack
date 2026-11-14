"""A .docx rendered as markdown: headings, lists and tables kept as structure.

``document_read.docx_paragraphs`` is a search index: it flattens a table into
one paragraph per cell and drops heading levels and list numbering, which is
fine for ``pattern`` mode and wrong for reading. A model handed that text loses
which number sat in which column. This renderer keeps what Bedrock's own
document conversion keeps, and a little more:

* headings as ``#`` levels, from the paragraph style's outline level or its
  ``heading N`` / ``Title`` name;
* list items with their indent and their marker: ``-`` for a bullet level, a
  running ``N.`` for a numbered one, counted per list and level the way Word
  does;
* tables as markdown pipe tables, merged cells repeated into every grid slot
  they cover (a lookup by row or column then finds the value where it belongs),
  cell paragraphs joined with ``<br>``;
* an embedded picture as ``[image: <alt text>]`` — the text form cannot carry
  the picture itself, and saying one is there is better than silence;
* a native chart as its saved data (``office_charts``): a ``[chart: …]`` line,
  its axis titles and a table of its series, as a block after the paragraph
  that anchors it (inside a table cell, the one-line form);
* ``mc:AlternateContent`` read from its ``Choice`` only. Word writes a text box
  or a new-style chart twice, once for current readers and once as a
  ``Fallback`` for old ones, so reading both said everything twice.

Tracked changes are written out first through the same
``annotate_docx_for_model`` the inline document path and ``docx_paragraphs``
use, so all three agree on what the document says.

Pure stdlib (zipfile + ElementTree), deterministic: the same bytes always render
the same text, which is what lets a rendered attachment sit in the cacheable
prefix.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree

from apis.shared.files.office_charts import Chart, chart_line, chart_markdown, chart_reference, load_charts

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_V = "{urn:schemas-microsoft-com:vml}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_DOCUMENT_PART = "word/document.xml"
_HEADING_NAME = re.compile(r"^heading\s*(\d)$", re.IGNORECASE)
_BULLET_FORMATS = frozenset({"bullet", "none"})
#: Deepest heading level markdown has.
_MAX_HEADING = 6


class DocxRenderError(Exception):
    """The bytes are not a readable Word document."""


def _attr(node: Optional[ElementTree.Element], name: str) -> Optional[str]:
    if node is None:
        return None
    return node.get(f"{_W}{name}")


class _Styles:
    """Paragraph style id -> (heading level, numbering) from ``styles.xml``,
    following ``basedOn`` so a custom style built on ``Heading 2`` counts."""

    def __init__(self, xml: Optional[bytes]) -> None:
        self._raw: Dict[str, ElementTree.Element] = {}
        if xml:
            root = ElementTree.fromstring(xml)
            for style in root.iter(f"{_W}style"):
                sid = _attr(style, "styleId")
                if sid and _attr(style, "type") in (None, "paragraph"):
                    self._raw[sid] = style
        self._levels: Dict[str, Optional[int]] = {}
        self._numbering: Dict[str, Optional[Tuple[str, int]]] = {}

    def _chain(self, sid: str):
        seen = set()
        while sid and sid not in seen and sid in self._raw:
            seen.add(sid)
            style = self._raw[sid]
            yield style
            sid = _attr(style.find(f"{_W}basedOn"), "val") or ""

    def heading_level(self, sid: Optional[str]) -> Optional[int]:
        if not sid:
            return None
        if sid not in self._levels:
            self._levels[sid] = self._heading_level(sid)
        return self._levels[sid]

    def _heading_level(self, sid: str) -> Optional[int]:
        for style in self._chain(sid):
            name = (_attr(style.find(f"{_W}name"), "val") or "").strip()
            if name.lower() == "title":
                return 1
            match = _HEADING_NAME.match(name)
            if match:
                return int(match.group(1))
            outline = _attr(style.find(f"{_W}pPr/{_W}outlineLvl"), "val")
            if outline is not None and outline.isdigit() and int(outline) < 9:
                return int(outline) + 1
        # A document without styles.xml still names its headings by id.
        match = _HEADING_NAME.match(re.sub(r"(?i)^heading", "heading ", sid))
        return int(match.group(1)) if match else None

    def numbering(self, sid: Optional[str]) -> Optional[Tuple[str, int]]:
        if not sid:
            return None
        if sid not in self._numbering:
            found = None
            for style in self._chain(sid):
                num_pr = style.find(f"{_W}pPr/{_W}numPr")
                num_id = _attr(num_pr.find(f"{_W}numId") if num_pr is not None else None, "val")
                if num_id:
                    level = _attr(num_pr.find(f"{_W}ilvl"), "val") or "0"
                    found = (num_id, int(level) if level.isdigit() else 0)
                    break
            self._numbering[sid] = found
        return self._numbering[sid]


class _Numbering:
    """``numbering.xml``: which levels of which list are bullets, and the
    running counters Word would show."""

    def __init__(self, xml: Optional[bytes]) -> None:
        self._formats: Dict[Tuple[str, int], str] = {}
        self._starts: Dict[Tuple[str, int], int] = {}
        self._counters: Dict[str, Dict[int, int]] = {}
        if not xml:
            return
        root = ElementTree.fromstring(xml)
        abstract: Dict[str, Dict[int, Tuple[str, int]]] = {}
        for node in root.iter(f"{_W}abstractNum"):
            levels: Dict[int, Tuple[str, int]] = {}
            for lvl in node.iter(f"{_W}lvl"):
                ilvl = _attr(lvl, "ilvl") or "0"
                fmt = _attr(lvl.find(f"{_W}numFmt"), "val") or "decimal"
                start = _attr(lvl.find(f"{_W}start"), "val") or "1"
                levels[int(ilvl) if ilvl.isdigit() else 0] = (fmt, int(start) if start.isdigit() else 1)
            abstract[_attr(node, "abstractNumId") or ""] = levels
        for num in root.iter(f"{_W}num"):
            num_id = _attr(num, "numId") or ""
            ref = _attr(num.find(f"{_W}abstractNumId"), "val") or ""
            for ilvl, (fmt, start) in abstract.get(ref, {}).items():
                self._formats[(num_id, ilvl)] = fmt
                self._starts[(num_id, ilvl)] = start

    def marker(self, num_id: str, ilvl: int) -> str:
        fmt = self._formats.get((num_id, ilvl), "bullet")
        if fmt in _BULLET_FORMATS:
            return "-"
        counters = self._counters.setdefault(num_id, {})
        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]
        counters[ilvl] = counters.get(ilvl, self._starts.get((num_id, ilvl), 1) - 1) + 1
        return f"{counters[ilvl]}."


#: A chart a paragraph anchors: its relationship id and the drawing's alt text.
_ChartRef = Tuple[str, str]


def _drawing_alt(drawing: ElementTree.Element) -> str:
    for node in drawing.iter(f"{_WP}docPr"):
        return (node.get("descr") or node.get("title") or "").strip()
    return ""


def _run_text(paragraph: ElementTree.Element, *, in_cell: bool) -> Tuple[str, List[_ChartRef]]:
    """The paragraph's text, and the charts it anchors in document order."""
    parts: List[str] = []
    charts: List[_ChartRef] = []
    _walk(paragraph, parts, charts, in_cell)
    return "".join(parts).strip(), charts


def _walk(node: ElementTree.Element, parts: List[str], charts: List[_ChartRef], in_cell: bool) -> None:
    for child in node:
        tag = child.tag
        if tag == _MC_FALLBACK:
            continue
        if tag == f"{_W}t":
            parts.append(child.text or "")
        elif tag == f"{_W}tab":
            parts.append("\t")
        elif tag in (f"{_W}br", f"{_W}cr"):
            parts.append(" " if in_cell else "\n")
        elif tag == f"{_WP}docPr":
            alt = (child.get("descr") or child.get("title") or child.get("name") or "").strip()
            parts.append(f"[image: {alt}]" if alt else "[image]")
        elif tag == f"{_V}imagedata":
            parts.append("[image]")
        elif tag == f"{_W}drawing" and (rid := chart_reference(child)) is not None:
            charts.append((rid, _drawing_alt(child)))
        else:
            _walk(child, parts, charts, in_cell)


class _Renderer:
    def __init__(self, styles: _Styles, numbering: _Numbering, charts: Dict[str, Optional[Chart]]) -> None:
        self.styles = styles
        self.numbering = numbering
        self.charts = charts

    # -- blocks -----------------------------------------------------------

    def blocks(self, container: ElementTree.Element) -> List[Tuple[str, bool]]:
        """``(text, is_list_item)`` per block, in document order."""
        out: List[Tuple[str, bool]] = []
        for child in container:
            if child.tag == f"{_W}p":
                text, is_item, charts = self.paragraph(child)
                if text:
                    out.append((text, is_item))
                for rid, alt in charts:
                    out.append((chart_markdown(self.charts.get(rid), alt=alt), False))
            elif child.tag == f"{_W}tbl":
                table = self.table(child)
                if table:
                    out.append((table, False))
            elif child.tag in (f"{_W}sdt", f"{_W}sdtContent", f"{_W}customXml", f"{_W}ins", f"{_W}smartTag"):
                out.extend(self.blocks(child))
        return out

    def paragraph(self, para: ElementTree.Element) -> Tuple[str, bool, List[_ChartRef]]:
        text, charts = _run_text(para, in_cell=False)
        if not text:
            return "", False, charts
        ppr = para.find(f"{_W}pPr")
        style = _attr(ppr.find(f"{_W}pStyle") if ppr is not None else None, "val")
        level = self.styles.heading_level(style)
        if level is None and ppr is not None:
            outline = _attr(ppr.find(f"{_W}outlineLvl"), "val")
            if outline is not None and outline.isdigit() and int(outline) < 9:
                level = int(outline) + 1
        if level is not None:
            return f"{'#' * min(max(level, 1), _MAX_HEADING)} {text}", False, charts
        numbering = None
        num_pr = ppr.find(f"{_W}numPr") if ppr is not None else None
        if num_pr is not None:
            num_id = _attr(num_pr.find(f"{_W}numId"), "val")
            ilvl = _attr(num_pr.find(f"{_W}ilvl"), "val") or "0"
            if num_id and num_id != "0":
                numbering = (num_id, int(ilvl) if ilvl.isdigit() else 0)
        if numbering is None and num_pr is None:
            numbering = self.styles.numbering(style)
        if numbering is None:
            return text, False, charts
        num_id, ilvl = numbering
        marker = self.numbering.marker(num_id, ilvl)
        return f"{'  ' * ilvl}{marker} {text}", True, charts

    # -- tables -----------------------------------------------------------

    def cell_text(self, cell: ElementTree.Element) -> str:
        parts: List[str] = []
        for child in cell:
            if child.tag == f"{_W}p":
                text, charts = _run_text(child, in_cell=True)
                if text:
                    parts.append(text)
                parts.extend(chart_line(self.charts.get(rid), alt=alt) for rid, alt in charts)
            elif child.tag == f"{_W}tbl":
                # A nested table reads row by row inside its cell.
                for row in child.iter(f"{_W}tr"):
                    cells = [self.cell_text(c) for c in row.findall(f"{_W}tc")]
                    parts.append(" ; ".join(c for c in cells if c))
            elif child.tag in (f"{_W}sdt", f"{_W}sdtContent", f"{_W}customXml"):
                inner = self.cell_text(child)
                if inner:
                    parts.append(inner)
        return "<br>".join(p for p in parts if p).replace("|", "\\|").replace("\t", " ")

    def table(self, tbl: ElementTree.Element) -> str:
        grid: List[List[str]] = []
        # Column -> text of the cell a vertical merge is continuing.
        open_merges: Dict[int, str] = {}
        for tr in tbl.findall(f"{_W}tr"):
            row: List[str] = []
            col = 0
            for tc in _row_cells(tr):
                tcpr = tc.find(f"{_W}tcPr")
                span_val = _attr(tcpr.find(f"{_W}gridSpan") if tcpr is not None else None, "val")
                span = int(span_val) if span_val and span_val.isdigit() else 1
                vmerge = tcpr.find(f"{_W}vMerge") if tcpr is not None else None
                if vmerge is not None and _attr(vmerge, "val") != "restart":
                    text = open_merges.get(col, "")
                else:
                    text = self.cell_text(tc)
                    if vmerge is not None:
                        for c in range(col, col + span):
                            open_merges[c] = text
                    else:
                        for c in range(col, col + span):
                            open_merges.pop(c, None)
                row.extend([text] * span)
                col += span
            grid.append(row)
        if not grid or not any(any(c for c in r) for r in grid):
            return ""
        width = max(len(r) for r in grid)
        lines = []
        for i, row in enumerate(grid):
            cells = row + [""] * (width - len(row))
            lines.append("| " + " | ".join(cells) + " |")
            if i == 0:
                lines.append("|" + "|".join(["---"] * width) + "|")
        return "\n".join(lines)


def _row_cells(tr: ElementTree.Element) -> List[ElementTree.Element]:
    cells: List[ElementTree.Element] = []
    for child in tr:
        if child.tag == f"{_W}tc":
            cells.append(child)
        elif child.tag in (f"{_W}sdt", f"{_W}customXml"):
            cells.extend(child.iter(f"{_W}tc"))
    return cells


def docx_to_markdown(raw: bytes) -> str:
    """The document as markdown (see the module docstring). Raises
    :class:`DocxRenderError` for bytes that are not a Word package."""
    from apis.shared.files.document_read import annotate_docx_for_model

    raw = annotate_docx_for_model(raw)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
            document = archive.read(_DOCUMENT_PART)
            styles = archive.read("word/styles.xml") if "word/styles.xml" in names else None
            numbering = archive.read("word/numbering.xml") if "word/numbering.xml" in names else None
            charts = load_charts(archive, _DOCUMENT_PART)
    except (zipfile.BadZipFile, KeyError) as e:
        raise DocxRenderError(f"Could not open the Word document: {e}") from e
    try:
        root = ElementTree.fromstring(document)
        renderer = _Renderer(_Styles(styles), _Numbering(numbering), charts)
    except ElementTree.ParseError as e:
        raise DocxRenderError(f"Could not parse the Word document: {e}") from e
    body = root.find(f"{_W}body")
    if body is None:
        return ""
    return _join_blocks(renderer.blocks(body))


def _join_blocks(blocks: List[Tuple[str, bool]]) -> str:
    """Blank lines between blocks; a run of list items stays one tight list."""
    parts: List[str] = []
    previous_item = False
    for text, is_item in blocks:
        if parts:
            parts.append("\n" if (is_item and previous_item) else "\n\n")
        parts.append(text)
        previous_item = is_item
    return "".join(parts)


__all__ = ["DocxRenderError", "docx_to_markdown"]
