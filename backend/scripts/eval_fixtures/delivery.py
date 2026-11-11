"""The ``attachment_delivery`` family's fixtures: one generator, every type, sized in tokens.

Unified-attachment-delivery spec §8.4 asks the same planted-fact questions of
documents that differ in type and in size, so a delivery mode's effect can be
told apart from the document's. Every fixture here comes from one model of a
document, a list of sections of filler prose with planted facts at known
depths, rendered to each format. Its size is set in tokens (``SIZES``,
estimated at ``CHARS_PER_TOKEN`` characters each, the estimator
``apis.shared.files.document_tokens`` uses), not bytes, because tokens are
what the delivery decision will be made in.

Everything is synthetic (eval-harness spec D6): invented water-utility sites,
codes and numbers. No real person, organisation or identifier.

Each document plants three facts, early, middle and late (``DEPTHS``), each
beside a lookalike of the same shape in a different section (the backup
permit beside the operating one), so a case can forbid the value a misreading
produces. It also names one facility whose asked-for value is absent, with
only its lookalike present, for the absent-fact question. Fact codes are
``XX-ddddd``; the filler never writes a dashed code, so a planted value
cannot also occur by accident.
"""

from __future__ import annotations

import html
import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence, Tuple

from large_document_quality import pdf

from eval_fixtures import images, ooxml

CHARS_PER_TOKEN = 4

#: The §8.4 size ladder, in estimated tokens.
SIZES: Dict[str, int] = {"5k": 5_000, "30k": 30_000, "80k": 80_000, "150k": 150_000, "300k": 300_000}

#: Where each planted fact sits, as a fraction of the document.
DEPTHS: Dict[str, float] = {"early": 0.12, "middle": 0.52, "late": 0.9}

#: What each depth's fact is: the attribute asked for, its code prefix, and
#: the lookalike attribute planted beside it with a value one swap away.
_KINDS: Dict[str, Tuple[str, str, str]] = {
    "early": ("operating permit number", "WP", "backup permit number"),
    "middle": ("calibration certificate number", "CC", "expired calibration certificate number"),
    "late": ("emergency work order number", "WO", "cancelled emergency work order number"),
}

_FIRST = ("Kestrel", "Orrin", "Calder", "Wren", "Tamsin", "Brack", "Ilsey", "Corran",
          "Hale", "Morrow", "Pell", "Quill", "Saddle", "Thorn", "Vane", "Ashby")
_SECOND = ("Ford", "Reach", "Basin", "Hollow", "Gap", "Mere", "Weir", "Flats",
           "Bend", "Crossing", "Shoals", "Tarn", "Levee", "Cut")
_FACILITIES = ("pump station", "intake", "treatment plant", "sluice gate", "reservoir gauge")
#: Filler sites share no word with a fact site, so filler never names a subject.
_FILLER_SITES = tuple(f"{a} {b}" for a in ("North", "South", "East", "West", "Upper", "Lower", "Middle")
                      for b in ("Yard", "Depot", "Annex", "Works", "Field", "Spur"))
_FEATURES = ("spillway", "outfall", "settling pond", "valve chamber", "access road", "fish ladder", "weir crest")
_ADJECTIVES = ("ordinary", "settled", "dry", "calm", "unremarkable", "steady", "clear")
_WEATHER = ("overcast", "dry and still", "wet", "windy", "cold and clear", "mild")
_RECORDS = ("site handbook", "shift roster", "lockout procedure", "sampling plan", "visitor log")

_FILLER = (
    "Crews at {site} completed {n} routine inspections during week {w}, and the log notes {adj} conditions "
    "along the {feature}.",
    "Flow past the {site} {feature} averaged {n} litres per second over the period, inside the seasonal band.",
    "The {site} team replaced {n} worn gaskets and recorded the work in the maintenance ledger.",
    "Sediment sampling at {site} found {adj} turbidity, and no follow-up visit was scheduled.",
    "A scheduled walk of the {feature} at {site} took {n} minutes and raised no actions.",
    "Staff at {site} reviewed the {record} and confirmed it matched the posted procedure.",
    "Weather on the {site} reach was {weather} for most of week {w}, and work went ahead as planned.",
    "Vegetation was cut back along {n} metres of the {feature} at {site} before the spring survey.",
    "The {site} duty officer signed off {n} permit-to-work forms without exceptions.",
)


@dataclass(frozen=True)
class Fact:
    """One planted fact and its lookalike."""

    depth: str
    subject: str
    attribute: str
    value: str
    lookalike_attribute: str
    lookalike: str

    @property
    def sentence(self) -> str:
        return f"For the record, the {self.attribute} for {self.subject} is {self.value}."

    @property
    def lookalike_sentence(self) -> str:
        return f"The {self.lookalike_attribute} for {self.subject} is {self.lookalike}, kept for audit only."

    def question(self, where: str = "the attached document") -> str:
        return f"According to {where}, what is the {self.attribute} for {self.subject}? Reply with just the number."


@dataclass(frozen=True)
class Absent:
    """A facility whose asked-for value the document does not give; only its lookalike is there."""

    subject: str
    attribute: str
    lookalike_attribute: str
    lookalike: str

    @property
    def lookalike_sentence(self) -> str:
        return f"The {self.lookalike_attribute} for {self.subject} is {self.lookalike}, kept for audit only."

    def question(self) -> str:
        return (
            f"Does the attached document give the {self.attribute} for {self.subject}? If it does, reply with "
            "just the number. If the document doesn't say, reply exactly: NOT IN DOCUMENT"
        )


@dataclass(frozen=True)
class Document:
    """Sections of prose with the planted facts in place; every format renders this."""

    title: str
    sections: Tuple[Tuple[str, Tuple[str, ...]], ...]
    facts: Dict[str, Fact]
    absent: Absent

    @property
    def text(self) -> str:
        return "\n\n".join(
            [self.title] + [f"{heading}\n\n" + "\n\n".join(paragraphs) for heading, paragraphs in self.sections]
        )


def _sites() -> List[str]:
    names = [f"{a} {b}" for a in _FIRST for b in _SECOND]
    random.Random("attachment-delivery-sites").shuffle(names)
    return names


_SITE_POOL = _sites()
#: Each named document's place in the site pool, so no two documents share a
#: subject and a cross-document question can only be answered from both.
_SLOTS: Dict[str, int] = {}


_SITES_PER_SLOT = 4


def _subjects(name: str, count: int) -> List[str]:
    slot = _SLOTS.setdefault(name, len(_SLOTS))
    rng = random.Random(f"facility:{name}")
    return [f"the {_SITE_POOL[slot * _SITES_PER_SLOT + i]} {rng.choice(_FACILITIES)}" for i in range(count)]


def _code(rng: random.Random, prefix: str) -> Tuple[str, str]:
    """A ``PREFIX-ddddd`` code and its lookalike with the last two digits swapped (never equal)."""
    while True:
        digits = f"{rng.randint(10000, 99999)}"
        if digits[-1] != digits[-2]:
            return f"{prefix}-{digits}", f"{prefix}-{digits[:-2]}{digits[-1]}{digits[-2]}"


def _filler_paragraph(rng: random.Random) -> str:
    sentences = []
    for _ in range(rng.randint(4, 6)):
        sentences.append(rng.choice(_FILLER).format(
            site=rng.choice(_FILLER_SITES), feature=rng.choice(_FEATURES), adj=rng.choice(_ADJECTIVES),
            weather=rng.choice(_WEATHER), record=rng.choice(_RECORDS),
            n=rng.randint(2, 480), w=rng.randint(1, 52),
        ))
    return " ".join(sentences)


def document(name: str, tokens: int, *, title: str) -> Document:
    """The document called ``name``, about ``tokens`` long, with its facts planted. Deterministic."""
    rng = random.Random(f"document:{name}")
    subjects = _subjects(name, 4)
    facts: Dict[str, Fact] = {}
    for (depth, (attribute, prefix, lookalike_attribute)), subject in zip(_KINDS.items(), subjects):
        value, lookalike = _code(rng, prefix)
        facts[depth] = Fact(depth, subject, attribute, value, lookalike_attribute, lookalike)
    absent_value, _ = _code(rng, "WP")
    absent = Absent(subjects[3], _KINDS["early"][0], _KINDS["early"][2], absent_value)

    budget = tokens * CHARS_PER_TOKEN - len(title)
    paragraphs: List[str] = []
    used = 0
    while used < budget:
        paragraph = _filler_paragraph(rng)
        paragraphs.append(paragraph)
        used += len(paragraph) + 2
    n = len(paragraphs)

    def plant(fraction: float, sentence: str) -> None:
        index = min(n - 1, max(0, int(round(fraction * (n - 1)))))
        paragraphs[index] = f"{paragraphs[index]} {sentence}"

    for depth, fact in facts.items():
        plant(DEPTHS[depth], fact.sentence)
        # The lookalike sits well away from its fact, in another section.
        plant((DEPTHS[depth] + 0.3) % 1.0, fact.lookalike_sentence)
    plant(0.7, absent.lookalike_sentence)

    per_section = 8
    sections = tuple(
        (f"Section {i // per_section + 1}: operations notes", tuple(paragraphs[i:i + per_section]))
        for i in range(0, n, per_section)
    )
    return Document(title=title, sections=sections, facts=facts, absent=absent)


# --------------------------------------------------------------------------- renderers


def as_markdown(doc: Document) -> bytes:
    parts = [f"# {doc.title}"]
    for heading, paragraphs in doc.sections:
        parts.append(f"## {heading}")
        parts.extend(paragraphs)
    return ("\n\n".join(parts) + "\n").encode("utf-8")


def as_text(doc: Document) -> bytes:
    parts = [doc.title.upper()]
    for heading, paragraphs in doc.sections:
        parts.append(heading.upper())
        parts.extend(paragraphs)
    return ("\n\n".join(parts) + "\n").encode("utf-8")


def as_html(doc: Document) -> bytes:
    body = [f"<h1>{html.escape(doc.title)}</h1>"]
    for heading, paragraphs in doc.sections:
        body.append(f"<h2>{html.escape(heading)}</h2>")
        body.extend(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    return (
        f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{html.escape(doc.title)}</title></head>"
        f"<body>{''.join(body)}</body></html>"
    ).encode("utf-8")


def as_docx(doc: Document) -> bytes:
    blocks: List[str] = [doc.title]
    for heading, paragraphs in doc.sections:
        blocks.append(heading)
        blocks.extend(paragraphs)
    return ooxml.docx(blocks)


def as_pdf(doc: Document) -> bytes:
    """A text PDF, paginated: about 1,150 estimated tokens a page."""
    pages: List[pdf.Page] = []
    page = pdf.Page()
    y = 720.0

    def room(lines: int) -> None:
        nonlocal page, y
        if y - lines * 13 < 60:
            pages.append(page)
            page, y = pdf.Page(), 740.0

    page.text(72, y, doc.title, size=16, bold=True)
    y -= 30
    for heading, paragraphs in doc.sections:
        room(3)
        page.text(72, y, heading, size=12, bold=True)
        y -= 20
        for paragraph in paragraphs:
            for line in pdf.wrap(paragraph, 10, 468):
                room(1)
                page.text(72, y, line, size=10)
                y -= 13
            y -= 8
    pages.append(page)
    return pdf.build(pages)


def as_slides(doc: Document) -> bytes:
    """A deck: one slide per paragraph, its sentences as bullets."""
    slides = [ooxml.Slide(doc.title, ["Operations notes"])]
    for heading, paragraphs in doc.sections:
        for i, paragraph in enumerate(paragraphs, start=1):
            bullets = [s.strip().rstrip(".") + "." for s in paragraph.split(". ") if s.strip()]
            slides.append(ooxml.Slide(f"{heading}, part {i}", bullets))
    return ooxml.pptx(slides)


# --------------------------------------------------------------------------- tables


@dataclass(frozen=True)
class TableFact:
    """A planted cell: ``site``'s ticket in ``week``, beside the next week's lookalike ticket."""

    site: str
    week: int
    value: str
    lookalike: str

    def question(self) -> str:
        return (
            f"In the attached spreadsheet, what is the Ticket for {self.site} in week {self.week}? "
            "Reply with just the ticket."
        )


TABLE_COLUMNS = ("Site", "Week", "Inspections", "Flow (L/s)", "Ticket")


def table(name: str, tokens: int) -> Tuple[List[Tuple[str, int, int, int, str]], TableFact]:
    """Rows of site readings about ``tokens`` long as CSV, with one planted ticket. Deterministic."""
    rng = random.Random(f"table:{name}")
    site = _subjects(name, 1)[0].removeprefix("the ")
    value, lookalike = _code(rng, "WO")
    week = rng.randint(10, 40)
    rows: List[Tuple[str, int, int, int, str]] = []
    used, budget = 0, tokens * CHARS_PER_TOKEN
    while used < budget:
        row = (rng.choice(_FILLER_SITES), rng.randint(1, 52), rng.randint(1, 40), rng.randint(100, 999),
               f"T{rng.randint(10000, 99999)}")
        rows.append(row)
        used += sum(len(str(c)) for c in row) + 5
    for fraction, (wk, ticket) in ((0.62, (week, value)), (0.63, (week + 1, lookalike))):
        rows.insert(int(len(rows) * fraction), (site, wk, rng.randint(1, 40), rng.randint(100, 999), ticket))
    return rows, TableFact(site, week, value, lookalike)


def as_csv(rows: Sequence[Sequence[object]]) -> bytes:
    lines = [",".join(TABLE_COLUMNS)] + [",".join(str(c) for c in row) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def as_xlsx(rows: Sequence[Sequence[object]]) -> bytes:
    from openpyxl import Workbook

    from eval_fixtures.build import stable_xlsx

    book = Workbook()
    sheet = book.active
    sheet.title = "Readings"
    sheet.append(list(TABLE_COLUMNS))
    for row in rows:
        sheet.append(list(row))
    return stable_xlsx(book)


# --------------------------------------------------------------------------- the family's files

#: ``filename -> (document name, tokens, title, renderer)`` for every prose fixture.
_PROSE: Dict[str, Tuple[str, int, str, Callable[[Document], bytes]]] = {
    **{f"delivery_prose_{size}.md": (f"prose_{size}", tokens, f"Valley water operations report ({size})", as_markdown)
       for size, tokens in SIZES.items()},
    "delivery_log_30k.txt": ("log_30k", SIZES["30k"], "Field station operations log", as_text),
    "delivery_report_30k.pdf": ("report_30k", SIZES["30k"], "Quarterly operations report", as_pdf),
    "delivery_report_80k.pdf": ("report_80k", SIZES["80k"], "Annual operations report", as_pdf),
    "delivery_report_150k.pdf": ("report_150k", SIZES["150k"], "Five-year operations report", as_pdf),
    "delivery_memo_30k.docx": ("memo_30k", SIZES["30k"], "Operations memo", as_docx),
    "delivery_bulletin_30k.html": ("bulletin_30k", SIZES["30k"], "Operations bulletin", as_html),
    "delivery_briefing_30k.pptx": ("briefing_30k", SIZES["30k"], "Operations briefing", as_slides),
    **{f"delivery_note_{letter}_5k.md": (f"note_{letter}", SIZES["5k"], f"Site note {letter.upper()}", as_markdown)
       for letter in "abcd"},
}
_TABLES: Dict[str, Tuple[str, int, Callable[[Sequence[Sequence[object]]], bytes]]] = {
    "delivery_ledger_30k.csv": ("ledger_csv_30k", SIZES["30k"], as_csv),
    "delivery_ledger_30k.xlsx": ("ledger_xlsx_30k", SIZES["30k"], as_xlsx),
}

_DOCUMENTS: Dict[str, Document] = {}


def prose(filename: str) -> Document:
    """The document model behind prose fixture ``filename`` (its facts are what its cases ask)."""
    if filename not in _DOCUMENTS:
        name, tokens, title, _ = _PROSE[filename]
        _DOCUMENTS[filename] = document(name, tokens, title=title)
    return _DOCUMENTS[filename]


def table_fact(filename: str) -> TableFact:
    name, tokens, _ = _TABLES[filename]
    return table(name, tokens)[1]


#: The permit card image: a fact that is only in pixels.
#: "Wick Tor" shares no word with the site pool, so no prose fixture names it.
PERMIT_CARD_SUBJECT = "the Wick Tor pump station"
PERMIT_CARD_VALUE = "WP-60418"
PERMIT_CARD_LOOKALIKE = "WP-60481"


def permit_card() -> bytes:
    return images.dialog("Site permit card", [
        f"Operating permit number: {PERMIT_CARD_VALUE}",
        f"Backup permit number: {PERMIT_CARD_LOOKALIKE}",
        "Facility: Wick Tor pump station",
    ])


def _prose_builder(filename: str) -> Callable[[], bytes]:
    return lambda: _PROSE[filename][3](prose(filename))


def _table_builder(filename: str) -> Callable[[], bytes]:
    def build() -> bytes:
        name, tokens, render = _TABLES[filename]
        return render(table(name, tokens)[0])

    return build


# Allocate site slots in a fixed order, so a subject never depends on which
# fixture a process happened to build first.
for _name in list(_PROSE) + list(_TABLES):
    _SLOTS.setdefault(_PROSE[_name][0] if _name in _PROSE else _TABLES[_name][0], len(_SLOTS))

# --------------------------------------------------------------------------- whole-document questions


@dataclass(frozen=True)
class WholeFacts:
    """What a whole-document question needs: facts spread over the whole document.

    - ``notices``: every site that got a boil-water notice (list and count),
      beside one ``decoy`` site where a notice was considered and not issued;
    - ``gauge``: one reservoir gauge read at the spring survey early in the
      document and at the autumn survey late in it (compare two sections);
    - ``incidents``: the major incidents, one per quarter of the document
      (summarise).

    The filler never writes "boil-water", "gauge level" or "Major incident",
    so nothing else answers these.
    """

    notices: Tuple[str, ...]
    decoy: str
    gauge: str
    spring_level: str
    autumn_level: str
    change: str
    incidents: Tuple[Tuple[str, str], ...]


_INCIDENTS = (
    "a main burst flooded the access road",
    "a chlorine dosing pump failed overnight",
    "a lightning strike tripped the intake screens",
    "a contractor breached a raw-water main",
)
#: Where each planted item sits, as a fraction of the document.
_NOTICE_DEPTHS = (0.05, 0.22, 0.4, 0.58, 0.76, 0.95)
_INCIDENT_DEPTHS = (0.08, 0.35, 0.63, 0.91)


def _site_names(name: str, count: int) -> List[str]:
    """``count`` site names no other document uses (one slot per four)."""
    sites: List[str] = []
    part = 0
    while len(sites) < count:
        slot = _SLOTS.setdefault(f"{name}#{part}", len(_SLOTS))
        sites.extend(_SITE_POOL[slot * _SITES_PER_SLOT + i] for i in range(_SITES_PER_SLOT))
        part += 1
    return sites[:count]


def whole_document(name: str, tokens: int, *, title: str) -> Tuple[Document, WholeFacts]:
    """A document about ``tokens`` long with the whole-document facts planted. Deterministic."""
    rng = random.Random(f"whole:{name}")
    sites = _site_names(name, len(_NOTICE_DEPTHS) + 1 + 1 + len(_INCIDENTS))
    notices = tuple(sites[: len(_NOTICE_DEPTHS)])
    decoy = sites[len(_NOTICE_DEPTHS)]
    gauge = sites[len(_NOTICE_DEPTHS) + 1]
    incident_sites = sites[len(_NOTICE_DEPTHS) + 2:]
    spring = rng.randint(380, 460) / 10
    fall = rng.randint(12, 48) / 10
    autumn = round(spring - fall, 1)

    budget = tokens * CHARS_PER_TOKEN - len(title)
    paragraphs: List[str] = []
    used = 0
    while used < budget:
        paragraph = _filler_paragraph(rng)
        paragraphs.append(paragraph)
        used += len(paragraph) + 2
    n = len(paragraphs)

    def plant(fraction: float, sentence: str) -> None:
        index = min(n - 1, max(0, int(round(fraction * (n - 1)))))
        paragraphs[index] = f"{paragraphs[index]} {sentence}"

    for site, depth in zip(notices, _NOTICE_DEPTHS):
        plant(depth, f"A boil-water notice was issued for customers served from {site} and lifted two days later.")
    plant(0.5, f"A boil-water notice was considered for {decoy}, but sampling came back clear and none was issued.")
    plant(0.1, f"At the spring survey, the {gauge} reservoir gauge level read {spring:.1f} metres.")
    plant(0.88, f"At the autumn survey, the {gauge} reservoir gauge level read {autumn:.1f} metres.")
    incidents = tuple(zip(incident_sites, _INCIDENTS))
    for (site, what), depth in zip(incidents, _INCIDENT_DEPTHS):
        plant(depth, f"Major incident: at {site}, {what}; crews restored service the same day.")

    per_section = 8
    sections = tuple(
        (f"Section {i // per_section + 1}: operations notes", tuple(paragraphs[i:i + per_section]))
        for i in range(0, n, per_section)
    )
    doc = Document(title=title, sections=sections, facts={}, absent=Absent("", "", "", ""))
    return doc, WholeFacts(
        notices=notices, decoy=decoy, gauge=gauge, spring_level=f"{spring:.1f}",
        autumn_level=f"{autumn:.1f}", change=f"{fall:.1f}", incidents=incidents,
    )


#: The whole-document PDFs, by size label. Their site slots come after every
#: other fixture's, so adding them moved no existing subject.
_WHOLE: Dict[str, Tuple[str, int, str]] = {
    f"delivery_yearbook_{size}.pdf": (f"yearbook_{size}", SIZES[size], f"Operations yearbook ({size})")
    for size in ("30k", "80k", "150k")
}
_WHOLE_DOCUMENTS: Dict[str, Tuple[Document, WholeFacts]] = {}


def whole(filename: str) -> Tuple[Document, WholeFacts]:
    if filename not in _WHOLE_DOCUMENTS:
        name, tokens, title = _WHOLE[filename]
        _WHOLE_DOCUMENTS[filename] = whole_document(name, tokens, title=title)
    return _WHOLE_DOCUMENTS[filename]


for _name in _WHOLE:
    whole(_name)

#: The twenty-document count cell's other fifteen site notes (phase 4b's 20
#: files a message). Their slots come after every other fixture's, so adding
#: them moved no existing document's subjects.
COUNT_NOTE_LETTERS = "efghijklmnopqrs"
for _letter in COUNT_NOTE_LETTERS:
    _note = f"delivery_note_{_letter}_5k.md"
    _PROSE[_note] = (f"note_{_letter}", SIZES["5k"], f"Site note {_letter.upper()}", as_markdown)
    _SLOTS.setdefault(_PROSE[_note][0], len(_SLOTS))

#: Phase 5's overflow cells (unified-attachment-delivery §4.3, ``search``): a
#: conversation whose on-demand set grows across turns. Eight ~30k-token site
#: dossiers, each over Haiku 5.5's 25k document budget on its own, attached two
#: a turn. Their slots come after every other fixture's, so adding them moved
#: no existing document's subjects.
OVERFLOW_LETTERS = "abcdefgh"
for _letter in OVERFLOW_LETTERS:
    _dossier = f"delivery_dossier_{_letter}_30k.md"
    _PROSE[_dossier] = (f"dossier_{_letter}", SIZES["30k"], f"Site dossier {_letter.upper()}", as_markdown)
    _SLOTS.setdefault(_PROSE[_dossier][0], len(_SLOTS))


def dossier(letter: str) -> str:
    """The overflow cells' fixture file for ``letter``."""
    return f"delivery_dossier_{letter}_30k.md"


GENERATED: Dict[str, Callable[[], bytes]] = {
    **{name: _prose_builder(name) for name in _PROSE},
    **{name: _table_builder(name) for name in _TABLES},
    "delivery_permit_card.png": permit_card,
    **{name: (lambda n=name: as_pdf(whole(n)[0])) for name in _WHOLE},
}


def estimated_tokens(filename: str) -> int:
    """The estimated tokens fixture ``filename`` brings, for a run's spend estimate; 0 if not sized here."""
    if filename in _PROSE:
        return len(prose(filename).text) // CHARS_PER_TOKEN
    if filename in _TABLES:
        return _TABLES[filename][1]
    if filename == "delivery_permit_card.png":
        return 1_500
    if filename in _WHOLE:
        return len(whole(filename)[0].text) // CHARS_PER_TOKEN
    return 0
