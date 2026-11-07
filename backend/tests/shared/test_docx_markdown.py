"""A .docx renders as markdown that keeps its structure.

``docx_to_markdown`` is what a Converse request carries in place of a Word
document block (``document_cap``). Measured on dev, the structure is the point:
the flat paragraph extractor dropped a table's blank cells, so every later
value shifted a column and both models read the wrong numbers. These tests pin
headings, list markers and nesting, table alignment (blank and merged cells),
images, tracked changes and determinism.
"""

import pytest

from apis.shared.files.docx_markdown import DocxRenderError, docx_to_markdown
from tests.shared.docx_fixtures import W_NS, build_docx, deleted, inserted, paragraph, run

_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W_NS}">
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
  <w:style w:type="paragraph" w:styleId="ChapterHead"><w:name w:val="Chapter Head"/><w:basedOn w:val="Heading2"/></w:style>
  <w:style w:type="paragraph" w:styleId="Outline3"><w:name w:val="Custom"/><w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:pPr><w:numPr><w:numId w:val="2"/></w:numPr></w:pPr></w:style>
</w:styles>"""

_NUMBERING = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="{W_NS}">
  <w:abstractNum w:abstractNumId="10">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/></w:lvl>
    <w:lvl w:ilvl="2"><w:numFmt w:val="bullet"/></w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="20">
    <w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="20"/></w:num>
</w:numbering>"""


def _styled(style: str, text: str) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{run(text)}</w:p>'


def _item(text: str, num_id: int = 1, level: int = 0) -> str:
    return (
        f'<w:p><w:pPr><w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr></w:pPr>'
        f"{run(text)}</w:p>"
    )


def _cell(text: str = "", props: str = "") -> str:
    tcpr = f"<w:tcPr>{props}</w:tcPr>" if props else ""
    body = paragraph(run(text)) if text else "<w:p/>"
    return f"<w:tc>{tcpr}{body}</w:tc>"


def _table(*rows: str) -> str:
    return f"<w:tbl>{''.join(f'<w:tr>{r}</w:tr>' for r in rows)}</w:tbl>"


def _render(*body: str, styles: bool = True, numbering: bool = True) -> str:
    parts = {}
    if styles:
        parts["word/styles.xml"] = _STYLES
    if numbering:
        parts["word/numbering.xml"] = _NUMBERING
    return docx_to_markdown(build_docx(*body, extra_parts=parts))


class TestHeadings:
    def test_heading_styles_become_markdown_levels(self):
        out = _render(_styled("Title", "Plan"), _styled("Heading1", "Scope"), _styled("Heading2", "Budget"), paragraph(run("Body.")))
        assert out == "# Plan\n\n# Scope\n\n## Budget\n\nBody."

    def test_a_style_based_on_a_heading_is_a_heading(self):
        assert _render(_styled("ChapterHead", "Travel")) == "## Travel"

    def test_an_outline_level_is_a_heading(self):
        assert _render(_styled("Outline3", "Field Travel")) == "### Field Travel"

    def test_heading_style_ids_work_without_styles_xml(self):
        assert _render(_styled("Heading3", "Deep"), styles=False) == "### Deep"


class TestLists:
    def test_numbered_items_count_and_nest(self):
        out = _render(
            _item("Approve minutes"),
            _item("Correction", level=1),
            _item("New courses"),
            _item("DATA 602", level=1),
            _item("ENVS 575", level=1),
            _item("Needs an assessment plan", level=2),
            _item("Program review"),
        )
        assert out == (
            "1. Approve minutes\n"
            "  1. Correction\n"
            "2. New courses\n"
            "  1. DATA 602\n"
            "  2. ENVS 575\n"
            "    - Needs an assessment plan\n"
            "3. Program review"
        )

    def test_a_list_reads_as_one_block_between_paragraphs(self):
        out = _render(paragraph(run("Before.")), _item("one", num_id=2), _item("two", num_id=2), paragraph(run("After.")))
        assert out == "Before.\n\n- one\n- two\n\nAfter."

    def test_numbering_from_the_paragraph_style(self):
        assert _render(_styled("ListBullet", "From style")) == "- From style"

    def test_an_unknown_list_reads_as_bullets(self):
        assert _render(_item("orphan", num_id=99), numbering=False) == "- orphan"

    def test_num_id_zero_switches_numbering_off(self):
        assert _render(_item("plain", num_id=0)) == "plain"


class TestTables:
    def test_a_table_is_a_pipe_table_with_its_header(self):
        out = _render(_table(_cell("Category") + _cell("Year 1"), _cell("Travel") + _cell("$6,200")))
        assert out == "| Category | Year 1 |\n|---|---|\n| Travel | $6,200 |"

    def test_blank_cells_keep_every_later_value_in_its_column(self):
        """The failure the flat extractor had: a missed reading shifted July into June."""
        out = _render(_table(_cell("Site") + _cell("Jan") + _cell("Feb") + _cell("Mar"), _cell("D") + _cell() + _cell() + _cell("9.2")))
        assert out.splitlines()[-1] == "| D |  |  | 9.2 |"

    def test_horizontally_merged_cells_repeat_into_each_column(self):
        header = _cell("Item") + _cell("Timeline", '<w:gridSpan w:val="2"/>')
        out = _render(_table(header, _cell("Memo") + _cell("Oct 28") + _cell("Open")))
        assert out.splitlines()[0] == "| Item | Timeline | Timeline |"
        assert out.splitlines()[2] == "| Memo | Oct 28 | Open |"

    def test_vertically_merged_cells_repeat_down_the_rows(self):
        out = _render(
            _table(
                _cell("Group") + _cell("Member"),
                _cell("Lab A", '<w:vMerge w:val="restart"/>') + _cell("Ana"),
                _cell("", "<w:vMerge/>") + _cell("Ben"),
                _cell("Lab B") + _cell("Cy"),
            )
        )
        assert out.splitlines()[2:] == ["| Lab A | Ana |", "| Lab A | Ben |", "| Lab B | Cy |"]

    def test_pipes_and_multi_paragraph_cells_stay_inside_the_cell(self):
        cell = f"<w:tc>{paragraph(run('a|b'))}{paragraph(run('second'))}</w:tc>"
        out = _render(_table(_cell("H"), cell))
        assert out.splitlines()[-1] == "| a\\|b<br>second |"

    def test_a_ragged_row_is_padded(self):
        out = _render(_table(_cell("A") + _cell("B"), _cell("only")))
        assert out.splitlines()[-1] == "| only |  |"

    def test_an_empty_table_renders_nothing(self):
        assert _render(_table(_cell() + _cell()), paragraph(run("After."))) == "After."


class TestContent:
    def test_an_image_is_named_by_its_alt_text(self):
        drawing = (
            '<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
            '<wp:docPr id="1" name="Picture 1" descr="Figure 1: late policy"/></wp:inline></w:drawing></w:r></w:p>'
        )
        assert _render(drawing) == "[image: Figure 1: late policy]"

    def test_tracked_changes_are_written_out(self, monkeypatch):
        monkeypatch.delenv("WORD_TRACKED_CHANGES_ENABLED", raising=False)
        out = _render(paragraph(run("Cap of "), deleted("$50,000"), inserted("$250,000"), run(".")))
        assert "[deleted: $50,000][inserted: $250,000]" in out

    def test_content_controls_are_read_through(self):
        assert _render(f"<w:sdt><w:sdtContent>{paragraph(run('Inside.'))}</w:sdtContent></w:sdt>") == "Inside."

    def test_tabs_and_breaks(self):
        out = _render(paragraph(run("a"), "<w:r><w:tab/></w:r>", run("b"), "<w:r><w:br/></w:r>", run("c")))
        assert out == "a\tb\nc"

    def test_rendering_is_deterministic(self):
        raw = build_docx(_styled("Heading1", "H"), _item("x"), _table(_cell("a") + _cell("b")), extra_parts={"word/styles.xml": _STYLES, "word/numbering.xml": _NUMBERING})
        assert docx_to_markdown(raw) == docx_to_markdown(raw)

    def test_not_a_word_package_raises(self):
        with pytest.raises(DocxRenderError):
            docx_to_markdown(b"not a zip")
