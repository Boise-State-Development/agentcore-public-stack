"""Native Office charts reach the model as their saved data.

A Word or PowerPoint chart stores its series, categories and values in a
cache inside its chart part. ``office_charts`` reads that cache; the Word
renderer (``docx_to_markdown``), the Word paragraph index
(``docx_paragraphs``, which ``pattern`` searches and large Word files are read
through) and ``read_powerpoint_presentation`` all render through it. Before,
Word said ``[image: Chart 1]`` and PowerPoint said nothing.

The fixtures follow the structure Word 365 writes: a chart in a ``w:drawing``
inside a run, ``c14`` extension lists, an Office 2016 chart wrapped in
``mc:AlternateContent`` with a picture as its ``Fallback``.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from apis.shared.files import office_charts
from apis.shared.files.document_read import docx_paragraphs
from apis.shared.files.docx_markdown import docx_to_markdown
from apis.shared.files.office_charts import (
    CHART_MARKER_CLOSE,
    CHART_MARKER_OPEN,
    chart_line,
    chart_markdown,
    fill_chart_markers,
    read_chart,
)
from tests.shared.docx_fixtures import document_xml, paragraph, run

C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
CX_NS = "http://schemas.microsoft.com/office/drawing/2014/chartex"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
REL_CHART = f"{R_NS}/chart"
REL_CHARTEX = "http://schemas.microsoft.com/office/2014/relationships/chartEx"


# ---------------------------------------------------------------------------
# Chart part builders
# ---------------------------------------------------------------------------


def _str_cache(values, ref="Sheet1!$A$2:$A$5") -> str:
    pts = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in enumerate(values) if v is not None)
    return f'<c:strRef><c:f>{ref}</c:f><c:strCache><c:ptCount val="{len(values)}"/>{pts}</c:strCache></c:strRef>'


def _num_cache(values, fmt="General", ref="Sheet1!$B$2:$B$5") -> str:
    pts = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in enumerate(values) if v is not None)
    return f'<c:numRef><c:f>{ref}</c:f><c:numCache><c:formatCode>{fmt}</c:formatCode><c:ptCount val="{len(values)}"/>{pts}</c:numCache></c:numRef>'


def _ser(idx, name, cats, vals, fmt="General", cat_tag="cat", val_tag="val", extra="") -> str:
    cat = f"<c:{cat_tag}>{cats}</c:{cat_tag}>" if cats else ""
    return (
        f'<c:ser><c:idx val="{idx}"/><c:order val="{idx}"/>'
        f"<c:tx>{_str_cache([name], ref='Sheet1!$B$1')}</c:tx>"
        f"<c:spPr/>{cat}<c:{val_tag}>{_num_cache(vals, fmt)}</c:{val_tag}>{extra}"
        '<c:extLst><c:ext uri="{C3380CC4-5D6E-409C-BE32-E72D297353CC}"/></c:extLst></c:ser>'
    )


def _title(text: str) -> str:
    return f'<c:title><c:tx><c:rich><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></c:rich></c:tx><c:overlay val="0"/></c:title>'


def chart_space(plots: str, *, title: str = "", axes: str = "", date1904: bool = False) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f'<c:chartSpace xmlns:c="{C_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">'
        f'<c:date1904 val="{1 if date1904 else 0}"/><c:roundedCorners val="0"/>'
        f'<c:chart>{_title(title) if title else ""}<c:autoTitleDeleted val="{0 if title else 1}"/>'
        f'<c:plotArea><c:layout/>{plots}{axes}</c:plotArea><c:plotVisOnly val="1"/></c:chart>'
        '<c:externalData r:id="rId3"><c:autoUpdate val="0"/></c:externalData></c:chartSpace>'
    )


def _axis(tag: str, ax_id: int, pos: str, title: str = "") -> str:
    return f'<c:{tag}><c:axId val="{ax_id}"/><c:axPos val="{pos}"/>{_title(title) if title else ""}</c:{tag}>'


QUARTERS = _str_cache(["Q1", "Q2", "Q3", "Q4"])

REVENUE = chart_space(
    '<c:barChart><c:barDir val="col"/><c:grouping val="clustered"/><c:varyColors val="0"/>'
    + _ser(0, "2024", QUARTERS, [10, 11.5, 9, 12])
    + _ser(1, "2025", QUARTERS, [12, 13, 14.25, 15])
    + '<c:axId val="1"/><c:axId val="2"/></c:barChart>',
    title="Revenue by quarter",
    axes=_axis("catAx", 1, "b", "Quarter") + _axis("valAx", 2, "l", "Revenue ($M)"),
)


def _docx(body: str, charts: dict) -> bytes:
    """A Word package whose ``document.xml`` references ``charts`` (rId -> xml)."""
    rels = "".join(
        f'<Relationship Id="{rid}" Type="{REL_CHARTEX if "chartEx" in name else REL_CHART}" Target="charts/{name}"/>'
        for rid, (name, _) in charts.items()
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", document_xml(body))
        z.writestr(
            "word/_rels/document.xml.rels",
            f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>',
        )
        for name, xml in charts.values():
            z.writestr(f"word/charts/{name}", xml)
    return buf.getvalue()


def _chart_run(rid: str, name: str = "Chart 1", descr: str = "") -> str:
    alt = f' descr="{descr}"' if descr else ""
    return (
        f'<w:r><w:drawing><wp:inline xmlns:wp="{WP_NS}"><wp:extent cx="5486400" cy="3200400"/>'
        f'<wp:docPr id="1" name="{name}"{alt}/>'
        f'<a:graphic xmlns:a="{A_NS}"><a:graphicData uri="{C_NS}">'
        f'<c:chart xmlns:c="{C_NS}" xmlns:r="{R_NS}" r:id="{rid}"/></a:graphicData></a:graphic>'
        "</wp:inline></w:drawing></w:r>"
    )


def _chartex_run(rid: str) -> str:
    """How Word writes an Office 2016 chart: the chart for readers that know
    ``cx1``, a picture of it as the fallback for those that do not."""
    return (
        '<w:r><mc:AlternateContent><mc:Choice xmlns:cx1="http://schemas.microsoft.com/office/drawing/2015/9/8/chartex" Requires="cx1">'
        f'<w:drawing><wp:inline xmlns:wp="{WP_NS}"><wp:docPr id="2" name="Chart 2"/>'
        f'<a:graphic xmlns:a="{A_NS}"><a:graphicData uri="http://schemas.microsoft.com/office/drawing/2014/chartex">'
        f'<cx:chart xmlns:cx="{CX_NS}" xmlns:r="{R_NS}" r:id="{rid}"/></a:graphicData></a:graphic>'
        "</wp:inline></w:drawing></mc:Choice>"
        f'<mc:Fallback><w:drawing><wp:inline xmlns:wp="{WP_NS}"><wp:docPr id="3" name="Chart 2" descr="Chart type not supported"/>'
        "</wp:inline></w:drawing></mc:Fallback></mc:AlternateContent></w:r>"
    )


WATERFALL = (
    f'<cx:chartSpace xmlns:cx="{CX_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">'
    '<cx:chartData><cx:data id="0">'
    '<cx:strDim type="cat"><cx:f>Sheet1!$A$2:$A$5</cx:f><cx:lvl ptCount="4">'
    '<cx:pt idx="0">Start</cx:pt><cx:pt idx="1">Grants</cx:pt><cx:pt idx="2">Travel</cx:pt><cx:pt idx="3">End</cx:pt>'
    "</cx:lvl></cx:strDim>"
    '<cx:numDim type="val"><cx:f>Sheet1!$B$2:$B$5</cx:f><cx:lvl ptCount="4" formatCode="General">'
    '<cx:pt idx="0">100</cx:pt><cx:pt idx="1">40</cx:pt><cx:pt idx="2">-15</cx:pt><cx:pt idx="3">125</cx:pt>'
    "</cx:lvl></cx:numDim></cx:data></cx:chartData>"
    "<cx:chart><cx:title><cx:tx><cx:txData><cx:v>Budget bridge</cx:v></cx:txData></cx:tx></cx:title>"
    '<cx:plotArea><cx:plotAreaRegion><cx:series layoutId="waterfall" uniqueId="{1}">'
    "<cx:tx><cx:txData><cx:f>Sheet1!$B$1</cx:f><cx:v>Amount</cx:v></cx:txData></cx:tx>"
    '<cx:dataId val="0"/></cx:series></cx:plotAreaRegion></cx:plotArea></cx:chart></cx:chartSpace>'
)


# ---------------------------------------------------------------------------
# Reading one chart part
# ---------------------------------------------------------------------------


class TestReadChart:
    def test_a_clustered_column_chart_renders_as_a_wide_table(self):
        out = chart_markdown(read_chart(REVENUE.encode()))
        assert out == (
            "[chart: Revenue by quarter (column chart)]\n"
            "Category axis: Quarter · Value axis: Revenue ($M)\n"
            "\n"
            "| Quarter | 2024 | 2025 |\n"
            "|---|---|---|\n"
            "| Q1 | 10 | 12 |\n"
            "| Q2 | 11.5 | 13 |\n"
            "| Q3 | 9 | 14.25 |\n"
            "| Q4 | 12 | 15 |"
        )

    def test_the_one_line_form_carries_the_same_values(self):
        assert chart_line(read_chart(REVENUE.encode())) == (
            "[chart: Revenue by quarter (column chart). Category axis: Quarter, Value axis: Revenue ($M). "
            "2024: Q1 10, Q2 11.5, Q3 9, Q4 12. 2025: Q1 12, Q2 13, Q3 14.25, Q4 15]"
        )

    def test_a_percent_format_is_applied(self):
        # 0.45 under "0%" is what the chart shows as 45%; the raw number would be misread.
        xml = chart_space(
            '<c:pieChart><c:varyColors val="1"/>'
            + _ser(0, "Share", _str_cache(["North", "South", "West"]), [0.45, 0.305, 0.245], fmt="0.0%")
            + "</c:pieChart>"
        )
        out = chart_markdown(read_chart(xml.encode()))
        assert "| North | 45.0% |" in out and "| South | 30.5% |" in out
        assert out.startswith("[chart: Pie chart]")

    def test_float_noise_is_dropped(self):
        xml = chart_space("<c:lineChart>" + _ser(0, "S", QUARTERS, [0.30000000000000004, 1, 2, 3]) + "</c:lineChart>")
        assert "| Q1 | 0.3 |" in chart_markdown(read_chart(xml.encode()))

    def test_date_categories_are_dates(self):
        dates = _num_cache([45658, 45689], fmt="m/d/yyyy", ref="Sheet1!$A$2:$A$3")
        xml = chart_space("<c:lineChart>" + _ser(0, "Visits", dates, [120, 140]) + "</c:lineChart>")
        out = chart_markdown(read_chart(xml.encode()))
        assert "| 2025-01-01 | 120 |" in out and "| 2025-02-01 | 140 |" in out

    def test_a_1904_workbook_counts_dates_from_1904(self):
        dates = _num_cache([0], fmt="yyyy-mm-dd", ref="Sheet1!$A$2")
        xml = chart_space("<c:lineChart>" + _ser(0, "S", dates, [1]) + "</c:lineChart>", date1904=True)
        assert "| 1904-01-01 | 1 |" in chart_markdown(read_chart(xml.encode()))

    def test_a_red_negative_format_is_not_a_date(self):
        xml = chart_space("<c:barChart>" + _ser(0, "Net", QUARTERS, [5, -3, 2, 1], fmt="#,##0;[Red]-#,##0") + "</c:barChart>")
        assert "| Q2 | -3 |" in chart_markdown(read_chart(xml.encode()))

    def test_a_missing_point_is_a_blank_cell_in_its_own_row(self):
        xml = chart_space("<c:lineChart>" + _ser(0, "S", QUARTERS, [1, None, 3, 4]) + "</c:lineChart>")
        out = chart_markdown(read_chart(xml.encode()))
        assert "| Q2 |  |" in out and "| Q3 | 3 |" in out

    def test_a_combo_chart_names_both_kinds(self):
        xml = chart_space(
            '<c:barChart><c:barDir val="col"/><c:grouping val="stacked"/>'
            + _ser(0, "Cost", QUARTERS, [1, 2, 3, 4])
            + "</c:barChart><c:lineChart>"
            + _ser(1, "Target", QUARTERS, [2, 2, 3, 3])
            + "</c:lineChart>",
            title="Cost vs target",
        )
        out = chart_markdown(read_chart(xml.encode()))
        assert out.startswith("[chart: Cost vs target (stacked column and line chart)]")
        assert "| Quarter | Cost | Target |" not in out  # no category axis title here
        assert "| Category | Cost | Target |" in out

    def test_a_horizontal_bar_chart_is_a_bar_chart(self):
        xml = chart_space('<c:barChart><c:barDir val="bar"/>' + _ser(0, "S", QUARTERS, [1, 2, 3, 4]) + "</c:barChart>")
        assert chart_markdown(read_chart(xml.encode())).startswith("[chart: Bar chart]")

    def test_a_scatter_chart_with_different_x_per_series_is_a_long_table(self):
        xml = chart_space(
            "<c:scatterChart>"
            + _ser(0, "Lab A", _num_cache([1, 2], ref="X1"), [10, 20], cat_tag="xVal", val_tag="yVal")
            + _ser(1, "Lab B", _num_cache([1.5], ref="X2"), [12], cat_tag="xVal", val_tag="yVal")
            + "</c:scatterChart>",
            axes=_axis("valAx", 1, "b", "Dose (mg)") + _axis("valAx", 2, "l", "Response"),
        )
        out = chart_markdown(read_chart(xml.encode()))
        assert "X axis: Dose (mg) · Y axis: Response" in out
        assert "| Series | X | Y |" in out
        assert "| Lab A | 2 | 20 |" in out and "| Lab B | 1.5 | 12 |" in out

    def test_a_bubble_chart_keeps_its_sizes(self):
        xml = chart_space(
            "<c:bubbleChart>"
            + _ser(
                0,
                "Sites",
                _num_cache([1, 2], ref="X"),
                [3, 4],
                cat_tag="xVal",
                val_tag="yVal",
                extra=f"<c:bubbleSize>{_num_cache([30, 50], ref='S')}</c:bubbleSize>",
            )
            + "</c:bubbleChart>"
        )
        out = chart_markdown(read_chart(xml.encode()))
        assert "| Series | X | Y | Size |" in out and "| Sites | 2 | 4 | 50 |" in out

    def test_multi_level_categories_join_outer_and_inner(self):
        multi = (
            '<c:multiLvlStrRef><c:f>A2:B5</c:f><c:multiLvlStrCache><c:ptCount val="4"/>'
            '<c:lvl><c:pt idx="0"><c:v>Q1</c:v></c:pt><c:pt idx="1"><c:v>Q2</c:v></c:pt>'
            '<c:pt idx="2"><c:v>Q1</c:v></c:pt><c:pt idx="3"><c:v>Q2</c:v></c:pt></c:lvl>'
            '<c:lvl><c:pt idx="0"><c:v>2024</c:v></c:pt><c:pt idx="2"><c:v>2025</c:v></c:pt></c:lvl>'
            "</c:multiLvlStrCache></c:multiLvlStrRef>"
        )
        xml = chart_space("<c:barChart>" + _ser(0, "Sales", multi, [1, 2, 3, 4]) + "</c:barChart>")
        out = chart_markdown(read_chart(xml.encode()))
        assert "| 2024 / Q2 | 2 |" in out and "| 2025 / Q1 | 3 |" in out

    def test_an_office_2016_waterfall_is_read(self):
        out = chart_markdown(read_chart(WATERFALL.encode()))
        assert out.startswith("[chart: Budget bridge (waterfall chart)]")
        assert "| Travel | -15 |" in out and "| End | 125 |" in out

    def test_a_chart_without_saved_data_says_so(self):
        xml = chart_space('<c:barChart><c:barDir val="col"/></c:barChart>', title="Empty")
        assert chart_markdown(read_chart(xml.encode())) == "[chart: Empty (column chart)]\n(The chart has no data saved with it.)"

    def test_a_long_series_is_cut_and_says_so(self):
        n = office_charts.MAX_ROWS + 50
        cats = _str_cache([f"d{i}" for i in range(n)])
        xml = chart_space("<c:lineChart>" + _ser(0, "S", cats, list(range(n))) + "</c:lineChart>")
        out = chart_markdown(read_chart(xml.encode()))
        assert f"first {office_charts.MAX_ROWS} of {n} points" in out.splitlines()[0]
        assert out.count("\n| d") == office_charts.MAX_ROWS
        assert len(chart_line(read_chart(xml.encode()))) < 2_000

    def test_a_pipe_in_a_label_is_escaped(self):
        xml = chart_space("<c:barChart>" + _ser(0, "A|B", _str_cache(["x|y"]), [1]) + "</c:barChart>")
        assert "| x\\|y | 1 |" in chart_markdown(read_chart(xml.encode()))

    @pytest.mark.parametrize("raw", [b"not xml", b"<other/>", f'<c:chartSpace xmlns:c="{C_NS}"/>'.encode()])
    def test_bytes_that_are_not_a_chart_read_as_none(self, raw):
        assert read_chart(raw) is None
        assert chart_markdown(None, alt="Sales") == "[chart: Sales]"

    def test_rendering_is_deterministic(self):
        assert chart_markdown(read_chart(REVENUE.encode())) == chart_markdown(read_chart(REVENUE.encode()))


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------


class TestWord:
    def test_a_chart_renders_after_the_paragraph_that_anchors_it(self):
        raw = _docx(
            paragraph(run("Results are below.")) + paragraph(_chart_run("rId5")) + paragraph(run("End.")),
            {"rId5": ("chart1.xml", REVENUE)},
        )
        out = docx_to_markdown(raw)
        assert out.startswith("Results are below.\n\n[chart: Revenue by quarter (column chart)]")
        assert "| Q3 | 9 | 14.25 |" in out
        assert out.endswith("| Q4 | 12 | 15 |\n\nEnd.")
        assert "[image" not in out

    def test_a_chart_in_a_text_paragraph_keeps_the_text(self):
        raw = _docx(paragraph(run("See chart: "), _chart_run("rId5")), {"rId5": ("chart1.xml", REVENUE)})
        out = docx_to_markdown(raw)
        assert out.startswith("See chart:\n\n[chart: Revenue by quarter")

    def test_a_chart_in_a_table_cell_is_one_line(self):
        cell = f"<w:tc>{paragraph(_chart_run('rId5'))}</w:tc>"
        raw = _docx(f"<w:tbl><w:tr><w:tc>{paragraph(run('Revenue'))}</w:tc>{cell}</w:tr></w:tbl>", {"rId5": ("chart1.xml", REVENUE)})
        out = docx_to_markdown(raw)
        assert out.startswith("| Revenue | [chart: Revenue by quarter (column chart). ")
        assert "2025: Q1 12, Q2 13, Q3 14.25, Q4 15]" in out
        assert out.count("\n") == 1  # header row and separator only: the chart did not break the table

    def test_an_office_2016_chart_reads_its_choice_not_its_fallback_picture(self):
        raw = _docx(paragraph(_chartex_run("rId7")), {"rId7": ("chartEx1.xml", WATERFALL)})
        out = docx_to_markdown(raw)
        assert out.startswith("[chart: Budget bridge (waterfall chart)]")
        assert "not supported" not in out and "[image" not in out

    def test_a_text_box_is_read_once(self):
        # Word saves a text box as a drawing (Choice) and as VML (Fallback) with the same text.
        box = (
            '<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing>'
            f'<wp:anchor xmlns:wp="{WP_NS}"><wp:docPr id="4" name="Text Box 4"/>'
            "<w:txbxContent><w:p><w:r><w:t>Sidebar note</w:t></w:r></w:p></w:txbxContent>"
            "</wp:anchor></w:drawing></mc:Choice><mc:Fallback><w:pict>"
            "<w:txbxContent><w:p><w:r><w:t>Sidebar note</w:t></w:r></w:p></w:txbxContent>"
            "</w:pict></mc:Fallback></mc:AlternateContent></w:r>"
        )
        out = docx_to_markdown(_docx(paragraph(box), {}))
        assert out.count("Sidebar note") == 1

    def test_a_chart_whose_part_is_missing_keeps_its_alt_text(self):
        raw = _docx(paragraph(_chart_run("rId9", descr="Enrollment by term")), {})
        assert docx_to_markdown(raw) == "[chart: Enrollment by term]"

    def test_a_chart_without_a_title_is_titled_by_its_alt_text(self):
        xml = chart_space("<c:lineChart>" + _ser(0, "S", QUARTERS, [1, 2, 3, 4]) + "</c:lineChart>")
        raw = _docx(paragraph(_chart_run("rId5", descr="Visits per quarter")), {"rId5": ("chart1.xml", xml)})
        assert docx_to_markdown(raw).startswith("[chart: Visits per quarter (line chart)]")

    def test_a_picture_is_still_an_image(self):
        pic = f'<w:r><w:drawing><wp:inline xmlns:wp="{WP_NS}"><wp:docPr id="1" name="Picture 1" descr="Logo"/></wp:inline></w:drawing></w:r>'
        assert docx_to_markdown(_docx(paragraph(pic), {})) == "[image: Logo]"


class TestWordParagraphIndex:
    def test_the_chart_is_on_its_anchor_paragraph(self):
        raw = _docx(
            paragraph(run("Results are below.")) + paragraph(_chart_run("rId5")) + paragraph(run("End.")),
            {"rId5": ("chart1.xml", REVENUE)},
        )
        paragraphs = docx_paragraphs(raw)
        assert paragraphs[0] == "Results are below."
        assert paragraphs[1].startswith("[chart: Revenue by quarter (column chart). ")
        assert "2025: Q1 12, Q2 13, Q3 14.25, Q4 15]" in paragraphs[1]
        assert paragraphs[2] == "End."

    def test_text_and_chart_share_the_paragraph(self):
        raw = _docx(paragraph(run("See chart:"), _chart_run("rId5")), {"rId5": ("chart1.xml", REVENUE)})
        (only,) = docx_paragraphs(raw)
        assert only.startswith("See chart: [chart: Revenue by quarter")

    def test_a_document_without_charts_reads_as_before(self):
        raw = _docx(paragraph(run("One.")) + paragraph(run("Two.")), {})
        assert docx_paragraphs(raw) == ["One.", "Two."]

    def test_an_office_2016_chart_is_indexed_once(self):
        raw = _docx(paragraph(_chartex_run("rId7")), {"rId7": ("chartEx1.xml", WATERFALL)})
        (only,) = docx_paragraphs(raw)
        assert only.count("[chart:") == 1 and "Travel -15" in only


# ---------------------------------------------------------------------------
# PowerPoint: markers from the sandbox, filled on the host
# ---------------------------------------------------------------------------


def _pptx(charts: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        for name, xml in charts.items():
            z.writestr(name, xml)
    return buf.getvalue()


def _marker(part: str, alt: str = "") -> str:
    return f"{CHART_MARKER_OPEN}{part}|{alt}{CHART_MARKER_CLOSE}"


class TestPowerPointMarkers:
    def test_a_marker_becomes_the_chart(self):
        deck = _pptx({"ppt/charts/chart1.xml": REVENUE})
        text = f"## Slide 1\nRevenue\n{_marker('/ppt/charts/chart1.xml')}\n"
        out = fill_chart_markers(text, deck)
        assert out.startswith("## Slide 1\nRevenue\n[chart: Revenue by quarter (column chart)]\n")
        assert "| Q4 | 12 | 15 |" in out

    def test_a_marker_for_a_missing_part_keeps_its_alt_text(self):
        out = fill_chart_markers(_marker("/ppt/charts/chart9.xml", "Pipeline"), _pptx({}))
        assert out == "[chart: Pipeline]"

    def test_a_marker_cannot_read_a_part_that_is_not_a_chart(self):
        deck = _pptx({"ppt/slides/slide1.xml": REVENUE})
        assert fill_chart_markers(_marker("/ppt/slides/slide1.xml"), deck) == "[chart]"

    def test_text_without_markers_is_unchanged(self):
        assert fill_chart_markers("## Slide 1\nHello", b"not a zip") == "## Slide 1\nHello"

    def test_markers_in_a_package_that_does_not_open_become_plain_markers(self):
        assert fill_chart_markers(_marker("/ppt/charts/chart1.xml"), b"not a zip") == "[chart]"


class TestPowerPointTool:
    def test_the_extraction_script_compiles_and_names_the_marker(self):
        from agents.builtin_tools import powerpoint_presentation_tool as tool

        script = tool._PPTX_TEXT_EXTRACTION.replace("__SOURCE__", repr("/tmp/deck.pptx"))
        compile(script, "<sandbox>", "exec")
        assert repr(CHART_MARKER_OPEN) in script and repr(CHART_MARKER_CLOSE) in script

    def test_the_tool_fills_markers_from_the_deck_it_read(self, monkeypatch):
        from agents.builtin_tools import powerpoint_presentation_tool as tool

        deck = _pptx({"ppt/charts/chart1.xml": REVENUE})

        class _Interpreter:
            def __init__(self, region):
                pass

            def start(self, identifier):
                pass

            def stop(self):
                pass

        import bedrock_agentcore.tools.code_interpreter_client as client

        monkeypatch.setattr(client, "CodeInterpreter", _Interpreter)
        monkeypatch.setattr(tool, "_ci_write_bytes", lambda ci, path, data: None)
        monkeypatch.setattr(tool, "_ci_exec", lambda ci, code: f"## Slide 1\n{_marker('/ppt/charts/chart1.xml')}\n")
        out = tool._extract_pptx_text("ci-test", deck)
        assert out.startswith("## Slide 1\n[chart: Revenue by quarter (column chart)]")
        assert "| Q2 | 11.5 | 13 |" in out
