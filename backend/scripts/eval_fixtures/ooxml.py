"""Minimal Word and PowerPoint packages, written as OOXML with ``zipfile``. Stdlib only.

python-docx and python-pptx are not backend dependencies (the deck tools run
python-pptx inside Code Interpreter, never here), so the eval fixtures are
written as the XML parts themselves. Each package holds only the parts its
reader needs: a Word body for ``docx_markdown`` and Bedrock, and for a deck
the master, layout, theme and notes master python-pptx resolves when it opens
a slide and its notes.

Every part is written with a fixed timestamp, so the same content always
produces the same bytes.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple, Union
from xml.sax.saxutils import escape

#: Zip entries get one fixed date, so a fixture's bytes do not depend on when it was built.
_FIXED_DATE = (2026, 1, 5, 0, 0, 0)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def _zip(parts: Sequence[Tuple[str, str]]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts:
            info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text.encode("utf-8"))
    return out.getvalue()


def _rels(entries: Sequence[Tuple[str, str, str]]) -> str:
    body = "".join(f'<Relationship Id="{rid}" Type="{kind}" Target="{target}"/>' for rid, kind, target in entries)
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_PKG_REL}">{body}</Relationships>'


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tracked:
    """A tracked replacement inside a paragraph: ``deleted`` struck, ``inserted`` added."""

    deleted: str
    inserted: str


#: A paragraph is plain text, or a sequence of plain runs and tracked replacements.
Paragraph = Union[str, Sequence[Union[str, Tracked]]]


@dataclass(frozen=True)
class Table:
    """Rows of cells; an empty string is a blank cell, written as an empty paragraph."""

    rows: Sequence[Sequence[str]]


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _paragraph(paragraph: Paragraph, revision: List[int]) -> str:
    pieces = [paragraph] if isinstance(paragraph, str) else list(paragraph)
    body = []
    for piece in pieces:
        if isinstance(piece, Tracked):
            revision[0] += 1
            deleted = revision[0]
            revision[0] += 1
            body.append(
                f'<w:del w:id="{deleted}" w:author="Reviewer" w:date="2026-01-05T00:00:00Z">'
                f'<w:r><w:delText xml:space="preserve">{escape(piece.deleted)}</w:delText></w:r></w:del>'
                f'<w:ins w:id="{revision[0]}" w:author="Reviewer" w:date="2026-01-05T00:00:00Z">{_run(piece.inserted)}</w:ins>'
            )
        else:
            body.append(_run(piece))
    return f"<w:p>{''.join(body)}</w:p>"


def _table(table: Table) -> str:
    columns = max(len(row) for row in table.rows)
    grid = "".join('<w:gridCol w:w="1800"/>' for _ in range(columns))
    rows = []
    for row in table.rows:
        cells = []
        for value in list(row) + [""] * (columns - len(row)):
            inner = _run(value) if value else ""
            cells.append(f'<w:tc><w:tcPr><w:tcW w:w="1800" w:type="dxa"/></w:tcPr><w:p>{inner}</w:p></w:tc>')
        rows.append(f"<w:tr>{''.join(cells)}</w:tr>")
    return (
        '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>'
        f"<w:tblGrid>{grid}</w:tblGrid>{''.join(rows)}</w:tbl>"
    )


def docx(blocks: Sequence[Union[Paragraph, Table]]) -> bytes:
    """A Word document of paragraphs and tables, in order."""
    revision = [0]
    body = "".join(_table(b) if isinstance(b, Table) else _paragraph(b, revision) for b in blocks)
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{_W}">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="{_CT}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    return _zip([
        ("[Content_Types].xml", content_types),
        ("_rels/.rels", _rels([("rId1", f"{_REL}/officeDocument", "word/document.xml")])),
        ("word/document.xml", document),
    ])


# ---------------------------------------------------------------------------
# PowerPoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slide:
    title: str
    body: Sequence[str] = ()
    notes: Optional[str] = None


@dataclass
class _Deck:
    parts: List[Tuple[str, str]] = field(default_factory=list)
    overrides: List[Tuple[str, str]] = field(default_factory=list)

    def add(self, name: str, xml: str, content_type: Optional[str] = None) -> None:
        self.parts.append((name, xml))
        if content_type:
            self.overrides.append((f"/{name}", content_type))


_PML = "application/vnd.openxmlformats-officedocument.presentationml"
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_NS = f'xmlns:a="{_A}" xmlns:r="{_R}" xmlns:p="{_P}"'
_EMPTY_TREE = (
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
)
_CLR_MAP = (
    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" '
    'accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
)


def _theme() -> str:
    colors = "".join(
        f'<a:{name}><a:srgbClr val="{value}"/></a:{name}>'
        for name, value in (
            ("dk1", "000000"), ("lt1", "FFFFFF"), ("dk2", "1F2937"), ("lt2", "F3F4F6"),
            ("accent1", "2563EB"), ("accent2", "059669"), ("accent3", "D97706"), ("accent4", "DC2626"),
            ("accent5", "7C3AED"), ("accent6", "0891B2"), ("hlink", "2563EB"), ("folHlink", "7C3AED"),
        )
    )
    font = '<a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/>'
    fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    line = f'<a:ln w="9525">{fill}</a:ln>'
    return (
        f'{_XML_HEAD}<a:theme xmlns:a="{_A}" name="Eval"><a:themeElements>'
        f'<a:clrScheme name="Eval">{colors}</a:clrScheme>'
        f'<a:fontScheme name="Eval"><a:majorFont>{font}</a:majorFont><a:minorFont>{font}</a:minorFont></a:fontScheme>'
        f'<a:fmtScheme name="Eval"><a:fillStyleLst>{fill * 3}</a:fillStyleLst>'
        f"<a:lnStyleLst>{line * 3}</a:lnStyleLst>"
        "<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle>"
        "<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>"
        f"<a:bgFillStyleLst>{fill * 3}</a:bgFillStyleLst></a:fmtScheme>"
        "</a:themeElements></a:theme>"
    )


def _shape(shape_id: int, name: str, placeholder: str, paragraphs: Sequence[str], index: Optional[int] = None) -> str:
    ph = f'<p:ph type="{placeholder}"' + (f' idx="{index}"' if index is not None else "") + "/>"
    text = "".join(f"<a:p><a:r><a:rPr lang=\"en-US\"/><a:t>{escape(p)}</a:t></a:r></a:p>" for p in paragraphs) or "<a:p/>"
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{escape(name)}"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        f"<p:nvPr>{ph}</p:nvPr></p:nvSpPr><p:spPr/>"
        f"<p:txBody><a:bodyPr/><a:lstStyle/>{text}</p:txBody></p:sp>"
    )


def pptx(slides: Sequence[Slide]) -> bytes:
    """A deck of title-and-body slides, each with optional speaker notes."""
    deck = _Deck()
    has_notes = any(s.notes for s in slides)

    deck.add("ppt/theme/theme1.xml", _theme(), "application/vnd.openxmlformats-officedocument.theme+xml")
    deck.add(
        "ppt/slideMasters/slideMaster1.xml",
        f'{_XML_HEAD}<p:sldMaster {_NS}><p:cSld><p:spTree>{_EMPTY_TREE}'
        f'{_shape(2, "Title Placeholder 1", "title", [])}{_shape(3, "Text Placeholder 2", "body", [], 1)}'
        f'</p:spTree></p:cSld>{_CLR_MAP}<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        "<p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>",
        f"{_PML}.slideMaster+xml",
    )
    deck.add("ppt/slideMasters/_rels/slideMaster1.xml.rels", _rels([
        ("rId1", f"{_REL}/slideLayout", "../slideLayouts/slideLayout1.xml"),
        ("rId2", f"{_REL}/theme", "../theme/theme1.xml"),
    ]))
    deck.add(
        "ppt/slideLayouts/slideLayout1.xml",
        f'{_XML_HEAD}<p:sldLayout {_NS} type="obj" preserve="1"><p:cSld name="Title and Content"><p:spTree>{_EMPTY_TREE}'
        f'{_shape(2, "Title 1", "title", [])}{_shape(3, "Content Placeholder 2", "body", [], 1)}'
        "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>",
        f"{_PML}.slideLayout+xml",
    )
    deck.add("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _rels([
        ("rId1", f"{_REL}/slideMaster", "../slideMasters/slideMaster1.xml"),
    ]))

    if has_notes:
        deck.add("ppt/theme/theme2.xml", _theme(), "application/vnd.openxmlformats-officedocument.theme+xml")
        deck.add(
            "ppt/notesMasters/notesMaster1.xml",
            f'{_XML_HEAD}<p:notesMaster {_NS}><p:cSld><p:spTree>{_EMPTY_TREE}'
            f'{_shape(2, "Slide Image Placeholder 1", "sldImg", [])}{_shape(3, "Notes Placeholder 2", "body", [], 1)}'
            f"</p:spTree></p:cSld>{_CLR_MAP}</p:notesMaster>",
            f"{_PML}.notesMaster+xml",
        )
        deck.add("ppt/notesMasters/_rels/notesMaster1.xml.rels", _rels([
            ("rId1", f"{_REL}/theme", "../theme/theme2.xml"),
        ]))

    slide_ids = []
    presentation_rels = [
        ("rId1", f"{_REL}/slideMaster", "slideMasters/slideMaster1.xml"),
        ("rId2", f"{_REL}/theme", "theme/theme1.xml"),
    ]
    if has_notes:
        presentation_rels.append(("rId3", f"{_REL}/notesMaster", "notesMasters/notesMaster1.xml"))
    for number, slide in enumerate(slides, start=1):
        rid = f"rId{len(presentation_rels) + 1}"
        presentation_rels.append((rid, f"{_REL}/slide", f"slides/slide{number}.xml"))
        slide_ids.append(f'<p:sldId id="{255 + number}" r:id="{rid}"/>')
        deck.add(
            f"ppt/slides/slide{number}.xml",
            f'{_XML_HEAD}<p:sld {_NS}><p:cSld><p:spTree>{_EMPTY_TREE}'
            f'{_shape(2, "Title 1", "title", [slide.title])}{_shape(3, "Content Placeholder 2", "body", slide.body, 1)}'
            "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>",
            f"{_PML}.slide+xml",
        )
        slide_rels = [("rId1", f"{_REL}/slideLayout", "../slideLayouts/slideLayout1.xml")]
        if slide.notes:
            slide_rels.append(("rId2", f"{_REL}/notesSlide", f"../notesSlides/notesSlide{number}.xml"))
            deck.add(
                f"ppt/notesSlides/notesSlide{number}.xml",
                f'{_XML_HEAD}<p:notes {_NS}><p:cSld><p:spTree>{_EMPTY_TREE}'
                f'{_shape(2, "Slide Image Placeholder 1", "sldImg", [])}'
                f'{_shape(3, "Notes Placeholder 2", "body", [slide.notes], 1)}'
                "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>",
                f"{_PML}.notesSlide+xml",
            )
            deck.add(f"ppt/notesSlides/_rels/notesSlide{number}.xml.rels", _rels([
                ("rId1", f"{_REL}/notesMaster", "../notesMasters/notesMaster1.xml"),
                ("rId2", f"{_REL}/slide", f"../slides/slide{number}.xml"),
            ]))
        deck.add(f"ppt/slides/_rels/slide{number}.xml.rels", _rels(slide_rels))

    notes_master = '<p:notesMasterIdLst><p:notesMasterId r:id="rId3"/></p:notesMasterIdLst>' if has_notes else ""
    deck.add(
        "ppt/presentation.xml",
        f'{_XML_HEAD}<p:presentation {_NS}>'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"{notes_master}<p:sldIdLst>{''.join(slide_ids)}</p:sldIdLst>"
        '<p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
        "<p:defaultTextStyle/></p:presentation>",
        f"{_PML}.presentation.main+xml",
    )
    deck.add("ppt/_rels/presentation.xml.rels", _rels(presentation_rels))

    overrides = "".join(f'<Override PartName="{name}" ContentType="{kind}"/>' for name, kind in deck.overrides)
    content_types = (
        f'{_XML_HEAD}<Types xmlns="{_CT}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Default Extension="xml" ContentType="application/xml"/>{overrides}</Types>'
    )
    package_rels = _rels([("rId1", f"{_REL}/officeDocument", "ppt/presentation.xml")])
    return _zip([("[Content_Types].xml", content_types), ("_rels/.rels", package_rels), *deck.parts])
