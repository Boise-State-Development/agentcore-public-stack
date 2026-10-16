"""The redline engine behind ``redline_word_document`` (docs/specs/word-tracked-changes.md §3).

The two invariants every test leans on: **Reject All gives back the original
wording and Accept All gives the requested wording**, so a redline can never
silently change text it did not mark; and **nothing outside the edited
paragraphs changes**, byte for byte.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone
from typing import List
from xml.etree import ElementTree

import pytest

from agents.builtin_tools.office.docx_redline import Change, RedlineError, redline
from apis.shared.kb_backend.docx_revisions import annotated_text
from tests.shared.docx_fixtures import (
    MEDIA_BYTES,
    W_NS,
    build_docx,
    inserted,
    paragraph,
    part,
    run,
)

W = f"{{{W_NS}}}"
NOW = datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc)


def _redline(docx: bytes, *changes: Change, author: str = "Pat Reviewer"):
    return redline(docx, list(changes), author, now=NOW)


def _view(docx: bytes, accept: bool) -> List[str]:
    """Paragraph text after Accept All (``accept``) or Reject All."""
    root = ElementTree.fromstring(part(docx))
    keep, drop = (W + "ins", W + "del") if accept else (W + "del", W + "ins")
    texts = []
    for p in root.iter(W + "p"):
        pieces = []

        def walk(node):
            for child in node:
                if child.tag == drop:
                    continue
                if child.tag in (W + "t", W + "delText"):
                    pieces.append(child.text or "")
                elif child.tag == W + "tab":
                    pieces.append("\t")
                elif child.tag != W + "p":
                    walk(child)

        walk(p)
        texts.append("".join(pieces))
    return texts


def accepted(docx: bytes) -> List[str]:
    return _view(docx, accept=True)


def rejected(docx: bytes) -> List[str]:
    return _view(docx, accept=False)


def _replace_part(docx: bytes, name: str, xml: str) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(docx))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            data = xml.encode("utf-8") if info.filename == name else source.read(info.filename)
            target.writestr(info, data)
    return out.getvalue()


def _revisions(docx: bytes, tag: str) -> List[ElementTree.Element]:
    return list(ElementTree.fromstring(part(docx)).iter(W + tag))


CONTRACT = build_docx(
    paragraph(run("SERVICES AGREEMENT", bold=True)),
    paragraph(run("1. Payment. Invoices are payable within thirty (30) days of receipt.")),
    paragraph(run("2. Governing law. The laws of the State of Idaho govern this Agreement.")),
    paragraph(run("3. Term. The term is one year, renewable by the parties.")),
)
ORIGINAL = rejected(CONTRACT)


class TestReplace:
    def test_only_the_words_that_differ_are_marked(self):
        result = _redline(CONTRACT, Change("within thirty (30) days", "within ninety (90) days"))

        [deleted] = _revisions(result.docx, "del")
        [ins] = _revisions(result.docx, "ins")
        assert "".join(t.text for t in deleted.iter(W + "delText")) == "thirty (30)"
        assert "".join(t.text for t in ins.iter(W + "t")) == "ninety (90)"
        assert accepted(result.docx)[1] == "1. Payment. Invoices are payable within ninety (90) days of receipt."
        assert rejected(result.docx) == ORIGINAL
        assert result.applied == ['1. deleted "thirty (30)", inserted "ninety (90)"']

    def test_each_revision_carries_the_author_and_date(self):
        result = _redline(CONTRACT, Change("Idaho", "Delaware"), author="Sam O'Neil & Co")

        for element in _revisions(result.docx, "del") + _revisions(result.docx, "ins"):
            assert element.get(W + "author") == "Sam O'Neil & Co"
            assert element.get(W + "date") == "2026-10-12T15:30:00Z"

    def test_added_words_at_a_boundary_become_a_pure_insertion(self):
        result = _redline(CONTRACT, Change("of receipt.", "of receipt, without setoff."))

        assert _revisions(result.docx, "del") == []
        [ins] = _revisions(result.docx, "ins")
        assert "".join(t.text for t in ins.iter(W + "t")) == ", without setoff"
        assert accepted(result.docx)[1].endswith("days of receipt, without setoff.")
        assert rejected(result.docx) == ORIGINAL

    def test_a_word_is_replaced_whole_rather_than_patched(self):
        docx = build_docx(paragraph(run("The Supplier shall pay the fee.")))

        result = _redline(docx, Change("shall pay", "shall payment"))

        [deleted] = _revisions(result.docx, "del")
        assert "".join(t.text for t in deleted.iter(W + "delText")) == "pay"

    def test_an_empty_replace_deletes_and_keeps_single_spacing(self):
        result = _redline(CONTRACT, Change("one year, renewable by the parties", "one year"))

        assert accepted(result.docx)[3] == "3. Term. The term is one year."
        assert _revisions(result.docx, "ins") == []
        assert rejected(result.docx) == ORIGINAL

    def test_deleting_a_middle_phrase_takes_one_space_with_it(self):
        result = _redline(CONTRACT, Change("of the State", ""))

        assert accepted(result.docx)[2] == "2. Governing law. The laws of Idaho govern this Agreement."

    def test_several_changes_in_one_paragraph(self):
        result = _redline(
            CONTRACT,
            Change("Invoices are payable", "Invoices are due"),
            Change("thirty (30)", "ninety (90)"),
            Change("of receipt", "of the invoice date"),
        )

        assert accepted(result.docx)[1] == "1. Payment. Invoices are due within ninety (90) days of the invoice date."
        assert rejected(result.docx) == ORIGINAL
        assert [line.split(".")[0] for line in result.applied] == ["1", "2", "3"]

    def test_a_change_across_runs_keeps_each_runs_formatting(self):
        docx = build_docx(paragraph(run("Fees are "), run("non-refundable", bold=True), run(" in all cases.")))

        result = _redline(docx, Change("non-refundable in all cases", "refundable within 30 days"))

        deleted_runs = [r for d in _revisions(result.docx, "del") for r in d.iter(W + "r")]
        bold = [r.find(W + "rPr") is not None for r in deleted_runs]
        assert bold == [True, False]
        [ins] = _revisions(result.docx, "ins")
        assert ins.find(f"{W}r/{W}rPr/{W}b") is not None  # styled like the first deleted run
        assert accepted(result.docx) == ["Fees are refundable within 30 days."]
        assert rejected(result.docx) == ["Fees are non-refundable in all cases."]

    def test_split_runs_preserve_their_spacing(self):
        result = _redline(CONTRACT, Change("Idaho", "Delaware"))

        for t in ElementTree.fromstring(part(result.docx)).iter(W + "t"):
            if t.text and t.text != t.text.strip():
                assert t.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"

    def test_wording_beside_a_tab_can_be_changed(self):
        docx = build_docx(paragraph('<w:r><w:t>Section 4</w:t><w:tab/><w:t xml:space="preserve">Fees apply.</w:t></w:r>'))

        result = _redline(docx, Change("Fees apply", "No fees apply"))

        assert accepted(result.docx) == ["Section 4\tNo fees apply."]
        assert rejected(result.docx) == ["Section 4\tFees apply."]


class TestMatching:
    def test_typography_and_spacing_need_not_match_exactly(self):
        docx = build_docx(paragraph(run("the “Services” — as defined  herein")))

        result = _redline(docx, Change('the "Services" - as defined herein', 'the "Deliverables" - as defined herein'))

        assert accepted(result.docx) == ['the "Deliverables" — as defined  herein']

    def test_an_ambiguous_find_names_the_count(self):
        with pytest.raises(RedlineError) as raised:
            _redline(CONTRACT, Change("the", "a"))
        assert raised.value.problems == [
            'Change 1: "the" appears 2 times. Pass `occurrence` (1–2) or a longer `find` that appears once.'
        ]

    def test_occurrence_picks_the_match(self):
        result = _redline(CONTRACT, Change("the", "both", occurrence=2))

        assert accepted(result.docx)[3] == "3. Term. The term is one year, renewable by both parties."

    def test_a_find_matches_whole_words_only(self):
        with pytest.raises(RedlineError) as raised:
            _redline(CONTRACT, Change("pay", "settle"))
        assert raised.value.problems[0].startswith('Change 1: "pay" was not found.')

    def test_a_miss_says_so_and_hints_at_capitalization(self):
        with pytest.raises(RedlineError) as raised:
            _redline(CONTRACT, Change("governing law", "x"), Change("Arbitration", "x"))
        assert raised.value.problems[0].startswith('Change 1: "governing law" was not found. It does appear')
        assert raised.value.problems[1].startswith('Change 2: "Arbitration" was not found. Copy the wording')

    def test_a_find_across_paragraphs_is_refused_with_the_reason(self):
        with pytest.raises(RedlineError) as raised:
            _redline(CONTRACT, Change("of receipt.\n2. Governing", ""))
        assert "spans paragraphs" in raised.value.problems[0]

    def test_text_inside_an_existing_tracked_change_is_left_alone(self):
        docx = build_docx(paragraph(run("Payment is due "), inserted("within 90 days"), run(".")))

        with pytest.raises(RedlineError) as raised:
            _redline(docx, Change("within 90", "within 60"))
        assert "an existing tracked change" in raised.value.problems[0]

    def test_a_hyperlink_is_not_cut_into(self):
        docx = build_docx(
            paragraph(run("See "), '<w:hyperlink r:id="rId9" xmlns:r="urn:r"><w:r><w:t>our terms</w:t></w:r></w:hyperlink>')
        )

        with pytest.raises(RedlineError) as raised:
            _redline(docx, Change("our", "the"))
        assert "a hyperlink" in raised.value.problems[0]

    def test_a_comment_may_cover_a_hyperlink_whole(self):
        docx = build_docx(
            paragraph(run("See "), '<w:hyperlink><w:r><w:t>our terms</w:t></w:r></w:hyperlink>', run(" online."))
        )

        result = _redline(docx, Change("See our terms", comment="Link is stale."))

        assert len(list(ElementTree.fromstring(part(result.docx)).iter(W + "commentRangeStart"))) == 1


class TestAllOrNothing:
    def test_one_bad_change_saves_nothing_and_every_problem_is_listed(self):
        with pytest.raises(RedlineError) as raised:
            _redline(
                CONTRACT,
                Change("Idaho", "Delaware"),
                Change("nowhere", "x"),
                Change("State of Idaho", "State of Utah"),
                Change("one year"),
            )
        problems = raised.value.problems
        assert problems[0].startswith('Change 2: "nowhere" was not found.')
        assert problems[1] == 'Change 4: give `replace` (new wording, "" to delete) or `comment`.'
        assert problems[2] == "Change 3 overlaps change 1."

    def test_replacing_wording_with_itself_is_refused(self):
        with pytest.raises(RedlineError) as raised:
            _redline(CONTRACT, Change("one year", "one  year"))
        assert raised.value.problems == ["Change 1: `replace` is the same wording as `find`."]

    def test_no_changes_is_an_error(self):
        with pytest.raises(RedlineError):
            redline(CONTRACT, [], "A")

    def test_not_a_word_file(self):
        with pytest.raises(RedlineError) as raised:
            redline(b"plain text", [Change("a", "b")], "A")
        assert raised.value.problems == ["The file is not a Word document."]


class TestComments:
    def test_a_comment_creates_the_part_its_relationship_and_content_type(self):
        result = _redline(CONTRACT, Change("one year", comment="Confirm the term\nwith the sponsor."))

        comments = ElementTree.fromstring(part(result.docx, "word/comments.xml"))
        [comment] = list(comments)
        assert comment.get(W + "author") == "Pat Reviewer"
        assert comment.get(W + "initials") == "PR"
        assert ["".join(t.text for t in p.iter(W + "t")) for p in comment] == ["Confirm the term", "with the sponsor."]
        assert "relationships/comments" in part(result.docx, "word/_rels/document.xml.rels")
        assert 'PartName="/word/comments.xml"' in part(result.docx, "[Content_Types].xml")

        root = ElementTree.fromstring(part(result.docx))
        ids = {e.get(W + "id") for tag in ("commentRangeStart", "commentRangeEnd", "commentReference") for e in root.iter(W + tag)}
        assert ids == {comment.get(W + "id")}
        assert rejected(result.docx) == ORIGINAL  # a comment changes no wording

    def test_a_comment_on_a_replacement_wraps_the_change(self):
        result = _redline(CONTRACT, Change("State of Idaho", "State of Delaware", comment="Their counsel insists."))

        p = list(ElementTree.fromstring(part(result.docx)).iter(W + "p"))[2]
        tags = [c.tag.removeprefix(W) for c in p]
        start = tags.index("commentRangeStart")
        assert tags[start : start + 5] == ["commentRangeStart", "del", "ins", "commentRangeEnd", "r"]
        assert result.applied == ['1. deleted "Idaho", inserted "Delaware", with a comment on the change']

    def test_an_existing_comments_part_is_extended_with_fresh_ids(self):
        docx = build_docx(
            paragraph(run("Clause one."), '<w:commentRangeStart w:id="0"/>', run("Clause two."), '<w:commentRangeEnd w:id="0"/>'),
            comments={"0": ("Counsel", "Earlier note.")},
        )

        result = _redline(docx, Change("Clause one.", comment="New note."))

        comments = ElementTree.fromstring(part(result.docx, "word/comments.xml"))
        assert [c.get(W + "id") for c in comments] == ["0", "1"]
        assert part(result.docx, "word/_rels/document.xml.rels").count("relationships/comments") == 1


class TestThePackage:
    def test_untouched_paragraphs_and_parts_are_byte_identical(self):
        result = _redline(CONTRACT, Change("Idaho", "Delaware"))

        before, after = part(CONTRACT), part(result.docx)
        edited = re.compile(r"<w:p>(?:(?!<w:p>).)*?Idaho.*?</w:p>", re.S)
        assert edited.sub("", before) == re.sub(r"<w:p>(?:(?!<w:p>).)*?Delaware.*?</w:p>", "", after, flags=re.S)
        with zipfile.ZipFile(io.BytesIO(result.docx)) as package:
            assert package.read("word/media/image1.png") == MEDIA_BYTES
            assert package.namelist() == zipfile.ZipFile(io.BytesIO(CONTRACT)).namelist()

    def test_root_declarations_that_mc_ignorable_relies_on_survive(self):
        root = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
            f'<w:document xmlns:wpc="urn:wpc" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            f'xmlns:w="{W_NS}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
            'mc:Ignorable="w14 wpc"><w:body>'
        )
        xml = root + '<w:p w14:paraId="1A2B3C4D"><w:r><w:t>Fee: $10.</w:t></w:r></w:p><w:sectPr/></w:body></w:document>'
        docx = _replace_part(build_docx(paragraph(run("x"))), "word/document.xml", xml)

        result = _redline(docx, Change("$10", "$12"))

        after = part(result.docx)
        assert after.startswith(root)
        assert 'w14:paraId="1A2B3C4D"' in after
        assert accepted(result.docx) == ["Fee: $12."]

    def test_a_declaration_inside_an_edited_paragraph_is_put_back_on_it(self):
        docx = build_docx(
            paragraph('<w:pPr><x:tag xmlns:x="urn:example" x:v="1"/></w:pPr>', run("Net 30 days."))
        )

        result = _redline(docx, Change("Net 30", "Net 45"))

        p = next(ElementTree.fromstring(part(result.docx)).iter(W + "p"))
        assert p.find(f"{W}pPr/{{urn:example}}tag").get("{urn:example}v") == "1"

    def test_revision_ids_are_fresh(self):
        docx = build_docx(paragraph('<w:bookmarkStart w:id="41" w:name="x"/>', run("Pay 5 dollars."), '<w:bookmarkEnd w:id="41"/>'))

        result = _redline(docx, Change("5", "6"), Change("dollars", comment="USD?"))

        ids = [int(m) for m in re.findall(r'w:id="(\d+)"', part(result.docx)) if m != "41"]
        assert min(ids) > 41
        revision_ids = [e.get(W + "id") for tag in ("ins", "del") for e in _revisions(result.docx, tag)]
        assert len(set(revision_ids)) == len(revision_ids)

    def test_the_tracked_changes_reader_sees_the_intended_redline(self):
        result = _redline(
            CONTRACT,
            Change("within thirty (30) days", "within ninety (90) days"),
            Change("State of Idaho", "State of Delaware", comment="Counsel insists."),
        )

        text = annotated_text(result.docx)
        assert "payable within [deleted: thirty (30)][inserted: ninety (90)] days" in text
        assert "State of [deleted: Idaho][inserted: Delaware] [comment by Pat Reviewer: Counsel insists.]" in text
        assert "tracked changes by Pat Reviewer" in text
