"""Word tracked changes are written out as text before knowledge-base ingestion.

Finding B9 (docs/testing/projects-team-simulation-2026-10.md): managed ingestion
indexed a sponsor redline as "net thirty (30) dayspayable in full within ninety
(90) days", and the project assistant read a changed clause as unchanged.
"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

import pytest

from apis.shared.kb_backend.docx_revisions import (
    DOCX_MIME_TYPE,
    _rewrite_part,
    annotate_tracked_changes,
    annotated_text,
    document_text,
)
from tests.shared.docx_fixtures import (
    MEDIA_BYTES,
    W_NS,
    all_text,
    build_docx,
    deleted,
    document_xml,
    inserted,
    paragraph,
    part,
    python_docx_paragraphs,
    run,
)

NOTE_PREFIX = "Note: this document contains tracked changes"


def _body_text(docx: bytes) -> list:
    """python-docx's view, minus the note paragraph at the top."""
    paragraphs = python_docx_paragraphs(docx)
    assert paragraphs[0].startswith(NOTE_PREFIX)
    return paragraphs[1:]


class TestInsertionOnly:
    def test_inserted_wording_is_marked(self):
        docx = build_docx(paragraph(run("Payment is due "), inserted("within 90 days"), run(".")))

        annotated = annotate_tracked_changes(docx)

        assert _body_text(annotated) == ["Payment is due [inserted: within 90 days]."]

    def test_without_the_rewrite_python_docx_loses_the_insertion(self):
        """The legacy (Docling) failure this fixes, pinned so the reason stays visible."""
        docx = build_docx(paragraph(run("Payment is due "), inserted("within 90 days"), run(".")))

        assert python_docx_paragraphs(docx) == ["Payment is due ."]


class TestDeletionOnly:
    def test_deleted_wording_is_marked(self):
        docx = build_docx(paragraph(run("The University shall "), deleted("not "), run("indemnify.")))

        annotated = annotate_tracked_changes(docx)

        assert _body_text(annotated) == ["The University shall [deleted: not ]indemnify."]

    def test_del_text_becomes_ordinary_text(self):
        """``w:delText`` is invisible to readers that only take ``w:t``."""
        annotated = annotate_tracked_changes(build_docx(paragraph(deleted("gone"))))

        xml = part(annotated)
        assert "w:delText" not in xml
        assert "<w:del " not in xml and "</w:del>" not in xml


class TestReplacement:
    def test_the_b9_clause_reads_as_a_change(self):
        docx = build_docx(
            paragraph(
                run("Invoices are payable "),
                deleted("net thirty (30) days"),
                inserted("payable in full within ninety (90) days"),
                run(" of receipt."),
            )
        )
        # The flattened text the finding recorded, reproduced from the fixture.
        assert "net thirty (30) dayspayable in full within ninety (90) days" in all_text(docx)

        annotated = annotate_tracked_changes(docx)

        expected = (
            "Invoices are payable [deleted: net thirty (30) days]"
            "[inserted: payable in full within ninety (90) days] of receipt."
        )
        assert _body_text(annotated) == [expected]
        # A parser that reads every run sees the same thing.
        assert expected in all_text(annotated)

    def test_formatting_of_revised_runs_is_kept(self):
        docx = build_docx(
            paragraph(
                '<w:del w:id="1" w:author="A"><w:r><w:rPr><w:b/></w:rPr>'
                "<w:delText>bold</w:delText></w:r></w:del>"
            )
        )

        xml = part(annotate_tracked_changes(docx))

        assert "<w:r><w:rPr><w:b/></w:rPr><w:t>bold</w:t></w:r>" in xml

    def test_the_note_names_each_author_once_in_first_seen_order(self):
        docx = build_docx(
            paragraph(deleted("a", author="Sponsor Counsel"), inserted("b", author="Contracts Office")),
            paragraph(inserted("c", author="Sponsor Counsel")),
        )

        note = python_docx_paragraphs(annotate_tracked_changes(docx))[0]

        assert note.startswith(f"{NOTE_PREFIX} by Sponsor Counsel, Contracts Office.")
        assert "insertions kept and deletions removed" in note

    def test_an_author_whose_revision_holds_no_wording_is_not_named(self):
        docx = build_docx(
            paragraph(deleted("a", author="Sponsor Counsel")),
            paragraph('<w:del w:id="7" w:author="Formatting Bot"><w:r/></w:del>'),
        )

        note = python_docx_paragraphs(annotate_tracked_changes(docx))[0]

        assert "Formatting Bot" not in note

    def test_an_author_name_with_markup_characters_stays_well_formed(self):
        docx = build_docx(paragraph(inserted("x", author="Smith &amp; Jones &lt;LLP&gt;")))

        annotated = annotate_tracked_changes(docx)

        ElementTree.fromstring(part(annotated))
        assert "by Smith & Jones <LLP>." in python_docx_paragraphs(annotated)[0]

    def test_moves_read_as_a_deletion_and_an_insertion(self):
        docx = build_docx(
            paragraph(
                '<w:moveFrom w:id="5" w:author="A"><w:r><w:t>Clause 4</w:t></w:r></w:moveFrom>'
            ),
            paragraph(
                '<w:moveTo w:id="6" w:author="A"><w:r><w:t>Clause 4</w:t></w:r></w:moveTo>'
            ),
        )

        assert _body_text(annotate_tracked_changes(docx)) == [
            "[deleted: Clause 4]",
            "[inserted: Clause 4]",
        ]

    def test_an_insertion_later_deleted_is_marked_both_ways(self):
        docx = build_docx(
            paragraph(
                '<w:del w:id="1" w:author="B"><w:ins w:id="2" w:author="A">'
                "<w:r><w:delText>draft</w:delText></w:r></w:ins></w:del>"
            )
        )

        assert _body_text(annotate_tracked_changes(docx)) == ["[deleted: [inserted: draft]]"]

    def test_property_level_revision_marks_are_left_in_place(self):
        """``w:ins``/``w:del`` inside ``w:rPr`` mark a paragraph mark as revised.
        They hold no wording, and a run injected into ``w:rPr`` would corrupt it."""
        docx = build_docx(
            '<w:p><w:pPr><w:rPr><w:ins w:id="9" w:author="A" w:date="2026-10-01T00:00:00Z"/>'
            "</w:rPr></w:pPr>" + inserted("new paragraph") + "</w:p>"
        )

        xml = part(annotate_tracked_changes(docx))

        assert '<w:rPr><w:ins w:id="9" w:author="A" w:date="2026-10-01T00:00:00Z"/></w:rPr>' in xml
        assert _body_text(annotate_tracked_changes(docx)) == ["[inserted: new paragraph]"]

    def test_headers_and_footers_are_annotated_too(self):
        header = (
            f'<w:hdr xmlns:w="{W_NS}">'
            + paragraph(run("Draft "), deleted("v1"), inserted("v2"))
            + "</w:hdr>"
        )
        docx = build_docx(paragraph(run("Body.")), extra_parts={"word/header1.xml": header})

        annotated = annotate_tracked_changes(docx)

        assert python_docx_paragraphs(annotated, "word/header1.xml") == [
            "Draft [deleted: v1][inserted: v2]"
        ]


class TestComments:
    def test_a_comment_is_inlined_at_its_anchor(self):
        docx = build_docx(
            paragraph(
                run("Article 10. "),
                deleted("Sponsor shall indemnify University."),
                inserted("University shall indemnify Sponsor."),
                '<w:r><w:commentReference w:id="0"/></w:r>',
            ),
            comments={"0": ("Sponsor Counsel", "Required by our insurer.")},
        )

        annotated = annotate_tracked_changes(docx)

        assert _body_text(annotated) == [
            "Article 10. [deleted: Sponsor shall indemnify University.]"
            "[inserted: University shall indemnify Sponsor.]"
            " [comment by Sponsor Counsel: Required by our insurer.]"
        ]
        assert "[comment by <author>: ...]" in python_docx_paragraphs(annotated)[0]

    def test_comments_alone_do_not_trigger_a_rewrite(self):
        """Comment-only documents keep today's ingestion exactly."""
        docx = build_docx(
            paragraph(run("Text."), '<w:r><w:commentReference w:id="0"/></w:r>'),
            comments={"0": ("Reviewer", "Looks fine.")},
        )

        assert annotate_tracked_changes(docx) is None


class TestNoRevisionsIsUnchanged:
    """``None`` means the caller ingests the original bytes, so a document with
    nothing to annotate is indexed exactly as before."""

    def test_a_plain_document_returns_none(self):
        assert annotate_tracked_changes(build_docx(paragraph(run("Just text.")))) is None

    def test_a_paragraph_mark_revision_alone_returns_none(self):
        docx = build_docx(
            '<w:p><w:pPr><w:rPr><w:del w:id="1" w:author="A"/></w:rPr></w:pPr>'
            + run("Text.")
            + "</w:p>"
        )

        assert annotate_tracked_changes(docx) is None

    def test_a_formatting_only_revision_returns_none(self):
        docx = build_docx(
            '<w:p><w:r><w:rPr><w:b/><w:rPrChange w:id="1" w:author="A"><w:rPr/>'
            "</w:rPrChange></w:rPr><w:t>Now bold.</w:t></w:r></w:p>"
        )

        assert annotate_tracked_changes(docx) is None

    def test_a_revision_with_no_wording_returns_none(self):
        """A deleted empty run changes nothing a reader would see."""
        docx = build_docx(paragraph(run("Text."), '<w:del w:id="1" w:author="A"><w:r/></w:del>'))

        assert annotate_tracked_changes(docx) is None

    @pytest.mark.parametrize("raw", [b"", b"not a zip", b"%PDF-1.7\n..."])
    def test_bytes_that_are_not_a_package_return_none(self, raw):
        assert annotate_tracked_changes(raw) is None

    def test_a_zip_without_a_word_body_returns_none(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as package:
            package.writestr("xl/workbook.xml", "<workbook/>")

        assert annotate_tracked_changes(buffer.getvalue()) is None


class TestPackageIntegrity:
    def _annotated(self) -> bytes:
        return annotate_tracked_changes(
            build_docx(paragraph(run("a "), deleted("b"), inserted("c")))
        )

    def test_untouched_parts_keep_their_bytes_order_and_compression(self):
        original = build_docx(paragraph(run("a "), deleted("b"), inserted("c")))
        annotated = annotate_tracked_changes(original)

        with zipfile.ZipFile(io.BytesIO(original)) as before, zipfile.ZipFile(
            io.BytesIO(annotated)
        ) as after:
            assert after.namelist() == before.namelist()
            assert after.read("word/media/image1.png") == MEDIA_BYTES
            assert after.read("[Content_Types].xml") == before.read("[Content_Types].xml")
            for old, new in zip(before.infolist(), after.infolist()):
                assert new.compress_type == old.compress_type

    def test_the_rewritten_body_is_well_formed_and_keeps_its_prolog(self):
        xml = part(self._annotated())

        ElementTree.fromstring(xml.encode("utf-8"))
        assert xml.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n')
        # Namespace declarations mc:Ignorable depends on survive verbatim.
        assert 'mc:Ignorable="w14"' in xml and "xmlns:mc=" in xml

    def test_the_rewrite_is_deterministic(self):
        assert self._annotated() == self._annotated()

    def test_the_note_is_the_first_paragraph_of_the_body(self):
        root = ElementTree.fromstring(part(self._annotated()))
        body = root.find(f"{{{W_NS}}}body")
        first = "".join(t.text for t in body[0].iter(f"{{{W_NS}}}t"))
        assert first.startswith(NOTE_PREFIX)

    def test_unbalanced_markup_keeps_its_wording(self):
        """A truncated container must never swallow text."""
        xml = document_xml(
            paragraph(run("kept "), '<w:ins w:id="1" w:author="A">', run("before nested "))
            + paragraph(inserted("also kept"))
        ).replace("</w:ins>", "")

        rewritten, _ = _rewrite_part(xml, {}, [])

        positions = [rewritten.index(f">{text}<") for text in ("kept ", "before nested ", "also kept")]
        assert positions == sorted(positions)


class TestAnnotatedText:
    """The managed engine's input: the rewritten document as plain text."""

    def test_is_none_exactly_when_there_is_nothing_to_annotate(self):
        assert annotated_text(build_docx(paragraph(run("Just text.")))) is None
        assert annotated_text(b"not a zip") is None

    def test_the_b9_redline_keeps_every_word_inside_its_markers(self):
        """The dev failure this guards: a model-based parser turned
        ``[deleted: Idaho]`` into ``[deleted: ]``. Text has no parser to do that."""
        docx = build_docx(
            paragraph(
                run("governed by the laws of the State of "),
                deleted("Idaho"),
                inserted("Washington"),
                run(", with venue in "),
                deleted("Ada County, Idaho", rid=3),
                inserted("King County, Washington", rid=4),
                run("."),
            )
        )

        text = annotated_text(docx)

        note, body = text.split("\n\n")
        assert note.startswith(NOTE_PREFIX)
        assert "[deleted: ...] and inserted wording as [inserted: ...]" in note
        assert body == (
            "governed by the laws of the State of [deleted: Idaho][inserted: Washington], "
            "with venue in [deleted: Ada County, Idaho][inserted: King County, Washington]."
        )

    def test_paragraphs_are_separated_and_empty_ones_dropped(self):
        docx = build_docx(paragraph(run("One")), paragraph(), paragraph(run("Two")))

        assert document_text(docx) == "One\n\nTwo"

    def test_a_table_becomes_one_line_per_row(self):
        cell = lambda text: f"<w:tc>{paragraph(run(text))}</w:tc>"  # noqa: E731
        table = (
            "<w:tbl><w:tblPr/>"
            f"<w:tr>{cell('Article')}{cell('Change')}</w:tr>"
            f"<w:tr>{cell('10')}<w:tc>{paragraph(deleted('mutual'), inserted('University'))}</w:tc></w:tr>"
            "</w:tbl>"
        )

        text = annotated_text(build_docx(paragraph(run("Summary")), table))

        assert text.split("\n\n")[1:] == [
            "Summary",
            "Article | Change",
            "10 | [deleted: mutual][inserted: University]",
        ]

    def test_tabs_breaks_and_content_controls_are_read(self):
        docx = build_docx(
            "<w:sdt><w:sdtPr/><w:sdtContent>"
            + paragraph('<w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t></w:r>')
            + "</w:sdtContent></w:sdt>"
        )

        assert document_text(docx) == "a\tb\nc"

    def test_alternate_content_fallback_is_not_read_twice(self):
        mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
        docx = build_docx(
            paragraph(
                run("Before "),
                f'<mc:AlternateContent xmlns:mc="{mc}"><mc:Choice Requires="wps">'
                "<w:r><w:t>box</w:t></w:r></mc:Choice>"
                "<mc:Fallback><w:r><w:t>box</w:t></w:r></mc:Fallback></mc:AlternateContent>",
            )
        )

        assert document_text(docx) == "Before box"

    def test_footnotes_follow_the_body_and_separators_are_skipped(self):
        footnotes = (
            f'<w:footnotes xmlns:w="{W_NS}">'
            '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
            '<w:footnote w:id="1">'
            + paragraph(run("Per the "), deleted("2024"), inserted("2026"), run(" rate sheet."))
            + "</w:footnote></w:footnotes>"
        )
        docx = build_docx(
            paragraph(run("Rates apply."), deleted("x")),
            extra_parts={"word/footnotes.xml": footnotes},
        )

        assert annotated_text(docx).split("\n\n")[-1] == (
            "[footnote: Per the [deleted: 2024][inserted: 2026] rate sheet.]"
        )


def test_the_mime_type_is_wordprocessingml():
    assert DOCX_MIME_TYPE == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
