"""Whole-paragraph insertions in the redline engine (docs/specs/word-tracked-changes.md §3a).

The invariants: **Reject All gives back the original paragraphs, text and
formatting, with no empty paragraph left where an insertion was; Accept All
gives the new paragraphs with the formatting of the paragraph they copy**,
including its numbering; and nothing outside the anchor paragraphs changes.

``fixtures/contract-numbered.docx`` is a real package written by LibreOffice
26.2 from a flat ODT: headings numbered through the ``Heading1`` style's own
``w:numPr``, a lettered list numbered by direct ``w:numPr``, a bold lead-in
run, and a two-by-two table. The views below apply paragraph-mark revisions the
way Word does: a paragraph whose mark is inserted joins the next paragraph when
the insertion is rejected.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple
from xml.etree import ElementTree

import pytest

from agents.builtin_tools.office.docx_redline import Change, NewParagraph, RedlineError, redline
from apis.shared.kb_backend.docx_revisions import annotated_text
from tests.shared.docx_fixtures import W_NS, build_docx, paragraph, part, run

W = f"{{{W_NS}}}"
NOW = datetime(2026, 11, 7, 15, 30, tzinfo=timezone.utc)
FIXTURE = (Path(__file__).parent / "fixtures" / "contract-numbered.docx").read_bytes()

#: (text, pStyle, numId) per paragraph, in document order.
Row = Tuple[str, Optional[str], Optional[str]]


def _redline(docx: bytes, *changes: Change) -> bytes:
    return redline(docx, list(changes), "Contract Assistant", now=NOW).docx


def _mark(p: ElementTree.Element, tag: str) -> bool:
    rpr = p.find(f"{W}pPr/{W}rPr")
    return rpr is not None and rpr.find(W + tag) is not None


def _text(node: ElementTree.Element, drop: str) -> str:
    out = []
    for child in node:
        if child.tag == W + drop or child.tag == W + "p":
            continue
        if child.tag in (W + "t", W + "delText"):
            out.append(child.text or "")
        else:
            out.append(_text(child, drop))
    return "".join(out)


def _rows(docx: bytes, accept: bool) -> List[Row]:
    """Each paragraph after Accept All or Reject All, paragraph marks included."""
    root = ElementTree.fromstring(part(docx))
    drop = "del" if accept else "ins"
    merging = "del" if accept else "ins"
    rows: List[Row] = []
    carry = ""
    containers = [root.find(W + "body")] + list(root.iter(W + "tc"))
    order = {id(p): i for i, p in enumerate(root.iter(W + "p"))}
    paragraphs = []
    for container in containers:
        paragraphs.extend(child for child in container if child.tag == W + "p")
    for p in sorted(paragraphs, key=lambda el: order[id(el)]):
        text = carry + _text(p, drop)
        if _mark(p, merging):
            carry = text
            continue
        carry = ""
        style = p.find(f"{W}pPr/{W}pStyle")
        num = p.find(f"{W}pPr/{W}numPr/{W}numId")
        rows.append((text, style.get(W + "val") if style is not None else None, num.get(W + "val") if num is not None else None))
    assert carry == "", "an inserted paragraph mark has no paragraph to join"
    return rows


def accepted(docx: bytes) -> List[Row]:
    return _rows(docx, accept=True)


def rejected(docx: bytes) -> List[Row]:
    return _rows(docx, accept=False)


def _texts(rows: List[Row]) -> List[str]:
    return [text for text, _, _ in rows]


def _paragraph_with(docx: bytes, wording: str) -> ElementTree.Element:
    root = ElementTree.fromstring(part(docx))
    hits = [p for p in root.iter(W + "p") if wording in _text(p, "del")]
    assert len(hits) == 1, wording
    return hits[0]


def _unit_sources(docx: bytes) -> List[str]:
    xml = part(docx)
    return re.findall(r"<w:p>.*?</w:p>|<w:p .*?</w:p>", xml)


# ── New sections and list items, on the real fixture ─────────────────────────


class TestNewSection:
    CHANGE = Change(
        "indirect, incidental or consequential damages",
        insert="after",
        paragraphs=(
            NewParagraph("Survival", like="Limitation of Liability"),
            NewParagraph("Sections 3 and 4 survive termination of this Agreement."),
        ),
    )

    def test_reject_all_gives_back_the_original_text_and_formatting(self):
        assert rejected(_redline(FIXTURE, self.CHANGE)) == rejected(FIXTURE) == accepted(FIXTURE)

    def test_accept_all_adds_a_heading_numbered_by_its_style_and_a_body_paragraph(self):
        rows = accepted(_redline(FIXTURE, self.CHANGE))
        at = _texts(rows).index("Neither party shall be liable for indirect, incidental or consequential damages.")
        assert rows[at + 1] == ("Survival", "Heading1", None)
        assert rows[at + 2] == ("Sections 3 and 4 survive termination of this Agreement.", "Normal", None)
        assert rows[at + 3][0] == "Entire Agreement"

    def test_the_new_paragraphs_copy_the_whole_paragraph_properties_of_their_source(self):
        out = _redline(FIXTURE, self.CHANGE)
        heading = _paragraph_with(out, "Survival")
        source = _paragraph_with(FIXTURE, "Limitation of Liability")

        def props(p):
            ppr = p.find(W + "pPr")
            return [ElementTree.tostring(c) for c in ppr if c.tag != W + "rPr"]

        assert props(heading) == props(source)

    def test_each_new_paragraph_is_a_tracked_insertion_mark_and_runs(self):
        out = _redline(FIXTURE, self.CHANGE)
        for wording in ("Survival", "Sections 3 and 4 survive"):
            p = _paragraph_with(out, wording)
            assert _mark(p, "ins")
            runs = [c for c in p if c.tag == W + "r"]
            assert runs == []  # every run sits inside w:ins
            ins = p.find(W + "ins")
            assert ins.get(W + "author") == "Contract Assistant"
            assert ins.get(W + "date") == "2026-11-07T15:30:00Z"

    def test_the_tracked_changes_reader_shows_the_insertion(self):
        text = annotated_text(_redline(FIXTURE, self.CHANGE))
        assert "[inserted: Survival]" in text
        assert "[inserted: Sections 3 and 4 survive termination of this Agreement.]" in text

    def test_untouched_paragraphs_keep_their_bytes(self):
        before = _unit_sources(FIXTURE)
        after = _unit_sources(_redline(FIXTURE, self.CHANGE))
        anchor = next(i for i, s in enumerate(before) if "consequential damages" in s)
        assert after[:anchor] == before[:anchor]
        assert after[anchor + 3 :] == before[anchor + 1 :]

    def test_the_summary_names_the_anchor(self):
        result = redline(FIXTURE, [self.CHANGE], "Contract Assistant", now=NOW)
        assert result.applied == [
            '1. inserted 2 paragraphs after "Neither party shall be liable for indirect, incidental or c…"'
        ]


class TestListItem:
    def test_a_list_item_like_its_neighbour_takes_the_lists_numbering(self):
        out = _redline(
            FIXTURE,
            Change(
                "has given written notice",
                insert="after",
                paragraphs=(NewParagraph("Contractor has offered University a payment plan; and"),),
            ),
        )
        rows = accepted(out)
        at = _texts(rows).index("Contractor has given written notice of the overdue amount; and")
        assert rows[at][2] == "2"
        assert rows[at + 1] == ("Contractor has offered University a payment plan; and", "Normal", "2")
        assert rows[at + 2][0].startswith("University has not cured")
        assert rejected(out) == accepted(FIXTURE)

    def test_insert_before_the_first_item(self):
        out = _redline(
            FIXTURE,
            Change(
                "more than ninety (90) days past due",
                insert="before",
                paragraphs=(NewParagraph("University has received a correct invoice;"),),
            ),
        )
        rows = accepted(out)
        at = _texts(rows).index("University has received a correct invoice;")
        assert rows[at][2] == "2"
        assert rows[at + 1][0] == "the invoice is more than ninety (90) days past due;"
        assert rejected(out) == accepted(FIXTURE)


class TestRunFormatting:
    def test_a_lead_takes_the_lead_in_format_and_the_text_the_body_format(self):
        out = _redline(
            FIXTURE,
            Change(
                "University shall pay each undisputed invoice",
                insert="after",
                paragraphs=(NewParagraph("Contractor shall send each invoice to Exhibit B.", lead="Invoice Address."),),
            ),
        )
        p = _paragraph_with(out, "Invoice Address.")
        lead, body = p.find(W + "ins").findall(W + "r")
        assert lead.find(f"{W}rPr/{W}b") is not None
        assert lead.find(W + "t").text == "Invoice Address."
        assert body.find(f"{W}rPr/{W}b") is None
        assert body.find(W + "t").text == " Contractor shall send each invoice to Exhibit B."

    def test_without_a_lead_the_text_takes_the_dominant_run_format(self):
        out = _redline(
            FIXTURE,
            Change(
                "University shall pay each undisputed invoice",
                insert="after",
                paragraphs=(NewParagraph("Late invoices are paid in the next cycle."),),
            ),
        )
        (only,) = _paragraph_with(out, "Late invoices").find(W + "ins").findall(W + "r")
        assert only.find(f"{W}rPr/{W}b") is None

    def test_a_lead_ending_in_a_space_is_not_doubled(self):
        docx = build_docx(paragraph(run("Term. ", bold=True), run("This Agreement runs one year.")), paragraph(run("End.")))
        out = _redline(docx, Change("runs one year", insert="after", paragraphs=(NewParagraph("It renews.", lead="Renewal. "),)))
        assert "Renewal. It renews." in _texts(accepted(out))


# ── Where no paragraph follows ───────────────────────────────────────────────


class TestNoFollowingParagraph:
    def test_before_a_table_the_anchor_mark_becomes_the_insertion(self):
        out = _redline(
            FIXTURE,
            Change(
                "supersedes all prior agreements",
                insert="after",
                paragraphs=(
                    NewParagraph("Order of Precedence", like="Limitation of Liability"),
                    NewParagraph("Exhibit A controls over this Agreement."),
                ),
            ),
        )
        anchor = _paragraph_with(out, "supersedes all prior agreements")
        assert _mark(anchor, "ins")
        last = _paragraph_with(out, "Exhibit A controls")
        assert not _mark(last, "ins")
        assert rejected(out) == accepted(FIXTURE)
        rows = accepted(out)
        at = _texts(rows).index("Order of Precedence")
        assert rows[at][1] == "Heading1"
        assert rows[at + 1][0] == "Exhibit A controls over this Agreement."
        assert rows[at + 2][0] == "Milestone"

    def test_the_last_paragraph_of_a_table_cell(self):
        out = _redline(FIXTURE, Change("Kickoff", insert="after", paragraphs=(NewParagraph("Payable on signature."),)))
        assert rejected(out) == accepted(FIXTURE)
        cells = ElementTree.fromstring(part(out)).iter(W + "tc")
        cell = next(tc for tc in cells if any("Kickoff" in (t.text or "") for t in tc.iter(W + "t")))
        assert [_text(p, "del") for p in cell.findall(W + "p")] == ["Kickoff", "Payable on signature."]

    def test_the_last_paragraph_of_the_document(self):
        docx = build_docx(paragraph(run("First.")), paragraph(run("Signature block.")))
        out = _redline(docx, Change("Signature block", insert="after", paragraphs=(NewParagraph("Witness."),)))
        assert _texts(rejected(out)) == ["First.", "Signature block."]
        assert _texts(accepted(out)) == ["First.", "Signature block.", "Witness."]

    def test_the_last_new_paragraph_there_must_keep_the_anchor_format(self):
        with pytest.raises(RedlineError) as err:
            _redline(
                FIXTURE,
                Change(
                    "Kickoff",
                    insert="after",
                    paragraphs=(NewParagraph("Payable on signature.", like="Limitation of Liability"),),
                ),
            )
        assert err.value.problems == [
            'Change 1: "Kickoff" is the last paragraph of its table cell, so the last new paragraph '
            "after it must keep that paragraph's own formatting. Drop `like` on the last new paragraph."
        ]

    def test_a_paragraph_mark_already_revised_cannot_take_another(self):
        marked = (
            '<w:p><w:pPr><w:rPr><w:ins w:id="5" w:author="Sponsor Counsel" w:date="2026-10-01T09:00:00Z"/>'
            '</w:rPr></w:pPr><w:r><w:t>Closing.</w:t></w:r></w:p>'
        )
        with pytest.raises(RedlineError) as err:
            _redline(build_docx(paragraph(run("Opening.")), marked), Change("Closing", insert="after", paragraphs=(NewParagraph("More."),)))
        assert "already a tracked change or a section break" in err.value.problems[0]

    def test_a_formatting_revision_on_the_anchor_blocks_it_too(self):
        changed = (
            '<w:p><w:pPr><w:jc w:val="center"/><w:pPrChange w:id="6" w:author="Sponsor Counsel" '
            'w:date="2026-10-01T09:00:00Z"><w:pPr/></w:pPrChange></w:pPr><w:r><w:t>Closing.</w:t></w:r></w:p>'
        )
        with pytest.raises(RedlineError):
            _redline(build_docx(changed), Change("Closing", insert="after", paragraphs=(NewParagraph("More."),)))
        out = _redline(build_docx(changed, paragraph(run("After."))), Change("Closing", insert="after", paragraphs=(NewParagraph("More."),)))
        assert _texts(accepted(out)) == ["Closing.", "More.", "After."]


# ── Several changes at once ──────────────────────────────────────────────────


class TestCombinations:
    def test_insertions_and_text_edits_apply_together(self):
        out = _redline(
            FIXTURE,
            Change("within thirty (30) days of receipt", replace="within forty-five (45) days of receipt"),
            Change(
                "University shall pay each undisputed invoice",
                insert="after",
                paragraphs=(NewParagraph("Disputed amounts are resolved under Section 4."),),
            ),
            Change("Contractor shall perform the services", insert="before", paragraphs=(NewParagraph("The parties agree as follows."),)),
        )
        texts = _texts(accepted(out))
        assert "Invoices. University shall pay each undisputed invoice within forty-five (45) days of receipt." in texts
        at = texts.index("Invoices. University shall pay each undisputed invoice within forty-five (45) days of receipt.")
        assert texts[at + 1] == "Disputed amounts are resolved under Section 4."
        at = texts.index("The parties agree as follows.")
        assert texts[at + 1].startswith("Contractor shall perform the services")
        assert rejected(out) == accepted(FIXTURE)

    def test_two_insertions_after_one_anchor_keep_request_order(self):
        docx = build_docx(paragraph(run("Anchor.")), paragraph(run("Next.")))
        out = _redline(
            docx,
            Change("Anchor", insert="after", paragraphs=(NewParagraph("One."),)),
            Change("Anchor", insert="after", paragraphs=(NewParagraph("Two."), NewParagraph("Three."))),
        )
        assert _texts(accepted(out)) == ["Anchor.", "One.", "Two.", "Three.", "Next."]
        assert _texts(rejected(out)) == ["Anchor.", "Next."]

    def test_two_insertions_after_the_last_paragraph_share_one_adopted_mark(self):
        docx = build_docx(paragraph(run("Only.")))
        out = _redline(
            docx,
            Change("Only", insert="after", paragraphs=(NewParagraph("One."),)),
            Change("Only", insert="after", paragraphs=(NewParagraph("Two."),)),
        )
        assert _texts(accepted(out)) == ["Only.", "One.", "Two."]
        assert _texts(rejected(out)) == ["Only."]

    def test_revision_and_comment_ids_stay_unique(self):
        out = redline(
            FIXTURE,
            [
                Change("Kickoff", insert="after", paragraphs=(NewParagraph("Payable on signature."),), comment="Timing."),
                Change("has given written notice", insert="after", paragraphs=(NewParagraph("Item."),), comment="Cure."),
                Change("thirty (30) days of receipt", replace="forty-five (45) days of receipt", comment="Net 45."),
            ],
            "Contract Assistant",
            now=NOW,
        ).docx
        root = ElementTree.fromstring(part(out))
        ids = [el.get(W + "id") for tag in ("ins", "del") for el in root.iter(W + tag)]
        assert len(ids) == len(set(ids))
        comment_ids = {el.get(W + "id") for el in root.iter(W + "commentReference")}
        assert len(comment_ids) == 3 and not comment_ids & set(ids)


class TestComments:
    def test_the_comment_sits_on_the_anchor_not_in_the_new_paragraph(self):
        out = _redline(
            FIXTURE,
            Change(
                "has given written notice",
                insert="after",
                paragraphs=(NewParagraph("Contractor has offered a payment plan; and"),),
                comment="Adds a cure step.",
            ),
        )
        anchor = _paragraph_with(out, "has given written notice")
        new = _paragraph_with(out, "offered a payment plan")
        assert anchor.find(W + "commentRangeStart") is not None
        assert anchor.find(f"{W}r/{W}commentReference") is not None
        assert not list(new.iter(W + "commentReference")) and not list(new.iter(W + "commentRangeStart"))
        with zipfile.ZipFile(io.BytesIO(out)) as package:
            assert "Adds a cure step." in package.read("word/comments.xml").decode()
        assert rejected(out) == accepted(FIXTURE)


# ── Formatting that must not be copied ───────────────────────────────────────


class TestCopiedProperties:
    def test_a_section_break_and_revision_records_are_not_copied(self):
        source = (
            "<w:p><w:pPr><w:pStyle w:val=\"BodyText\"/>"
            '<w:rPr><w:del w:id="7" w:author="Sponsor Counsel" w:date="2026-10-01T09:00:00Z"/><w:b/></w:rPr>'
            "<w:sectPr/>"
            '<w:pPrChange w:id="8" w:author="Sponsor Counsel" w:date="2026-10-01T09:00:00Z"><w:pPr/></w:pPrChange>'
            "</w:pPr><w:r><w:t>End of part one.</w:t></w:r></w:p>"
        )
        docx = build_docx(paragraph(run("Anchor.")), source, paragraph(run("Next.")))
        out = _redline(docx, Change("Anchor", insert="after", paragraphs=(NewParagraph("New.", like="End of part one"),)))
        ppr = _paragraph_with(out, "New.").find(W + "pPr")
        assert [c.tag for c in ppr] == [W + "pStyle", W + "rPr"]
        assert [c.tag for c in ppr.find(W + "rPr")] == [W + "ins", W + "b"]

    def test_a_tracked_numbering_change_is_not_copied(self):
        source = (
            '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="3"/>'
            '<w:ins w:id="9" w:author="Sponsor Counsel" w:date="2026-10-01T09:00:00Z"/></w:numPr></w:pPr>'
            "<w:r><w:t>Numbered item.</w:t></w:r></w:p>"
        )
        docx = build_docx(source, paragraph(run("Next.")))
        out = _redline(docx, Change("Numbered item", insert="after", paragraphs=(NewParagraph("Another item."),)))
        numbering = _paragraph_with(out, "Another item.").find(f"{W}pPr/{W}numPr")
        assert [c.tag for c in numbering] == [W + "ilvl", W + "numId"]

    def test_a_paragraph_without_properties_gets_only_the_insertion_mark(self):
        docx = build_docx(paragraph(run("Anchor.")), paragraph(run("Next.")))
        out = _redline(docx, Change("Anchor", insert="after", paragraphs=(NewParagraph("New."),)))
        ppr = _paragraph_with(out, "New.").find(W + "pPr")
        assert [c.tag for c in ppr] == [W + "rPr"]

    def test_inside_a_text_box_the_new_paragraph_joins_the_text_box(self):
        box = (
            '<w:p><w:r><w:t>Before the box.</w:t></w:r><w:r><w:pict>'
            '<v:shape xmlns:v="urn:schemas-microsoft-com:vml"><v:textbox><w:txbxContent>'
            "<w:p><w:r><w:t>Box line one.</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Box line two.</w:t></w:r></w:p>"
            "</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>"
        )
        docx = build_docx(box, paragraph(run("After.")))
        out = _redline(docx, Change("Box line one", insert="after", paragraphs=(NewParagraph("Box line new."),)))
        box_content = next(ElementTree.fromstring(part(out)).iter(W + "txbxContent"))
        assert [_text(p, "del") for p in box_content.findall(W + "p")] == ["Box line one.", "Box line new.", "Box line two."]
        ElementTree.fromstring(part(out))


# ── All or nothing ───────────────────────────────────────────────────────────


class TestProblems:
    def test_every_problem_is_reported_and_nothing_is_written(self):
        with pytest.raises(RedlineError) as err:
            _redline(
                FIXTURE,
                Change("Kickoff", insert="beside", paragraphs=(NewParagraph("x"),)),
                Change("Kickoff", replace="Start", insert="after", paragraphs=(NewParagraph("x"),)),
                Change("Kickoff", insert="after"),
                Change("Kickoff", insert="after", paragraphs=(NewParagraph("  "),)),
                Change("Kickoff", insert="after", paragraphs=(NewParagraph("x", lead=" "),)),
                Change("no such wording", insert="after", paragraphs=(NewParagraph("x"),)),
                Change("Kickoff", insert="after", paragraphs=(NewParagraph("x", like="no such wording"), NewParagraph("y"))),
                Change("Kickoff", insert="after", paragraphs=(NewParagraph("x", like="thirty (30) days"), NewParagraph("y"))),
            )
        assert err.value.problems == [
            'Change 1: `insert` must be "after" or "before".',
            "Change 2: give `replace` or `insert`, not both.",
            "Change 3: `insert` needs `paragraphs`, the wording of each new paragraph.",
            "Change 4: new paragraph 1 has no `text`.",
            "Change 5: new paragraph 1 has an empty `lead`.",
            'Change 6: "no such wording" was not found. Copy the wording exactly as it appears in a single paragraph.',
            'Change 7: `like` "no such wording" for new paragraph 1 was not found. Copy wording from the '
            "paragraph whose formatting it should have.",
            'Change 8: `like` "thirty (30) days" for new paragraph 1 appears in 2 paragraphs. Use wording from only one.',
        ]

    def test_an_ambiguous_anchor_needs_occurrence(self):
        docx = build_docx(paragraph(run("Same.")), paragraph(run("Same.")), paragraph(run("End.")))
        with pytest.raises(RedlineError) as err:
            _redline(docx, Change("Same", insert="after", paragraphs=(NewParagraph("x"),)))
        assert "appears 2 times" in err.value.problems[0]
        out = _redline(docx, Change("Same", insert="after", occurrence=2, paragraphs=(NewParagraph("New."),)))
        assert _texts(accepted(out)) == ["Same.", "Same.", "New.", "End."]


def test_wording_outside_a_revision_is_refused_before_anything_is_saved(monkeypatch):
    """The self-check: with this call's insertions dropped, every edited paragraph
    must read as it did. A new paragraph whose run escaped its ``w:ins`` fails it."""
    import agents.builtin_tools.office.docx_redline as engine

    def leaky(new, like, ppr, ids):
        p = ElementTree.Element(W + "p")
        p.append(ppr)
        p.append(ElementTree.fromstring(f'<w:r xmlns:w="{W_NS}"><w:t>{new.text}</w:t></w:r>'))
        return p

    monkeypatch.setattr(engine, "_new_paragraph", leaky)
    with pytest.raises(RedlineError) as err:
        _redline(FIXTURE, Change("Kickoff", insert="before", paragraphs=(NewParagraph("Untracked."),)))
    assert err.value.problems == ["The redline would have changed wording it did not mark, so nothing was saved."]
