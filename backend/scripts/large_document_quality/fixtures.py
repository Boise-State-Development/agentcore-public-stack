"""The four fixture documents and their questions (large-document spec §6).

One document per class the readout has to decide on:

* ``text``    a 300-page policy manual with a real text layer;
* ``scanned`` a 40-page inspection report whose pages are images only, so our
  text layer is empty and ``pattern`` can find nothing;
* ``layout``  a 24-page brochure whose tables and columns are drawn in a
  different order from how they read, so the extracted text separates each
  label from its value;
* ``docx``    a ~15 MB Word handbook (its size is mostly one embedded image).

Every document is synthetic and deterministic: the organisations, people and
numbers are invented, and the filler is generated from fixed word lists by a
seeded RNG, so the same seed always produces byte-identical files. Each is over
the 4 MB inline limit, so it is attached by reference like a real large upload.

Each question has an authored answer, planted on a known page. ``wording`` is
``literal`` when the question reuses the document's own words (a ``pattern``
search can find it) and ``paraphrase`` when it does not (the case semantic search
exists for). ``forbidden`` values are distractors planted elsewhere in the same
document, so an answer taken from the wrong place fails. ``locator`` is a regex
that matches the planted sentence in a text layer, used by the free ``probe``.
"""

from __future__ import annotations

import io
import random
import struct
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import pdf

DOC_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass(frozen=True)
class Question:
    qid: str
    doc: str
    wording: str  # literal | paraphrase
    question: str
    expected: Tuple[str, ...]
    forbidden: Tuple[str, ...] = ()
    page: Optional[int] = None
    locator: str = ""

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class Fixture:
    name: str
    filename: str
    doc_class: str
    fmt: str
    data: bytes
    questions: List[Question] = field(default_factory=list)

    @property
    def mime_type(self) -> str:
        return DOC_MIME[self.fmt]


# ---------------------------------------------------------------------------
# Filler text: fixed vocabularies, no digits, none of the planted values
# ---------------------------------------------------------------------------

_SUBJECTS = [
    "Each team", "The facilities group", "Line managers", "The records office", "Every depot",
    "The finance unit", "Supervisors", "The service desk", "Field crews", "The training office",
    "Department heads", "The safety committee", "Project leads", "The procurement team",
]
_VERBS = [
    "reviews", "maintains", "publishes", "coordinates", "documents", "approves", "monitors",
    "updates", "schedules", "audits", "circulates", "consolidates", "confirms", "archives",
]
_OBJECTS = [
    "the shared calendar", "its operating procedures", "supplier contracts", "the asset register",
    "visitor arrangements", "the induction checklist", "equipment inspections", "vehicle bookings",
    "quarterly summaries", "the emergency contact list", "workspace allocations", "printed forms",
    "the incident log", "meeting minutes", "storage requests", "community feedback",
]
_QUALIFIERS = [
    "in line with current guidance", "before the end of each cycle", "with the relevant manager",
    "through the internal portal", "where practicable", "as part of routine operations",
    "unless an exception has been approved", "in consultation with affected staff",
    "using the standard template", "at the start of each period", "without undue delay",
]
#: Sentences that share words with the questions but answer none of them, so a
#: naive ``pattern`` search returns noise as well as the fact.
_DECOYS = [
    "Notice boards in each depot are refreshed with new announcements.",
    "Leave planning conversations should start early in the year.",
    "Meal arrangements for events are coordinated by the facilities team.",
    "Travel bookings are made through the approved agency.",
    "Internet access in public areas is filtered for safety.",
    "Badges must be worn visibly at all times on site.",
    "Desk equipment requests are handled by the service desk.",
    "Reimbursement claims must include itemised receipts.",
    "Contact details for each team are kept on the intranet.",
    "Parking for visitors is limited during community events.",
    "Opening hours may change during public holidays.",
    "Membership questions are answered at the front counter.",
]


def _sentence(rng: random.Random) -> str:
    return f"{rng.choice(_SUBJECTS)} {rng.choice(_VERBS)} {rng.choice(_OBJECTS)} {rng.choice(_QUALIFIERS)}."


def _paragraph(rng: random.Random, sentences: int) -> str:
    parts = [_sentence(rng) for _ in range(sentences)]
    if rng.random() < 0.35:
        parts.insert(rng.randrange(len(parts) + 1), rng.choice(_DECOYS))
    return " ".join(parts)


def _noise_rgb(rng: random.Random, width: int, height: int) -> bytes:
    """A unique, smooth-ish raw RGB image: a gradient plus noise. Unique per page
    on purpose: a resource shared by every page is copied into every shard."""
    base = [rng.randrange(256) for _ in range(3)]
    out = bytearray(width * height * 3)
    i = 0
    for y in range(height):
        for x in range(width):
            out[i] = (base[0] + x + rng.randrange(48)) & 0xFF
            out[i + 1] = (base[1] + y + rng.randrange(48)) & 0xFF
            out[i + 2] = (base[2] + (x ^ y) + rng.randrange(48)) & 0xFF
            i += 3
    return bytes(out)


# ---------------------------------------------------------------------------
# text: 300-page policy manual
# ---------------------------------------------------------------------------

_MANUAL_FACTS: Dict[int, List[str]] = {
    37: ["An employee who resigns must give the Authority no fewer than 34 calendar days of written notice."],
    88: ["The per-diem meal allowance for travel within the region is $47.50."],
    143: ["Staff may carry over at most 11 days of unused annual leave into the next leave year."],
    201: ["Reimbursement of home internet costs for approved remote workers is capped at $38.25 per month."],
    256: ["Ergonomic workstation assessments are arranged by Orla Penhallow on extension 4471."],
    290: ["The fee for replacing a lost or damaged identity badge is $23.90."],
    # Distractors: same subjects, different values, elsewhere in the document.
    119: ["The per-diem meal allowance for interstate travel is $63.20."],
    176: ["Casual staff may carry over at most 3 days of unused leave."],
    212: ["Contractors who end an engagement early must give 12 calendar days of notice."],
}

_MANUAL_QUESTIONS = [
    Question("text-notice", "manual", "paraphrase", "How much advance notice do I have to give if I quit?",
             ("34 calendar days", "34 days"), ("12 calendar days",), 37, r"34 calendar days"),
    Question("text-perdiem", "manual", "literal", "What is the per-diem meal allowance for travel within the region?",
             ("47.50",), ("63.20",), 88, r"per-diem meal allowance for travel within the region"),
    Question("text-carryover", "manual", "paraphrase",
             "If I don't use all my vacation this year, how much of it can I roll over?",
             ("11 days", "eleven days"), ("3 days",), 143, r"carry over at most 11 days"),
    Question("text-wifi", "manual", "paraphrase",
             "I work from home. Will the Authority pay for my wifi, and up to how much?",
             ("38.25",), (), 201, r"home internet costs"),
    Question("text-desk", "manual", "paraphrase",
             "Who do I contact to get my desk setup checked, and what number do I call?",
             ("4471",), (), 256, r"extension 4471"),
    Question("text-badge", "manual", "literal", "What is the fee for replacing a lost identity badge?",
             ("23.90",), (), 290, r"identity badge"),
]


def build_manual(seed: int, pages: int = 300) -> Fixture:
    rng = random.Random(seed * 1009 + 1)
    out: List[pdf.Page] = []
    for number in range(1, pages + 1):
        page = pdf.Page()
        page.text(50, 760, "Harrow Valley Regional Authority - Staff Policy Manual", 9, bold=True)
        page.text(520, 760, f"Page {number}", 9)
        page.rule(50, 752, 562, 752)
        chapter = (number - 1) // 12 + 1
        page.text(50, 728, f"Chapter {chapter}. {rng.choice(_OBJECTS).capitalize()}", 13, bold=True)
        y = 706.0
        facts = list(_MANUAL_FACTS.get(number, ()))
        paragraphs = 6
        for i in range(paragraphs):
            text = _paragraph(rng, rng.randint(3, 5))
            if facts and i == 2:
                text = f"{text} {facts.pop(0)}"
            y = page.paragraph(50, y, text, 512) - 8
        page.image(pdf.Image(96, 72, _noise_rgb(rng, 96, 72)), 418, 60, 144, 108)
        out.append(page)
    questions = [q for q in _MANUAL_QUESTIONS if q.page is None or q.page <= pages]
    return Fixture("manual", "staff-policy-manual.pdf", "text", "pdf", pdf.build(out), questions)


# ---------------------------------------------------------------------------
# scanned: 40 image-only pages
# ---------------------------------------------------------------------------

_SCANNED_FACTS: Dict[int, str] = {
    6: "Bridge 14-C load rating: 32 tonnes.",
    15: "The culvert at Marsh Lane is scheduled for replacement in March 2029.",
    22: "Inspector of record for this survey: Dafydd Kestrel.",
    29: "Estimated repair cost for the north abutment: $418,600.",
    35: "Recommended inspection interval is reduced from 24 months to 9 months.",
    11: "Estimated repair cost for the south wingwall: $96,300.",
}

_SCANNED_QUESTIONS = [
    Question("scan-load", "scanned", "literal", "What is the load rating of bridge 14-C?",
             ("32 tonnes", "32 t"), (), 6, r"load rating"),
    Question("scan-culvert", "scanned", "paraphrase", "When is the Marsh Lane culvert going to be replaced?",
             ("March 2029",), (), 15, r"Marsh Lane"),
    Question("scan-inspector", "scanned", "paraphrase", "Who signed off on this inspection as the inspector?",
             ("Kestrel",), (), 22, r"Inspector of record"),
    Question("scan-abutment", "scanned", "paraphrase", "How much is it going to cost to fix the north abutment?",
             ("418,600", "418600"), ("96,300",), 29, r"north abutment"),
    Question("scan-interval", "scanned", "paraphrase", "How often should the structure be inspected from now on?",
             ("9 months", "nine months"), (), 35, r"inspection interval"),
]


def _scan_page(rng: random.Random, number: int, fact: Optional[str]) -> bytes:
    from PIL import Image as PILImage, ImageDraw, ImageFilter, ImageFont

    width, height = 1275, 1650  # 8.5 x 11 in at 150 dpi
    page = PILImage.new("L", (width, height), 250)
    draw = ImageDraw.Draw(page)
    title = ImageFont.load_default(size=30)
    body = ImageFont.load_default(size=24)
    draw.text((110, 90), "HARROW VALLEY REGIONAL AUTHORITY", font=title, fill=20)
    draw.text((110, 132), f"Structures Inspection Report  -  Sheet {number} of 40", font=body, fill=30)
    draw.line((110, 172, width - 110, 172), fill=40, width=2)
    y = 210
    lines: List[str] = []
    for i in range(7):
        text = _paragraph(rng, rng.randint(2, 4))
        if fact and i == 3:
            text = f"{text} {fact}"
        lines.extend(pdf.wrap(text, 10, 470))
        lines.append("")
    for line in lines:
        if y > height - 140:
            break
        draw.text((110, y), line, font=body, fill=rng.randint(15, 45))
        y += 34
    page = page.rotate(rng.uniform(-0.6, 0.6), resample=PILImage.BICUBIC, fillcolor=250)
    noise = PILImage.effect_noise((width, height), 18).convert("L")
    page = PILImage.blend(page, noise, 0.08).filter(ImageFilter.GaussianBlur(0.5))
    buffer = io.BytesIO()
    page.save(buffer, format="JPEG", quality=70)
    return buffer.getvalue()


def build_scanned(seed: int, pages: int = 40) -> Fixture:
    rng = random.Random(seed * 1009 + 2)
    out: List[pdf.Page] = []
    for number in range(1, pages + 1):
        jpeg = _scan_page(rng, number, _SCANNED_FACTS.get(number))
        page = pdf.Page()
        page.image(pdf.Image(1275, 1650, jpeg, "DeviceGray", "DCTDecode"), 0, 0, pdf.PAGE_WIDTH, pdf.PAGE_HEIGHT)
        out.append(page)
    questions = [q for q in _SCANNED_QUESTIONS if q.page is None or q.page <= pages]
    return Fixture("scanned", "structures-inspection-scanned.pdf", "scanned", "pdf", pdf.build(out), questions)


# ---------------------------------------------------------------------------
# layout: 24-page brochure, drawn out of reading order
# ---------------------------------------------------------------------------

_LAYOUT_QUESTIONS = [
    Question("layout-silver", "brochure", "paraphrase", "How much does the Silver membership cost each month?",
             ("$61",), ("$39", "$88", "$124"), 5, r"Silver"),
    Question("layout-pool", "brochure", "paraphrase", "What time does the pool close on Saturdays?",
             ("7:45 pm", "7:45pm", "19:45", "7:45 p.m."), ("9:30 pm", "6:15 pm"), 11, r"Saturday"),
    Question("layout-parking", "brochure", "paraphrase",
             "How long can I park for free if I get my ticket validated?",
             ("3 hours", "three hours"), ("1 hour",), 17, r"validat"),
    Question("layout-studio", "brochure", "literal", "What is the hourly rate to hire Studio B?",
             ("$27",), ("$19", "$42"), 20, r"Studio B"),
    Question("layout-lost", "brochure", "paraphrase", "How long do you keep things people leave behind?",
             ("45 days", "forty-five days"), (), 23, r"Lost property"),
]


def _table(page: pdf.Page, x: float, y: float, headers: Sequence[str], rows: Sequence[Sequence[str]],
           widths: Sequence[float]) -> float:
    """A ruled table, its cells emitted **column by column**: the reading order
    is row by row, so a text layer lists every label before any value."""
    row_h = 18.0
    total = sum(widths)
    for r in range(len(rows) + 2):
        page.rule(x, y - r * row_h + 13, x + total, y - r * row_h + 13, 0.4)
    for c, header in enumerate(headers):
        cx = x + sum(widths[:c]) + 4
        page.text(cx, y, header, 9, bold=True)
        for r, row in enumerate(rows):
            page.text(cx, y - (r + 1) * row_h, row[c], 9)
    return y - (len(rows) + 1) * row_h - 18


def build_brochure(seed: int, pages: int = 24) -> Fixture:
    rng = random.Random(seed * 1009 + 3)
    special: Dict[int, object] = {
        5: ("Membership plans", ["Plan", "Monthly fee", "Guest passes"],
            [["Bronze", "$39", "None"], ["Silver", "$61", "Two"], ["Gold", "$88", "Four"],
             ["Platinum", "$124", "Unlimited"]], [80, 80, 80]),
        11: ("Pool hours", ["Day", "Opens", "Closes"],
             [["Weekdays", "6:00 am", "9:30 pm"], ["Saturday", "7:00 am", "7:45 pm"],
              ["Sunday", "8:00 am", "6:15 pm"]], [80, 80, 80]),
        20: ("Room hire", ["Room", "Capacity", "Hourly rate"],
             [["Studio A", "Twelve", "$19"], ["Studio B", "Twenty", "$27"], ["Studio C", "Forty", "$42"]],
             [80, 80, 80]),
    }
    sidebar = {
        17: ("Parking", "Lot D is open to visitors. With a validated ticket, the first 3 hours are free. "
                        "Without validation, the first 1 hour is free."),
        23: ("Lost property", "Items handed in at reception are logged daily. Lost property is held for "
                              "45 days before it is donated."),
    }
    out: List[pdf.Page] = []
    for number in range(1, pages + 1):
        page = pdf.Page()
        # Right column first, then the left: the page reads left to right.
        right_y = 700.0
        if number in sidebar:
            heading, text = sidebar[number]
            # The sidebar's text is drawn before its heading, bottom half first.
            lines = pdf.wrap(text, 10, 240)
            half = len(lines) // 2
            for i, line in list(enumerate(lines))[half:] + list(enumerate(lines))[:half]:
                page.text(330, right_y - 20 - i * 13, line, 10)
            page.text(330, right_y, heading, 12, bold=True)
            right_y -= 40 + len(lines) * 13
        page.image(pdf.Image(300, 240, _noise_rgb(rng, 300, 240)), 330, 80, 230, 184)
        for _ in range(2):
            right_y = page.paragraph(330, right_y, _paragraph(rng, 2), 240) - 8
        left_y = 700.0
        if number in special:
            title, headers, rows, widths = special[number]  # type: ignore[misc]
            page.text(50, left_y, title, 12, bold=True)
            left_y = _table(page, 50, left_y - 24, headers, rows, widths)
        for _ in range(4):
            if left_y < 120:
                break
            left_y = page.paragraph(50, left_y, _paragraph(rng, 3), 240) - 8
        page.text(50, 760, "Harrow Valley Leisure Centre - Members' Guide", 14, bold=True)
        page.text(530, 40, str(number), 9)
        out.append(page)
    questions = [q for q in _LAYOUT_QUESTIONS if q.page is None or q.page <= pages]
    return Fixture("brochure", "leisure-centre-guide.pdf", "layout", "pdf", pdf.build(out), questions)


# ---------------------------------------------------------------------------
# docx: ~15 MB handbook
# ---------------------------------------------------------------------------

_DOCX_FACTS = {
    120: "The quiet study floor closes at 10:30 pm on weeknights.",
    310: "Each member may borrow up to 17 items at any one time.",
    480: "Overdue fines are waived for patrons aged 12 and under.",
    655: "Bookings for the makerspace laser cutter are managed by Tamsin Ollerenshaw.",
    790: "Interlibrary loan requests take on average 6 working days to fulfil.",
    260: "Members of the visiting scholars programme may borrow up to 40 items at once.",
}

_DOCX_QUESTIONS = [
    Question("docx-quiet", "handbook", "paraphrase", "Until what time can I use the quiet floor on a Tuesday?",
             ("10:30",), (), None, r"quiet study floor"),
    Question("docx-borrow", "handbook", "paraphrase",
             "What's the maximum number of things I can have checked out at the same time?",
             ("17",), ("40",), None, r"up to 17 items"),
    Question("docx-fines", "handbook", "literal", "Up to what age are overdue fines waived?",
             ("12",), (), None, r"overdue fines"),
    Question("docx-laser", "handbook", "paraphrase", "Who do I talk to about reserving the laser cutter?",
             ("Ollerenshaw",), (), None, r"laser cutter"),
    Question("docx-ill", "handbook", "paraphrase", "How long does it usually take to get a book from another library?",
             ("6 working days", "6 days", "six working days"), (), None, r"Interlibrary loan"),
]

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _png(rng: random.Random, width: int, height: int) -> bytes:
    """A valid, barely-compressed PNG (deflate level 0), so its size is predictable."""
    raw = bytearray()
    rgb = _noise_rgb(rng, width, height)
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), 0)) + chunk(b"IEND", b"")


_DRAWING = (
    '<w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
    '<wp:extent cx="5486400" cy="4114800"/><wp:docPr id="1" name="Floor plan"/>'
    '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
    '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
    '<pic:nvPicPr><pic:cNvPr id="1" name="floorplan.png"/><pic:cNvPicPr/></pic:nvPicPr>'
    '<pic:blipFill><a:blip r:embed="rIdImg1"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
    '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="5486400" cy="4114800"/></a:xfrm>'
    '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
    '</wp:inline></w:drawing></w:r></w:p>'
)


def build_handbook(seed: int, paragraphs: int = 900, image_side: int = 2240) -> Fixture:
    rng = random.Random(seed * 1009 + 4)
    body: List[str] = []
    for i in range(paragraphs):
        if i % 60 == 0:
            body.append(f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Section {i // 60 + 1}: '
                        f"{_xml_escape(rng.choice(_OBJECTS).capitalize())}</w:t></w:r></w:p>")
        text = _paragraph(rng, rng.randint(2, 4))
        if i in _DOCX_FACTS:
            text = f"{text} {_DOCX_FACTS[i]}"
        body.append(f'<w:p><w:r><w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r></w:p>')
        if i == 20:
            body.append(_DRAWING)
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_W} '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        f"<w:body>{''.join(body)}</w:body></w:document>"
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>'
        ),
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdImg1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/image" Target="media/floorplan.png"/></Relationships>'
        ),
        "word/document.xml": document,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            package.writestr(info, content.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        info = zipfile.ZipInfo("word/media/floorplan.png", date_time=(2026, 1, 1, 0, 0, 0))
        package.writestr(info, _png(rng, image_side, image_side), compress_type=zipfile.ZIP_STORED)
    questions = list(_DOCX_QUESTIONS)
    return Fixture("handbook", "member-handbook.docx", "docx", "docx", buffer.getvalue(), questions)


# ---------------------------------------------------------------------------

BUILDERS = {
    "manual": build_manual,
    "scanned": build_scanned,
    "brochure": build_brochure,
    "handbook": build_handbook,
}


def build_all(seed: int, names: Sequence[str] = tuple(BUILDERS)) -> List[Fixture]:
    return [BUILDERS[name](seed) for name in names]
