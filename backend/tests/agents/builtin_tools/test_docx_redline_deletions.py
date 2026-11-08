"""Whole-paragraph deletions in the redline engine (docs/specs/word-tracked-changes.md §3b).

The invariants: **Accept All removes the deleted paragraphs outright, marks
included, so the list items and sections after them renumber; Reject All gives
back the original paragraphs, text, style and numbering; and nothing outside the
paragraphs a change touches is re-serialized.**

The views (``accepted`` / ``rejected``, shared with the insertion tests) apply
paragraph-mark revisions the way Word does: a paragraph whose mark is deleted
joins the next paragraph when the deletion is accepted, and the next
paragraph's mark, with its properties, is the one that survives.
"""

from __future__ import annotations

from typing import List
from xml.etree import ElementTree

import pytest

import agents.builtin_tools.office.docx_redline as engine
from agents.builtin_tools.office.docx_redline import Change, NewParagraph, RedlineError, redline
from apis.shared.kb_backend.docx_revisions import annotated_text
from tests.agents.builtin_tools.test_docx_redline_paragraphs import (
    FIXTURE,
    NOW,
    W,
    _mark,
    _redline,
    _text,
    _texts,
    _unit_sources,
    accepted,
    rejected,
)
from tests.shared.docx_fixtures import build_docx, paragraph, part, run

ORIGINAL = accepted(FIXTURE)


def _paragraph_with(docx: bytes, wording: str) -> ElementTree.Element:
    """The one paragraph holding ``wording``, deleted wording included."""
    hits = [p for p in ElementTree.fromstring(part(docx)).iter(W + "p") if wording in _text(p, "ins")]
    assert len(hits) == 1, wording
    return hits[0]


def _problems(docx: bytes, *changes: Change) -> List[str]:
    with pytest.raises(RedlineError) as err:
        redline(docx, list(changes), "Contract Assistant", now=NOW)
    return err.value.problems


def _cell(docx: bytes, wording: str) -> List[ElementTree.Element]:
    root = ElementTree.fromstring(part(docx))
    cell = next(tc for tc in root.iter(W + "tc") if any(wording in _text(p, "ins") for p in tc.iter(W + "p")))
    return cell.findall(W + "p")


def _styled(text: str, jc: str = "both") -> str:
    return f'<w:p><w:pPr><w:jc w:val="{jc}"/></w:pPr>{run(text)}</w:p>'


def _table(*cell_paragraphs: str) -> str:
    return f"<w:tbl><w:tr><w:tc>{''.join(cell_paragraphs)}</w:tc></w:tr></w:tbl>"


def _accepted_cell(docx: bytes, wording: str) -> List[str]:
    """A table cell's paragraphs after Accept All."""
    out, carry = [], ""
    for p in _cell(docx, wording):
        text = carry + _text(p, "del")
        if _mark(p, "del"):
            carry = text
            continue
        carry = ""
        out.append(text)
    return out


# ── A lettered item, on the real fixture ─────────────────────────────────────


class TestListItem:
    CHANGE = Change("has given written notice", delete="paragraph")

    def test_accept_all_removes_the_item_and_the_next_one_takes_its_letter(self):
        rows = accepted(_redline(FIXTURE, self.CHANGE))
        before = ORIGINAL.index(("Contractor has given written notice of the overdue amount; and", "Normal", "2"))
        assert rows == ORIGINAL[:before] + ORIGINAL[before + 1 :]
        # The item after it keeps its own mark, so its numbering: (c) becomes (b).
        assert rows[before] == ("University has not cured within thirty (30) days after that notice.", "Normal", "2")

    def test_reject_all_gives_back_the_original(self):
        assert rejected(_redline(FIXTURE, self.CHANGE)) == ORIGINAL

    def test_every_run_and_the_paragraph_mark_are_tracked_deletions(self):
        p = _paragraph_with(_redline(FIXTURE, self.CHANGE), "has given written notice")
        assert _mark(p, "del")
        assert [c.tag for c in p if c.tag != W + "pPr"] == [W + "del"]
        assert p.find(f"{W}del/{W}r/{W}t") is None
        delete = p.find(W + "del")
        assert delete.get(W + "author") == "Contract Assistant"
        assert delete.get(W + "date") == "2026-11-07T15:30:00Z"
        mark = p.find(f"{W}pPr/{W}rPr/{W}del")
        assert mark.get(W + "author") == "Contract Assistant"

    def test_only_the_deleted_paragraph_is_re_serialized(self):
        before, after = _unit_sources(FIXTURE), _unit_sources(_redline(FIXTURE, self.CHANGE))
        at = next(i for i, s in enumerate(before) if "has given written notice" in s)
        assert after[:at] == before[:at] and after[at + 1 :] == before[at + 1 :]
        assert after[at] != before[at]

    def test_the_tracked_changes_reader_shows_the_deletion(self):
        text = annotated_text(_redline(FIXTURE, self.CHANGE))
        assert "[deleted: Contractor has given written notice of the overdue amount; and]" in text

    def test_the_summary_names_the_paragraph(self):
        result = redline(FIXTURE, [self.CHANGE], "Contract Assistant", now=NOW)
        assert result.applied == ['1. deleted the paragraph "Contractor has given written notice of the overdue amount; …"']


# ── A whole numbered section ─────────────────────────────────────────────────


class TestWholeSection:
    CHANGE = Change("Payment", delete="paragraph", through="University has not cured")

    def test_heading_to_last_item_goes_and_later_headings_keep_their_style_numbering(self):
        rows = accepted(_redline(FIXTURE, self.CHANGE))
        assert _texts(rows) == [
            "SERVICES AGREEMENT",
            "This Services Agreement is entered into by the University and the Contractor as of the Effective Date.",
            "Services",
            "Contractor shall perform the services described in Exhibit A in a professional and workmanlike manner.",
            "Limitation of Liability",
            "Neither party shall be liable for indirect, incidental or consequential damages.",
            "Entire Agreement",
            "This Agreement is the entire agreement of the parties and supersedes all prior agreements.",
            "Milestone",
            "Fee",
            "Kickoff",
            "$10,000",
            "IN WITNESS WHEREOF, the parties have signed this Agreement.",
        ]
        # Numbered by their style, so "Limitation of Liability" is now section 2.
        assert rows[4][1] == rows[6][1] == "Heading1"

    def test_reject_all_gives_back_the_original(self):
        assert rejected(_redline(FIXTURE, self.CHANGE)) == ORIGINAL

    def test_every_paragraph_in_the_span_is_deleted_mark_and_all(self):
        out = _redline(FIXTURE, self.CHANGE)
        for wording in ("Payment", "Invoices.", "except as follows", "ninety (90) days", "written notice", "has not cured"):
            assert _mark(_paragraph_with(out, wording), "del"), wording

    def test_the_summary_names_both_ends(self):
        result = redline(FIXTURE, [self.CHANGE], "Contract Assistant", now=NOW)
        assert result.applied == ['1. deleted 6 paragraphs, "Payment" through "University has not cured within thirty …"']

    def test_through_takes_the_first_later_paragraph_with_that_wording(self):
        # "thirty (30)" is in the Invoices paragraph and again in item (c).
        out = _redline(FIXTURE, Change("Payment", delete="paragraph", through="thirty (30)"))
        assert _texts(accepted(out))[4] == "Contractor shall not suspend the services for late payment, except as follows:"


# ── Where no paragraph follows ───────────────────────────────────────────────


class TestNoFollowingParagraph:
    def test_a_cell_whose_only_paragraph_is_deleted_keeps_one_empty_paragraph(self):
        result = redline(FIXTURE, [Change("Kickoff", delete="paragraph")], "Contract Assistant", now=NOW)
        assert result.applied == [
            '1. deleted the paragraph "Kickoff" (the table cell keeps one empty paragraph, as Word requires)'
        ]
        (p,) = _cell(result.docx, "Kickoff")
        assert not _mark(p, "del")
        assert p.find(f"{W}del/{W}r/{W}delText").text == "Kickoff"
        assert _accepted_cell(result.docx, "Kickoff") == [""]
        assert rejected(result.docx) == ORIGINAL

    def test_the_last_paragraph_of_a_cell_takes_the_mark_of_the_one_before(self):
        docx = build_docx(_table(_styled("Due on signature."), _styled("Payable by wire.")), paragraph(run("After.")))
        out = _redline(docx, Change("Payable by wire", delete="paragraph"))
        first, last = _cell(out, "Due on signature")
        assert _mark(first, "del") and not _mark(last, "del")
        assert _accepted_cell(out, "Due on signature") == ["Due on signature."]
        assert _texts(rejected(out)) == ["Due on signature.", "Payable by wire.", "After."]

    def test_that_needs_the_paragraph_before_to_be_formatted_the_same(self):
        docx = build_docx(_table(_styled("Due on signature.", "left"), _styled("Payable by wire.")), paragraph(run("After.")))
        (problem,) = _problems(docx, Change("Payable by wire", delete="paragraph"))
        assert problem.startswith('Change 1: "Payable by wire." is the last paragraph of its table cell, so its paragraph mark has to stay')
        assert '"Due on signature.", is formatted differently' in problem

    def test_a_section_ending_before_a_table_merges_into_the_paragraph_before_it(self):
        out = _redline(FIXTURE, Change("Entire Agreement", delete="paragraph", through="supersedes all prior"))
        assert _mark(_paragraph_with(out, "consequential damages"), "del")
        assert not _mark(_paragraph_with(out, "supersedes all prior"), "del")
        rows = accepted(out)
        at = _texts(rows).index("Neither party shall be liable for indirect, incidental or consequential damages.")
        assert rows[at][1] == "Normal" and rows[at + 1][0] == "Milestone"
        assert len(rows) == len(ORIGINAL) - 2
        assert rejected(out) == ORIGINAL

    def test_the_paragraph_before_a_table_alone_is_refused_when_it_follows_a_heading(self):
        (problem,) = _problems(FIXTURE, Change("supersedes all prior", delete="paragraph"))
        assert "comes right before a table, so its paragraph mark has to stay" in problem
        assert '"Entire Agreement", is formatted differently' in problem
        assert 'Strike the wording with `replace: ""` instead' in problem

    def test_the_last_paragraph_of_the_document_merges_into_the_one_before(self):
        docx = build_docx(_styled("First."), _styled("Second."), _styled("Signature block."))
        out = _redline(docx, Change("Signature block", delete="paragraph"))
        assert _texts(accepted(out)) == ["First.", "Second."]
        assert _texts(rejected(out)) == ["First.", "Second.", "Signature block."]

    def test_the_last_paragraph_after_a_table_is_refused(self):
        (problem,) = _problems(FIXTURE, Change("IN WITNESS WHEREOF", delete="paragraph"))
        assert "is the last paragraph of the document, so its paragraph mark has to stay" in problem
        assert "Here a table comes before it." in problem

    def test_the_paragraph_before_must_not_already_have_a_revised_mark(self):
        marked = (
            '<w:p><w:pPr><w:jc w:val="both"/><w:rPr><w:ins w:id="5" w:author="Sponsor Counsel" '
            'w:date="2026-10-01T09:00:00Z"/></w:rPr></w:pPr><w:r><w:t>Closing.</w:t></w:r></w:p>'
        )
        (problem,) = _problems(build_docx(marked, _styled("Signature block.")), Change("Signature block", delete="paragraph"))
        assert "That paragraph's mark is already a tracked change or a section break" in problem


# ── Comments ─────────────────────────────────────────────────────────────────


class TestComments:
    def test_the_comment_goes_on_the_next_paragraph_never_in_the_deleted_one(self):
        result = redline(
            FIXTURE,
            [Change("has given written notice", delete="paragraph", comment="Not acceptable.")],
            "Contract Assistant",
            now=NOW,
        )
        deleted = _paragraph_with(result.docx, "has given written notice")
        assert deleted.find(W + "commentRangeStart") is None and deleted.find(f".//{W}commentReference") is None
        keeper = _paragraph_with(result.docx, "has not cured")
        assert keeper.find(W + "commentRangeStart") is not None
        assert keeper.find(f"{W}r/{W}commentReference") is not None
        assert result.applied[0].endswith('and commented on "University has not cured within thirty …"')
        assert "Not acceptable." in part(result.docx, "word/comments.xml")
        assert rejected(result.docx) == ORIGINAL

    def test_where_the_mark_merges_backwards_the_comment_goes_where_the_mark_survives(self):
        # Not on the paragraph before, whose mark is deleted: LibreOffice then
        # keeps an empty paragraph on Accept All.
        result = redline(
            FIXTURE,
            [Change("Entire Agreement", delete="paragraph", through="supersedes", comment="Duplicate.")],
            "Contract Assistant",
            now=NOW,
        )
        assert _paragraph_with(result.docx, "consequential damages").find(W + "commentRangeStart") is None
        kept = _paragraph_with(result.docx, "supersedes all prior")
        assert not _mark(kept, "del") and kept.find(W + "commentRangeStart") is not None
        assert result.applied[0].endswith('"This Agreement is the entire agreement …", with a comment')
        assert rejected(result.docx) == ORIGINAL

    def test_a_comment_cannot_land_on_a_paragraph_another_change_deletes(self):
        problems = _problems(
            FIXTURE,
            Change("ninety (90) days past due", delete="paragraph", comment="Drop."),
            Change("has given written notice", delete="paragraph"),
        )
        assert problems == [
            "Change 1's comment would go on the next paragraph, which change 2 deletes. Delete them in one "
            "change with `through`."
        ]


# ── Combinations ─────────────────────────────────────────────────────────────


class TestCombinations:
    def test_deletions_insertions_and_text_edits_apply_together(self):
        out = _redline(
            FIXTURE,
            Change("thirty (30) days of receipt", replace="forty-five (45) days of receipt"),
            Change("has given written notice", delete="paragraph"),
            Change(
                "has not cured",
                insert="after",
                paragraphs=(NewParagraph("University has disputed the invoice in good faith."),),
            ),
            Change("Entire Agreement", delete="paragraph", through="supersedes"),
        )
        assert rejected(out) == ORIGINAL
        texts = _texts(accepted(out))
        assert "Invoices. University shall pay each undisputed invoice within forty-five (45) days of receipt." in texts
        at = texts.index("the invoice is more than ninety (90) days past due;")
        assert texts[at + 1 : at + 4] == [
            "University has not cured within thirty (30) days after that notice.",
            "University has disputed the invoice in good faith.",
            "Limitation of Liability",
        ]
        assert "Entire Agreement" not in texts

    def test_revision_ids_stay_unique(self):
        out = _redline(
            FIXTURE,
            Change("Payment", delete="paragraph", through="except as follows", comment="Moved to Exhibit B."),
            Change("Kickoff", delete="paragraph"),
        )
        root = ElementTree.fromstring(part(out))
        ids = [el.get(W + "id") for tag in ("ins", "del") for el in root.iter(W + tag)]
        assert len(ids) == len(set(ids)) > 0

    def test_inserting_before_the_paragraph_after_a_deletion_is_fine(self):
        out = _redline(
            FIXTURE,
            Change("has given written notice", delete="paragraph"),
            Change("has not cured", insert="before", paragraphs=(NewParagraph("Contractor has offered a payment plan; and"),)),
        )
        assert rejected(out) == ORIGINAL
        texts = _texts(accepted(out))
        at = texts.index("the invoice is more than ninety (90) days past due;")
        assert texts[at + 1] == "Contractor has offered a payment plan; and"


# ── Problems ─────────────────────────────────────────────────────────────────


class TestProblems:
    def test_every_problem_is_reported_and_nothing_is_written(self):
        hyperlinked = (
            '<w:p><w:r><w:t xml:space="preserve">See </w:t></w:r><w:hyperlink w:anchor="x"><w:r><w:t>Exhibit A</w:t>'
            "</w:r></w:hyperlink></w:p>"
        )
        docx = build_docx(_styled("Opening."), hyperlinked, _styled("Middle."), _styled("Closing."))
        problems = _problems(
            docx,
            Change("Exhibit A", delete="paragraph"),
            Change("Opening", delete="nonsense"),
            Change("Opening", replace="", delete="paragraph"),
            Change("Middle", through="Closing"),
            Change("Middle", delete="paragraph", through="Opening"),
            Change("Middle", delete="paragraph", through="nowhere at all"),
        )
        assert problems == [
            'Change 1: the paragraph "See Exhibit A" holds a hyperlink, which this tool cannot delete.',
            'Change 2: `delete` must be "paragraph".',
            'Change 3: give `replace` or `delete`, not both. To strike only some wording, use `replace: ""`.',
            'Change 4: `through` needs `delete: "paragraph"`.',
            'Change 5: `through` "Opening" was not found in a paragraph after "Middle." in the same part of the '
            "document. It appears only earlier, or in another table cell or text box.",
            'Change 6: `through` "nowhere at all" was not found in a paragraph after "Middle." in the same part '
            "of the document. Copy wording from the last paragraph to delete.",
        ]

    def test_a_span_across_a_table_is_refused(self):
        (problem,) = _problems(FIXTURE, Change("Entire Agreement", delete="paragraph", through="IN WITNESS"))
        assert problem == (
            'Change 1: a table lies between "Entire Agreement" and `through` "IN WITNESS". Delete the paragraphs '
            "on each side of it in separate changes."
        )

    @pytest.mark.parametrize(
        "locked,reason",
        [
            ('<w:p><w:r><w:t>Old </w:t></w:r><w:ins w:id="9" w:author="Sponsor Counsel" w:date="2026-10-01T09:00:00Z">'
             "<w:r><w:t>terms.</w:t></w:r></w:ins></w:p>", "holds an existing tracked change"),
            ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:t>Old terms.</w:t></w:r></w:p>',
             "holds a field or embedded object"),
            ('<w:p><w:pPr><w:sectPr/></w:pPr><w:r><w:t>Old terms.</w:t></w:r></w:p>',
             "ends a document section (it holds a section break)"),
            ('<w:p xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><w:r><w:t>Old terms.</w:t></w:r>'
             "<m:oMath/></w:p>", "holds content this tool cannot delete, such as an equation"),
        ],
    )
    def test_locked_content_is_refused(self, locked, reason):
        (problem,) = _problems(build_docx(_styled("Opening."), locked, _styled("Closing.")), Change("Old", delete="paragraph"))
        assert reason in problem

    def test_overlaps_with_other_changes_are_refused(self):
        problems = _problems(
            FIXTURE,
            Change("Payment", delete="paragraph", through="except as follows"),
            Change("Invoices", delete="paragraph"),
            Change("thirty (30) days of receipt", replace="sixty (60) days of receipt"),
            Change("except as follows", insert="after", paragraphs=(NewParagraph("x"),)),
        )
        assert problems == [
            "Change 2 deletes a paragraph change 1 also deletes.",
            "Change 3 edits wording in a paragraph change 1 deletes. Drop one of them.",
            "Change 4 inserts next to a paragraph change 1 deletes. Anchor it on a paragraph that stays.",
        ]

    def test_inserting_after_the_paragraph_whose_mark_a_deletion_takes_is_refused(self):
        problems = _problems(
            FIXTURE,
            Change("Entire Agreement", delete="paragraph", through="supersedes"),
            Change("consequential damages", insert="after", paragraphs=(NewParagraph("x"),)),
        )
        assert problems == [
            'Change 2 inserts after "Neither party shall be liable for indirect, incidental or c…", whose paragraph '
            "mark change 1 deletes. Insert before the next paragraph that stays."
        ]


# ── The self-check ───────────────────────────────────────────────────────────


def test_wording_left_outside_a_deletion_is_refused_before_anything_is_saved(monkeypatch):
    real = engine._strike_runs

    def leaky(paragraph, ids):
        real(paragraph, ids)
        paragraph.append(ElementTree.fromstring(f'<w:r xmlns:w="{W[1:-1]}"><w:t>left behind</w:t></w:r>'))

    monkeypatch.setattr(engine, "_strike_runs", leaky)
    assert _problems(FIXTURE, Change("Kickoff", delete="paragraph")) == [
        "The redline would have kept wording it marked as deleted, so nothing was saved."
    ]


def test_a_paragraph_property_changed_outside_a_revision_is_refused(monkeypatch):
    real = engine._mark_inserted

    def restyling(ppr, mark):
        real(ppr, mark)
        ElementTree.SubElement(ppr, W + "keepNext")

    monkeypatch.setattr(engine, "_mark_inserted", restyling)
    assert _problems(FIXTURE, Change("has given written notice", delete="paragraph")) == [
        "The redline would have changed wording it did not mark, so nothing was saved."
    ]
