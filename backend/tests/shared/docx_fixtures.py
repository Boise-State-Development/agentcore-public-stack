"""Minimal Word packages built in-test, for the tracked-changes ingestion tests.

A valid (if minimal) package — content types and relationships included, so a
real parser such as Bedrock's accepts it — with ``word/document.xml``, optionally
``word/comments.xml`` and extra story parts, and a media entry that must survive
a rewrite byte for byte.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Dict, List, Optional
from xml.etree import ElementTree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f'xmlns:w="{W_NS}"'
_MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'

#: A media entry, to prove parts the rewrite does not touch keep their bytes.
MEDIA_BYTES = bytes(range(256)) * 4


def paragraph(*runs: str) -> str:
    return f"<w:p>{''.join(runs)}</w:p>"


def run(text: str, *, bold: bool = False) -> str:
    props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:r>{props}<w:t xml:space="preserve">{text}</w:t></w:r>'


def deleted(text: str, author: str = "Sponsor Counsel", rid: int = 1) -> str:
    return (
        f'<w:del w:id="{rid}" w:author="{author}" w:date="2026-10-01T09:00:00Z">'
        f'<w:r><w:delText xml:space="preserve">{text}</w:delText></w:r></w:del>'
    )


def inserted(text: str, author: str = "Sponsor Counsel", rid: int = 2) -> str:
    return (
        f'<w:ins w:id="{rid}" w:author="{author}" w:date="2026-10-01T09:00:00Z">'
        f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:ins>'
    )


def document_xml(*paragraphs: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f'<w:document {_W} {_MC} mc:Ignorable="w14"><w:body>'
        f"{''.join(paragraphs)}<w:sectPr/></w:body></w:document>"
    )


_PKG = "http://schemas.openxmlformats.org/package/2006"
_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_OFFICE_DOCUMENT_REL = f"{_OFFICE_REL}/officeDocument"
_COMMENTS_REL = f"{_OFFICE_REL}/comments"
_DOCUMENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_COMMENTS_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"


def _content_types(overrides) -> str:
    parts = "".join(f'<Override PartName="{n}" ContentType="{t}"/>' for n, t in overrides)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_PKG}/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        f"{parts}</Types>"
    )


def _rels(relationships) -> str:
    body = "".join(
        f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>'
        for rid, rtype, target in relationships
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_PKG}/relationships">{body}</Relationships>'
    )


def build_docx(
    *paragraphs: str,
    comments: Optional[Dict[str, tuple]] = None,
    extra_parts: Optional[Dict[str, str]] = None,
) -> bytes:
    """A .docx whose body is ``paragraphs``. ``comments`` maps id → (author, text)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        overrides = [("/word/document.xml", _DOCUMENT_TYPE)]
        relationships = []
        if comments:
            overrides.append(("/word/comments.xml", _COMMENTS_TYPE))
            relationships.append(("rIdComments", _COMMENTS_REL, "comments.xml"))
        package.writestr("[Content_Types].xml", _content_types(overrides))
        package.writestr("_rels/.rels", _rels([("rId1", _OFFICE_DOCUMENT_REL, "word/document.xml")]))
        package.writestr("word/_rels/document.xml.rels", _rels(relationships))
        package.writestr("word/document.xml", document_xml(*paragraphs))
        if comments:
            body = "".join(
                f'<w:comment w:id="{cid}" w:author="{author}">'
                f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:comment>"
                for cid, (author, text) in comments.items()
            )
            package.writestr("word/comments.xml", f"<w:comments {_W}>{body}</w:comments>")
        for name, xml in (extra_parts or {}).items():
            package.writestr(name, xml)
        package.writestr("word/media/image1.png", MEDIA_BYTES, compress_type=zipfile.ZIP_STORED)
    return buffer.getvalue()


def part(docx: bytes, name: str = "word/document.xml") -> str:
    with zipfile.ZipFile(io.BytesIO(docx)) as package:
        return package.read(name).decode("utf-8")


def python_docx_paragraphs(docx: bytes, name: str = "word/document.xml") -> List[str]:
    """Paragraph text the way python-docx 1.2 (and so Docling) reads it.

    ``CT_P.text`` is ``"".join(e.text for e in self.xpath("w:r | w:hyperlink"))``:
    only *direct* children, so a run nested in ``w:ins``/``w:del`` is invisible.
    """
    root = ElementTree.fromstring(part(docx, name))
    texts = []
    for p in root.iter(f"{{{W_NS}}}p"):
        pieces = []
        for child in p:
            if child.tag in (f"{{{W_NS}}}r", f"{{{W_NS}}}hyperlink"):
                pieces.extend(t.text or "" for t in child.iter(f"{{{W_NS}}}t"))
        texts.append("".join(pieces))
    return texts


def all_text(docx: bytes, name: str = "word/document.xml") -> str:
    """Every ``w:t`` and ``w:delText`` in document order: what a markup-blind
    parser that reads every run (Bedrock's, per finding B9) produces."""
    xml = part(docx, name)
    return "".join(re.findall(r"<w:(?:t|delText)(?:\s[^>]*)?>([^<]*)</w:(?:t|delText)>", xml))
