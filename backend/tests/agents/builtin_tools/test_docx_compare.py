"""Comparing two Word documents into a redline (docs/specs/word-tracked-changes.md §3b).

The invariants: **Accept All gives the revised document's wording, Reject All
gives back the original** (text, style and numbering, with no empty paragraph
left behind), every change is marked in the original's own runs and formatting,
and nothing outside the changed paragraphs moves by a byte.

``fixtures/contract-numbered-revised.docx`` is the counterparty's clean return
of ``contract-numbered.docx``: its source was edited (two reworded clauses, a
removed list item, an added numbered section, a changed table cell) and the
result re-saved by LibreOffice 26.2 headless, so it is a real LibreOffice
package rather than a hand-made one. The Accept/Reject views below apply
paragraph-mark revisions the way Word does.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest

from agents.builtin_tools.office import docx_compare
from agents.builtin_tools.office.docx_compare import DEFAULT_AUTHOR, NoDifferences, compare
from agents.builtin_tools.office.docx_redline import RedlineError
from apis.shared.kb_backend.docx_revisions import annotated_text
from tests.agents.builtin_tools.test_docx_redline_paragraphs import accepted, rejected
from tests.shared.docx_fixtures import W_NS, build_docx, deleted, inserted, paragraph, part, run

W = f"{{{W_NS}}}"
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"
ORIGINAL = (FIXTURES / "contract-numbered.docx").read_bytes()
REVISED = (FIXTURES / "contract-numbered-revised.docx").read_bytes()


def _compare(original: bytes, revised: bytes, **kwargs):
    return compare(original, revised, now=NOW, **kwargs)


def _texts(rows) -> List[str]:
    return [text for text, _, _ in rows]


def _unit_sources(docx: bytes) -> List[str]:
    return re.findall(r"<w:p>.*?</w:p>|<w:p .*?</w:p>", part(docx))


def _comments(docx: bytes) -> List[str]:
    try:
        xml = part(docx, "word/comments.xml")
    except KeyError:
        return []
    return ["".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", c)) for c in re.findall(r"<w:comment .*?</w:comment>", xml)]


def _table(*rows: List[str]) -> str:
    body = "".join(
        "<w:tr>" + "".join(f"<w:tc><w:tcPr/>{paragraph(run(cell))}</w:tc>" for cell in cells) + "</w:tr>"
        for cells in rows
    )
    return f"<w:tbl><w:tblPr/><w:tblGrid/>{body}</w:tbl>"


# ── The counterparty's clean return, on the real fixtures ────────────────────


class TestCleanReturn:
    @pytest.fixture(scope="class")
    def result(self):
        return _compare(ORIGINAL, REVISED)

    def test_accept_all_gives_the_revised_document(self, result):
        assert accepted(result.docx) == accepted(REVISED)

    def test_reject_all_gives_back_the_original(self, result):
        assert rejected(result.docx) == rejected(ORIGINAL) == accepted(ORIGINAL)

    def test_the_summary_lists_each_change_in_document_order(self, result):
        assert result.changes == [
            'Reworded "Contractor shall perform the services described i…": deleted "and workmanlike"; '
            'added "consistent with industry standards"',
            'Reworded "Invoices. University shall pay each undisputed in…": "thirty (30)" → "forty-five (45)"; '
            'added ", without setoff"',
            'Removed "Contractor has given written notice of the overdue amount; …"',
            'Added 2 paragraphs before "Entire Agreement": "Survival", "Sections 3 and 4 survive termination of…"',
            'In a table: reworded "$10,000": "$10,000" → "$15,000"',
        ]
        assert result.unmarked == []
        assert (result.reworded, result.added, result.removed) == (3, 2, 1)

    def test_a_reworded_clause_marks_only_the_words_that_differ(self, result):
        text = annotated_text(result.docx)
        assert (
            "within [deleted: thirty (30)][inserted: forty-five (45)] days of receipt[inserted: , without setoff]."
            in text
        )
        assert "in a professional [deleted: and workmanlike ]manner[inserted:  consistent with industry standards]." in text

    def test_the_marks_keep_the_original_run_formatting(self, result):
        xml = part(result.docx)
        # The bold lead-in is untouched; the edit sits in the plain body run.
        assert "<w:r><w:rPr><w:b /></w:rPr><w:t>Invoices.</w:t></w:r>" in xml or "<w:b/></w:rPr><w:t>Invoices.</w:t>" in xml
        assert re.search(r'<w:del [^>]*w:author="Counterparty"[^>]*><w:r><w:rPr\s*/?>(</w:rPr>)?<w:delText', xml)

    def test_the_removed_list_item_loses_its_mark_too(self, result):
        xml = part(result.docx)
        item = next(p for p in _unit_sources(result.docx) if "Contractor has given written notice" in p)
        assert "<w:delText" in item and "<w:t>" not in item and "<w:t " not in item
        assert re.search(r"<w:pPr>.*<w:rPr><w:del ", item)
        assert xml.count("Contractor has given written notice") == 1

    def test_the_added_section_is_a_numbered_heading_and_a_body_paragraph(self, result):
        rows = accepted(result.docx)
        at = _texts(rows).index("Survival")
        assert rows[at] == ("Survival", "Heading1", None)
        assert rows[at + 1] == ("Sections 3 and 4 survive termination of this Agreement.", "Normal", None)
        assert rows[at + 2][0] == "Entire Agreement"

    def test_every_revision_is_by_the_counterparty_at_one_time(self, result):
        xml = part(result.docx)
        marks = re.findall(r'<w:(?:ins|del) w:id="(\d+)" w:author="([^"]*)" w:date="([^"]*)"', xml)
        assert len(marks) >= 8
        assert {(author, date) for _, author, date in marks} == {(DEFAULT_AUTHOR, "2026-11-08T09:00:00Z")}
        ids = [i for i, _, _ in marks]
        assert len(ids) == len(set(ids))

    def test_paragraphs_that_did_not_change_keep_their_bytes(self, result):
        before, after = _unit_sources(ORIGINAL), _unit_sources(result.docx)
        changed = ("Contractor shall perform", "Invoices.", "Contractor has given", "Entire Agreement", "$10,000")
        kept = [p for p in before if not any(c in p for c in changed)]
        assert kept and all(p in after for p in kept)
        # Everything outside the body is copied as it was.
        with zipfile.ZipFile(io.BytesIO(ORIGINAL)) as a, zipfile.ZipFile(io.BytesIO(result.docx)) as b:
            assert a.namelist() == b.namelist()
            for name in a.namelist():
                if name != "word/document.xml":
                    assert a.read(name) == b.read(name), name

    def test_the_author_can_be_named(self):
        out = _compare(ORIGINAL, REVISED, author="Sponsor Counsel").docx
        assert set(re.findall(r'w:author="([^"]*)"', part(out))) == {"Sponsor Counsel"}

    def test_comparing_the_other_way_round_inverts_it(self):
        out = _compare(REVISED, ORIGINAL).docx
        assert accepted(out) == accepted(ORIGINAL)
        assert rejected(out) == accepted(REVISED)


# ── Refusals and no-ops ──────────────────────────────────────────────────────


class TestRefusals:
    def test_a_document_with_tracked_changes_is_refused(self):
        redlined = build_docx(paragraph(run("Governed by the law of "), deleted("Idaho"), inserted("Utah"), run(".")))
        clean = build_docx(paragraph(run("Governed by the law of Utah.")))
        with pytest.raises(RedlineError) as original:
            _compare(redlined, clean)
        assert original.value.problems == [
            "The original already contains tracked changes. Accept or reject them in Word first, "
            "or read it to see its changes."
        ]
        with pytest.raises(RedlineError) as revised:
            _compare(clean, redlined)
        assert revised.value.problems[0].startswith("The revised version already contains tracked changes.")

    def test_a_formatting_revision_counts_as_a_tracked_change(self):
        formatted = build_docx(
            '<w:p><w:r><w:rPr><w:b/><w:rPrChange w:id="1" w:author="A" w:date="2026-01-01T00:00:00Z">'
            "<w:rPr/></w:rPrChange></w:rPr><w:t>Bold now.</w:t></w:r></w:p>"
        )
        with pytest.raises(RedlineError, match="already contains tracked changes"):
            _compare(formatted, build_docx(paragraph(run("Bold now."))))

    def test_the_same_wording_is_no_difference(self):
        with pytest.raises(NoDifferences):
            _compare(ORIGINAL, ORIGINAL)

    def test_typography_and_spacing_alone_are_no_difference(self):
        a = build_docx(paragraph(run("The “Services” — as  defined.")))
        b = build_docx(paragraph(run('The "Services" - as defined.')))
        with pytest.raises(NoDifferences):
            _compare(a, b)

    def test_formatting_alone_is_no_difference(self):
        a = build_docx(paragraph(run("Fees are due monthly.")))
        b = build_docx(paragraph(run("Fees are due monthly.", bold=True)))
        with pytest.raises(NoDifferences):
            _compare(a, b)

    def test_not_a_word_document(self):
        with pytest.raises(RedlineError, match="^Revised: The file is not a Word document"):
            _compare(ORIGINAL, b"plain text")


# ── Paragraph alignment ──────────────────────────────────────────────────────


class TestParagraphs:
    def test_a_paragraph_added_at_the_end_takes_no_empty_paragraph_on_reject(self):
        a = build_docx(paragraph(run("First.")), paragraph(run("Second.")))
        b = build_docx(paragraph(run("First.")), paragraph(run("Second.")), paragraph(run("Third.")))
        out = _compare(a, b)
        assert _texts(accepted(out.docx)) == ["First.", "Second.", "Third."]
        assert _texts(rejected(out.docx)) == ["First.", "Second."]
        assert out.changes == ['Added a paragraph after "Second.": "Third."']

    def test_a_paragraph_added_at_the_start(self):
        a = build_docx(paragraph(run("Body.")))
        b = build_docx(paragraph(run("Recitals.")), paragraph(run("Body.")))
        out = _compare(a, b)
        assert _texts(accepted(out.docx)) == ["Recitals.", "Body."]
        assert _texts(rejected(out.docx)) == ["Body."]

    def test_removing_the_last_paragraph_takes_the_previous_mark(self):
        # The engine's own deletion (§3c): the document's last mark has to
        # stay, so the paragraph before gives up its mark instead, and Accept
        # All leaves no empty paragraph.
        a = build_docx(paragraph(run("Keep.")), paragraph(run("Drop.")))
        b = build_docx(paragraph(run("Keep.")))
        out = _compare(a, b)
        assert _texts(accepted(out.docx)) == ["Keep."]
        assert _texts(rejected(out.docx)) == ["Keep.", "Drop."]
        assert out.changes == ['Removed "Drop."']
        keep = next(p for p in _unit_sources(out.docx) if "Keep." in p)
        assert re.search(r"<w:pPr><w:rPr><w:del ", keep)

    def test_removing_after_a_differently_formatted_paragraph_keeps_the_last_mark(self):
        heading = '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' + run("Terms") + "</w:p>"
        a = build_docx(heading, paragraph(run("Drop.")))
        b = build_docx(heading)
        out = _compare(a, b)
        # Taking the heading's mark would give it the body's formatting, so
        # the last mark stays, as Word keeps it: one empty paragraph.
        assert _texts(accepted(out.docx)) == ["Terms", ""]
        assert _texts(rejected(out.docx)) == ["Terms", "Drop."]

    def test_removed_paragraphs_are_whole_tracked_deletions(self):
        a = build_docx(*(paragraph(run(t)) for t in ("One.", "Two.", "Three.", "Four.")))
        b = build_docx(paragraph(run("One.")), paragraph(run("Four.")))
        out = _compare(a, b)
        for wording in ("Two.", "Three."):
            source = next(p for p in _unit_sources(out.docx) if wording in p)
            assert "<w:delText" in source and re.search(r"<w:pPr><w:rPr><w:del ", source)
        assert _texts(accepted(out.docx)) == ["One.", "Four."]
        assert _texts(rejected(out.docx)) == ["One.", "Two.", "Three.", "Four."]

    def test_a_replaced_last_paragraph_reads_as_a_rewording(self):
        a = build_docx(paragraph(run("Intro.")), paragraph(run("Old A.")), paragraph(run("Old B.")))
        b = build_docx(paragraph(run("Intro.")), paragraph(run("Completely different wording here.")))
        out = _compare(a, b)
        assert _texts(accepted(out.docx)) == ["Intro.", "Completely different wording here."]
        assert _texts(rejected(out.docx)) == ["Intro.", "Old A.", "Old B."]

    def test_removed_and_added_paragraphs_together_are_whole_paragraph_revisions(self):
        a = build_docx(*(paragraph(run(t)) for t in ("Intro.", "Old clause about fees.", "Old clause about venue.", "End.")))
        b = build_docx(*(paragraph(run(t)) for t in ("Intro.", "A new indemnity paragraph entirely.", "End.")))
        out = _compare(a, b)
        assert (out.reworded, out.added, out.removed) == (0, 1, 2)
        sources = _unit_sources(out.docx)
        added = next(p for p in sources if "indemnity" in p)
        assert re.search(r"<w:pPr><w:rPr><w:ins ", added) and "<w:del" not in added
        for wording in ("about fees", "about venue"):
            removed = next(p for p in sources if wording in p)
            assert re.search(r"<w:pPr><w:rPr><w:del ", removed) and "<w:t" not in removed.replace("<w:tab", "")
        assert _texts(accepted(out.docx)) == _texts(accepted(b))
        assert _texts(rejected(out.docx)) == _texts(accepted(a))

    def test_a_moved_paragraph_reads_as_removed_and_added(self):
        a = build_docx(*(paragraph(run(f"Clause {n} text that is long enough to pair.")) for n in "ABC"))
        b = build_docx(*(paragraph(run(f"Clause {n} text that is long enough to pair.")) for n in "BCA"))
        out = _compare(a, b)
        assert _texts(accepted(out.docx)) == _texts(accepted(b))
        assert _texts(rejected(out.docx)) == _texts(accepted(a))
        assert (out.added, out.removed) == (1, 1)

    def test_an_added_paragraph_keeps_its_own_run_formatting(self):
        a = build_docx(paragraph(run("Before.")), paragraph(run("After.")))
        b = build_docx(
            paragraph(run("Before.")), paragraph(run("Survival.", bold=True), run(" Sections 3 and 4 survive.")),
            paragraph(run("After.")),
        )
        out = _compare(a, b).docx
        added = next(p for p in _unit_sources(out) if "Survival." in p)
        assert re.search(r"<w:r><w:rPr><w:b ?/></w:rPr><w:t[^>]*>Survival\.</w:t></w:r><w:r><w:t[^>]*> Sections", added)

    def test_many_changes_in_one_paragraph_are_marked_separately(self):
        a = build_docx(paragraph(run("The Contractor shall deliver the report to the University within ten days.")))
        b = build_docx(paragraph(run("The Vendor shall deliver the final report to the University within thirty days.")))
        text = annotated_text(_compare(a, b).docx)
        assert text.splitlines()[-1] == (
            "The [deleted: Contractor][inserted: Vendor] shall deliver the [inserted: final ]report to the "
            "University within [deleted: ten][inserted: thirty] days."
        )

    def test_a_rewrite_too_large_to_pair_is_removals_and_additions(self, monkeypatch):
        monkeypatch.setattr(docx_compare, "_MAX_PAIRINGS", 3)
        a = build_docx(*(paragraph(run(f"Old clause number {n}.")) for n in range(3)), paragraph(run("End.")))
        b = build_docx(*(paragraph(run(f"New clause number {n}.")) for n in range(3)), paragraph(run("End.")))
        out = _compare(a, b)
        assert (out.reworded, out.added, out.removed) == (0, 3, 3)
        assert _texts(accepted(out.docx)) == _texts(accepted(b))
        assert _texts(rejected(out.docx)) == _texts(accepted(a))

    def test_an_added_empty_paragraph_is_an_inserted_mark_alone(self):
        a = build_docx(paragraph(run("One.")), paragraph(run("Two.")))
        b = build_docx(paragraph(run("One.")), paragraph(), paragraph(run("Two.")))
        out = _compare(a, b)
        assert _texts(accepted(out.docx)) == ["One.", "", "Two."]
        assert _texts(rejected(out.docx)) == ["One.", "Two."]


class TestFormattingFromTheRevisedDocument:
    STYLES = (
        f'<w:styles xmlns:w="{W_NS}"><w:style w:styleId="Normal"/><w:style w:styleId="Heading1"/></w:styles>'
    )

    def _doc(self, *paragraphs: str, styles: bool = True) -> bytes:
        return build_docx(*paragraphs, extra_parts={"word/styles.xml": self.STYLES} if styles else None)

    @staticmethod
    def _styled(style: str, text: str) -> str:
        return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{run(text)}</w:p>'

    def test_style_ids_are_mapped_through_the_paragraphs_both_share(self):
        # The counterparty's editor renamed the heading style; the shared
        # heading teaches the mapping, so the new heading uses the original's.
        a = self._doc(self._styled("Heading1", "Services"), self._styled("Normal", "Body."), self._styled("Normal", "End."))
        b = self._doc(
            self._styled("Ueberschrift1", "Services"), self._styled("Normal", "Body."),
            self._styled("Ueberschrift1", "Survival"), self._styled("Normal", "End."),
        )
        rows = accepted(_compare(a, b).docx)
        assert rows[2] == ("Survival", "Heading1", None)

    def test_a_style_the_original_lacks_is_dropped(self):
        a = self._doc(self._styled("Normal", "Body."), self._styled("Normal", "End."))
        b = self._doc(self._styled("Normal", "Body."), self._styled("Fancy", "Added."), self._styled("Normal", "End."))
        rows = accepted(_compare(a, b).docx)
        assert rows[1] == ("Added.", None, None)

    def test_after_the_last_paragraph_an_added_one_takes_its_formatting(self):
        # No paragraph follows, so the last original mark becomes the added
        # paragraph's (§3a): it reads as added, with that paragraph's style.
        a = self._doc(self._styled("Normal", "Body."))
        b = self._doc(self._styled("Normal", "Body."), self._styled("Heading1", "Survival"))
        out = _compare(a, b).docx
        assert accepted(out) == [("Body.", "Normal", None), ("Survival", "Normal", None)]
        assert rejected(out) == [("Body.", "Normal", None)]

    def test_markup_outside_wordprocessingml_is_not_copied(self):
        a = self._doc(self._styled("Normal", "Body."))
        b = build_docx(
            self._styled("Normal", "Body."),
            '<w:p xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" w14:paraId="1A2B3C4D">'
            '<w:pPr><w:pStyle w:val="Normal"/></w:pPr><w:r><w:rPr><w14:ligatures w14:val="standard"/></w:rPr>'
            "<w:t>Added.</w:t></w:r></w:p>",
            extra_parts={"word/styles.xml": self.STYLES},
        )
        out = _compare(a, b).docx
        assert "w14" not in next(p for p in _unit_sources(out) if "Added." in p)
        assert _texts(accepted(out)) == ["Body.", "Added."]


# ── Tables ───────────────────────────────────────────────────────────────────


class TestTables:
    def test_a_changed_cell_in_the_same_grid_is_marked_in_the_cell(self):
        a = build_docx(_table(["Milestone", "Fee"], ["Kickoff", "$10,000"]), paragraph(run("End.")))
        b = build_docx(_table(["Milestone", "Fee"], ["Kickoff", "$12,500"]), paragraph(run("End.")))
        out = _compare(a, b)
        assert out.changes == ['In a table: reworded "$10,000": "$10,000" → "$12,500"']
        assert "[deleted: $10,000][inserted: $12,500]" in annotated_text(out.docx)

    def test_a_paragraph_added_to_a_cell(self):
        a = build_docx(_table(["Scope"]), paragraph(run("End.")))
        b = build_docx(
            "<w:tbl><w:tblPr/><w:tblGrid/><w:tr><w:tc><w:tcPr/>"
            f"{paragraph(run('Scope'))}{paragraph(run('Includes travel.'))}</w:tc></w:tr></w:tbl>",
            paragraph(run("End.")),
        )
        out = _compare(a, b)
        assert _texts(accepted(out.docx)) == ["Scope", "Includes travel.", "End."]
        assert _texts(rejected(out.docx)) == ["Scope", "End."]

    def test_a_table_with_a_new_row_gets_a_comment_and_no_marks(self):
        a = build_docx(paragraph(run("Fees are due monthly.")), _table(["Kickoff", "$10,000"]), paragraph(run("End.")))
        b = build_docx(
            paragraph(run("Fees are due quarterly.")), _table(["Kickoff", "$10,000"], ["Launch", "$5,000"]),
            paragraph(run("End.")),
        )
        out = _compare(a, b)
        assert out.changes == ['Reworded "Fees are due monthly.": "monthly" → "quarterly"']
        assert out.unmarked == [
            'Not marked: the table starting "Kickoff | $10,000" has different rows or columns; '
            "a comment quotes the revised table."
        ]
        assert _comments(out.docx) == [
            "The revised version changes this table's rows or columns, so its changes are not marked as "
            'tracked changes. Revised table: "Kickoff | $10,000 / Launch | $5,000"'
        ]
        assert _texts(rejected(out.docx)) == _texts(accepted(a))

    def test_an_added_table_is_a_comment_on_the_next_paragraph(self):
        a = build_docx(paragraph(run("Intro.")), paragraph(run("End.")))
        b = build_docx(paragraph(run("Intro.")), _table(["Kickoff", "$10,000"]), paragraph(run("End.")))
        out = _compare(a, b)
        assert out.unmarked == ["Not marked: a table is added in the revised version; a comment quotes it."]
        assert _comments(out.docx) == [
            'The revised version adds a table here, not marked as a tracked change: "Kickoff | $10,000"'
        ]

    def test_a_removed_table_is_a_comment_on_it(self):
        a = build_docx(paragraph(run("Intro.")), _table(["Kickoff", "$10,000"]), paragraph(run("End.")))
        b = build_docx(paragraph(run("Intro.")), paragraph(run("End.")))
        out = _compare(a, b)
        assert _comments(out.docx) == ["The revised version removes this table; it is not marked as a tracked change."]
        assert "<w:commentRangeStart" in next(p for p in _unit_sources(out.docx) if "Kickoff" in p)


# ── What cannot be marked ────────────────────────────────────────────────────


class TestUnmarkable:
    LINK = '<w:hyperlink w:anchor="fees"><w:r><w:t>Schedule 1</w:t></w:r></w:hyperlink>'

    def test_a_rewording_inside_a_hyperlink_becomes_a_comment(self):
        a = build_docx(paragraph(run("Fees are in "), self.LINK, run(".")))
        b = build_docx(paragraph(run("Fees are in Schedule 2.")))
        out = _compare(a, b)
        assert out.changes == []
        assert out.unmarked == [
            'Not marked: "Fees are in Schedule 1." is reworded inside a hyperlink; a comment quotes the revised wording.'
        ]
        assert _comments(out.docx) == [
            "The revised version changes wording inside a hyperlink here, which cannot be marked as a tracked "
            'change: "1" → "2". Revised paragraph: "Fees are in Schedule 2."'
        ]

    def test_a_change_beside_a_hyperlink_is_still_marked(self):
        a = build_docx(paragraph(run("Fees are in "), self.LINK, run(" and are due monthly.")))
        b = build_docx(paragraph(run("Fees are in Schedule 1 and are due quarterly.")))
        out = _compare(a, b)
        assert out.unmarked == []
        assert "[deleted: monthly][inserted: quarterly]" in annotated_text(out.docx)

    def test_beside_a_hyperlink_edit_the_other_edits_are_still_marked(self):
        a = build_docx(paragraph(run("Fees are in "), self.LINK, run(" and are due monthly.")))
        b = build_docx(paragraph(run("Fees are in Schedule 2 and are due quarterly.")))
        out = _compare(a, b)
        assert out.changes == ['Reworded "Fees are in Schedule 1 and are due monthly.": "monthly" → "quarterly"']
        assert len(out.unmarked) == 1 and len(_comments(out.docx)) == 1
        assert '"1" → "2". Revised paragraph: "Fees are in Schedule 2 and are due quarterly."' in _comments(out.docx)[0]
        assert "[deleted: monthly][inserted: quarterly]" in annotated_text(out.docx)
        assert _texts(accepted(out.docx)) == ["Fees are in Schedule 1 and are due quarterly."]
        assert _texts(rejected(out.docx)) == ["Fees are in Schedule 1 and are due monthly."]

    def test_a_removed_paragraph_holding_a_hyperlink_becomes_a_comment(self):
        a = build_docx(paragraph(run("Keep.")), paragraph(run("See "), self.LINK, run(".")), paragraph(run("End.")))
        b = build_docx(paragraph(run("Keep.")), paragraph(run("End.")))
        out = _compare(a, b)
        assert out.removed == 0 and len(out.unmarked) == 1
        assert _comments(out.docx) == [
            "The revised version removes this paragraph, but it holds a hyperlink, which cannot be marked as a "
            "tracked deletion."
        ]

    def test_only_unmarkable_differences_still_write_the_comments(self):
        a = build_docx(paragraph(run("Fees are in "), self.LINK, run(".")))
        b = build_docx(paragraph(run("Fees are in Schedule 2.")))
        assert _comments(_compare(a, b).docx)


# ── The self-checks ──────────────────────────────────────────────────────────


def test_an_accept_view_that_would_differ_is_refused(monkeypatch):
    real = docx_compare._word_edits

    def wrong(old, new):
        return real(old, new)[:-1]  # drop the last edit: the accept view would not match

    monkeypatch.setattr(docx_compare, "_word_edits", wrong)
    with pytest.raises(RedlineError, match="would not read as the revised version"):
        _compare(ORIGINAL, REVISED)
