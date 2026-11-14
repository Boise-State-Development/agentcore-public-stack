"""A Word or PowerPoint chart read from the data the file saves with it.

A native Office chart is not a picture. Its part (``word/charts/chart1.xml``,
``ppt/charts/chart3.xml``) carries the series names, the categories and every
plotted value in a cache (``c:strCache`` / ``c:numCache``) that Office writes
so the chart can be drawn without opening its embedded workbook. Reading that
cache gives the model the numbers the chart shows, which a picture of it only
lets a vision model estimate and the text extractors used to drop entirely
(Word wrote ``[image: Chart 1]``; PowerPoint wrote nothing).

Two renderings of one :class:`Chart`:

* :func:`chart_markdown` — a marker line, the axis titles and a markdown table,
  for the reading renderers (``docx_markdown``, ``read_powerpoint_presentation``);
* :func:`chart_line` — the same facts on one line, for ``docx_paragraphs`` (the
  paragraph index ``pattern`` mode searches, which must stay one string per
  paragraph) and for a chart inside a table cell.

Classic charts (``c:chartSpace``: bar, column, line, pie, area, scatter,
bubble, radar, stock, surface, combinations of them) and the Office 2016 chart
types (``cx:chartSpace``: waterfall, treemap, sunburst, funnel, histogram, box
and whisker, Pareto, map) are both read. Values are shown as the chart's own
number format would show them only for the formats that change their meaning
(percentages and dates); anything else is the stored number, without float
noise. Each chart is bounded (:data:`MAX_ROWS` rows, :data:`MAX_SERIES` series),
and says so when it is cut.

Pure stdlib (zipfile + ElementTree) and deterministic: the same bytes always
render the same text, which is what lets a rendered attachment sit in the
cacheable prefix. A part that does not parse yields ``None``, never an
exception, so a broken chart costs its data and nothing else.
"""

from __future__ import annotations

import datetime
import posixpath
import re
import zipfile
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree

_C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
_CX = "{http://schemas.microsoft.com/office/drawing/2014/chartex}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

#: Element tags that reference a chart part from a drawing, by relationship id.
CHART_REFERENCE_TAGS = frozenset({f"{_C}chart", f"{_CX}chart"})

#: Rows of one chart's table (categories, or points for a scatter chart).
MAX_ROWS = 100
#: Series (value columns) of one chart's table.
MAX_SERIES = 12
#: Characters of one category label or series name.
_MAX_LABEL = 80

_PLOT_KINDS: Dict[str, str] = {
    "barChart": "bar",
    "bar3DChart": "3-D bar",
    "lineChart": "line",
    "line3DChart": "3-D line",
    "pieChart": "pie",
    "pie3DChart": "3-D pie",
    "ofPieChart": "pie of pie",
    "doughnutChart": "doughnut",
    "areaChart": "area",
    "area3DChart": "3-D area",
    "scatterChart": "scatter",
    "bubbleChart": "bubble",
    "radarChart": "radar",
    "stockChart": "stock",
    "surfaceChart": "surface",
    "surface3DChart": "3-D surface",
}
_XY_PLOTS = frozenset({"scatterChart", "bubbleChart"})

_CHARTEX_KINDS: Dict[str, str] = {
    "waterfall": "waterfall",
    "treemap": "treemap",
    "sunburst": "sunburst",
    "funnel": "funnel",
    "clusteredColumn": "histogram",
    "paretoLine": "Pareto line",
    "boxWhisker": "box and whisker",
    "regionMap": "map",
}

#: Quoted literals, escaped characters and bracketed sections (``[Red]``,
#: ``[$-409]``) of a number format, none of which say what kind of number it is.
_QUOTED = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]')
_DECIMALS = re.compile(r"\.(0+)")


@dataclass(frozen=True)
class Series:
    """One plotted series: its name, its category labels (x values for a
    scatter chart) and its values (y values), aligned by index."""

    name: str
    categories: Tuple[str, ...]
    values: Tuple[str, ...]
    sizes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Chart:
    title: str
    kind: str
    series: Tuple[Series, ...]
    #: ``(label, title)`` per titled axis, in the order the file lists them.
    axes: Tuple[Tuple[str, str], ...] = ()
    xy: bool = False
    note: str = ""


# ---------------------------------------------------------------------------
# Finding chart parts
# ---------------------------------------------------------------------------


def _rels_name(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", f"{name}.rels")


def chart_targets(archive: zipfile.ZipFile, part: str) -> Dict[str, str]:
    """Relationship id -> chart part name, for the charts ``part`` references
    (``word/document.xml``, ``ppt/slides/slide2.xml``). Empty when the part has
    no relationships or none of them is a chart."""
    try:
        xml = archive.read(_rels_name(part))
        root = ElementTree.fromstring(xml)
    except (KeyError, ElementTree.ParseError, zipfile.BadZipFile):
        return {}
    folder = posixpath.dirname(part)
    targets: Dict[str, str] = {}
    for rel in root.iter(f"{_REL}Relationship"):
        rtype = rel.get("Type") or ""
        if rel.get("TargetMode") == "External" or not (rtype.endswith("/chart") or rtype.endswith("/chartEx")):
            continue
        target = rel.get("Target") or ""
        name = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        if rel.get("Id"):
            targets[rel.get("Id") or ""] = name
    return targets


def chart_reference(node: ElementTree.Element) -> Optional[str]:
    """The relationship id of the first chart a drawing element holds, or
    ``None`` when it holds none (a picture, a shape, a text box)."""
    for child in node.iter():
        if child.tag in CHART_REFERENCE_TAGS:
            return child.get(f"{_R}id")
    return None


def load_charts(archive: zipfile.ZipFile, part: str) -> Dict[str, Optional["Chart"]]:
    """Relationship id -> parsed chart (``None`` when its part is missing or
    unreadable), for every chart ``part`` references."""
    names = set(archive.namelist())
    charts: Dict[str, Optional[Chart]] = {}
    for rid, name in chart_targets(archive, part).items():
        charts[rid] = _read_member(archive, name) if name in names else None
    return charts


def _read_member(archive: zipfile.ZipFile, name: str) -> Optional["Chart"]:
    try:
        return read_chart(archive.read(name))
    except (zipfile.BadZipFile, KeyError, OSError, EOFError):
        return None


# ---------------------------------------------------------------------------
# Reading one chart part
# ---------------------------------------------------------------------------


def read_chart(xml: bytes) -> Optional[Chart]:
    """A chart part's title, kind, axes and cached series, or ``None`` when the
    bytes are not a chart part this module can read."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    try:
        if root.tag == f"{_C}chartSpace":
            return _read_classic(root)
        if root.tag == f"{_CX}chartSpace":
            return _read_chartex(root)
    except (ValueError, TypeError):
        return None
    return None


def _clip(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= _MAX_LABEL else text[: _MAX_LABEL - 1].rstrip() + "…"


def _rich_text(node: Optional[ElementTree.Element]) -> str:
    if node is None:
        return ""
    paragraphs = []
    for para in node.iter(f"{_A}p"):
        paragraphs.append("".join(t.text or "" for t in para.iter(f"{_A}t")))
    return _clip(" ".join(p for p in paragraphs if p.strip()))


def _is_on(node: Optional[ElementTree.Element]) -> bool:
    return node is not None and (node.get("val") or "1") in ("1", "true")


# -- number formats ---------------------------------------------------------


def _format_value(raw: str, fmt: str, date1904: bool) -> str:
    raw = raw.strip()
    try:
        number = float(raw)
    except ValueError:
        return _clip(raw)
    if number != number or number in (float("inf"), float("-inf")):
        return raw
    plain = _QUOTED.sub("", fmt or "")
    if "%" in plain:
        decimals = _DECIMALS.search(plain.split("%")[0])
        places = len(decimals.group(1)) if decimals else 0
        return f"{number * 100:.{places}f}%"
    lowered = plain.lower()
    if plain and lowered != "general" and ("y" in lowered or "d" in lowered) and ("m" in lowered or "y" in lowered):
        base = datetime.date(1904, 1, 1) if date1904 else datetime.date(1899, 12, 30)
        try:
            return (base + datetime.timedelta(days=int(number))).isoformat()
        except OverflowError:
            return raw
    return _number(number)


def _number(number: float) -> str:
    """The stored number without binary float noise (``0.30000000000000004``
    reads ``0.3``)."""
    return format(number, ".15g")


# -- classic charts ----------------------------------------------------------


def _cache_points(cache: Optional[ElementTree.Element], ns: str, date1904: bool) -> List[str]:
    """A ``strCache`` / ``numCache`` / literal as a list indexed by point."""
    if cache is None:
        return []
    fmt_node = cache.find(f"{ns}formatCode")
    fmt = (fmt_node.text or "") if fmt_node is not None else ""
    count_node = cache.find(f"{ns}ptCount")
    points: Dict[int, str] = {}
    for pt in cache.findall(f"{ns}pt"):
        idx = int(pt.get("idx") or 0)
        value = pt.find(f"{ns}v")
        pt_fmt = pt.get("formatCode") or fmt
        text = (value.text or "") if value is not None else ""
        points[idx] = _format_value(text, pt_fmt, date1904) if cache.tag.endswith(("numCache", "numLit")) else _clip(text)
    count = int(count_node.get("val") or 0) if count_node is not None else 0
    count = max(count, max(points) + 1 if points else 0)
    return [points.get(i, "") for i in range(count)]


def _multi_level(cache: ElementTree.Element) -> List[str]:
    """Hierarchical categories, outermost first: ``"2025 / Q1"``. The first
    ``lvl`` is the innermost; an outer label covers its point and the points
    after it up to the next label of that level."""
    levels = cache.findall(f"{_C}lvl")
    count_node = cache.find(f"{_C}ptCount")
    count = int(count_node.get("val") or 0) if count_node is not None else 0
    filled: List[List[str]] = []
    for depth, level in enumerate(levels):
        points = {int(pt.get("idx") or 0): _clip(pt.findtext(f"{_C}v") or "") for pt in level.findall(f"{_C}pt")}
        count = max(count, max(points) + 1 if points else 0)
        row: List[str] = []
        current = ""
        for i in range(count):
            if i in points:
                current = points[i]
            elif depth == 0:
                current = ""
            row.append(current)
        filled.append(row)
    return [" / ".join(lvl[i] for lvl in reversed(filled) if i < len(lvl) and lvl[i]) for i in range(count)]


def _data_source(node: Optional[ElementTree.Element], date1904: bool) -> List[str]:
    """``c:cat`` / ``c:val`` / ``c:xVal`` / ``c:yVal`` / ``c:bubbleSize``."""
    if node is None:
        return []
    for ref, cache in (("strRef", "strCache"), ("numRef", "numCache")):
        found = node.find(f"{_C}{ref}/{_C}{cache}")
        if found is not None:
            return _cache_points(found, _C, date1904)
    multi = node.find(f"{_C}multiLvlStrRef/{_C}multiLvlStrCache")
    if multi is not None:
        return _multi_level(multi)
    for literal in ("strLit", "numLit"):
        found = node.find(f"{_C}{literal}")
        if found is not None:
            return _cache_points(found, _C, date1904)
    return []


def _series_name(ser: ElementTree.Element, index: int) -> str:
    tx = ser.find(f"{_C}tx")
    if tx is not None:
        cached = tx.findtext(f"{_C}strRef/{_C}strCache/{_C}pt/{_C}v")
        if cached and cached.strip():
            return _clip(cached)
        literal = tx.findtext(f"{_C}v")
        if literal and literal.strip():
            return _clip(literal)
    return f"Series {index + 1}"


def _plot_kind(plot: ElementTree.Element) -> str:
    local = plot.tag[len(_C) :]
    kind = _PLOT_KINDS.get(local, local)
    if local in ("barChart", "bar3DChart"):
        direction = plot.find(f"{_C}barDir")
        if direction is not None and direction.get("val") == "col":
            kind = kind.replace("bar", "column")
    grouping = plot.find(f"{_C}grouping")
    value = grouping.get("val") if grouping is not None else ""
    if value == "stacked":
        kind = f"stacked {kind}"
    elif value == "percentStacked":
        kind = f"100% stacked {kind}"
    return kind


def _classic_title(chart: ElementTree.Element) -> str:
    title = chart.find(f"{_C}title")
    if title is None:
        return ""
    rich = title.find(f"{_C}tx/{_C}rich")
    if rich is not None:
        return _rich_text(rich)
    cached = title.findtext(f"{_C}tx/{_C}strRef/{_C}strCache/{_C}pt/{_C}v")
    return _clip(cached or "")


def _axis_titles(plot_area: ElementTree.Element, xy: bool) -> Tuple[Tuple[str, str], ...]:
    found: List[Tuple[str, str]] = []
    for axis in plot_area:
        local = axis.tag[len(_C) :] if axis.tag.startswith(_C) else ""
        if local not in ("catAx", "valAx", "dateAx", "serAx"):
            continue
        title = axis.find(f"{_C}title")
        if title is None:
            continue
        text = _rich_text(title.find(f"{_C}tx/{_C}rich")) or _clip(title.findtext(f"{_C}tx/{_C}strRef/{_C}strCache/{_C}pt/{_C}v") or "")
        if not text:
            continue
        ax_pos = axis.find(f"{_C}axPos")
        position = (ax_pos.get("val") if ax_pos is not None else "") or ""
        if xy and local == "valAx":
            label = "X axis" if position in ("b", "t") else "Y axis"
        elif local == "serAx":
            label = "Depth axis"
        elif local == "valAx":
            label = "Value axis"
        else:
            label = "Category axis"
        found.append((label, text))
    return tuple(found)


def _read_classic(root: ElementTree.Element) -> Chart:
    date1904 = _is_on(root.find(f"{_C}date1904"))
    chart = root.find(f"{_C}chart")
    if chart is None:
        raise ValueError("no c:chart")
    plot_area = chart.find(f"{_C}plotArea")
    kinds: List[str] = []
    series: List[Series] = []
    xy = False
    if plot_area is not None:
        for plot in plot_area:
            local = plot.tag[len(_C) :] if plot.tag.startswith(_C) else ""
            if local not in _PLOT_KINDS:
                continue
            kind = _plot_kind(plot)
            if kind not in kinds:
                kinds.append(kind)
            is_xy = local in _XY_PLOTS
            xy = xy or is_xy
            for ser in plot.findall(f"{_C}ser"):
                cats = _data_source(ser.find(f"{_C}xVal" if is_xy else f"{_C}cat"), date1904)
                vals = _data_source(ser.find(f"{_C}yVal" if is_xy else f"{_C}val"), date1904)
                sizes = _data_source(ser.find(f"{_C}bubbleSize"), date1904) if local == "bubbleChart" else []
                series.append(Series(_series_name(ser, len(series)), tuple(cats), tuple(vals), tuple(sizes)))
    title = _classic_title(chart)
    if not title and chart.find(f"{_C}title") is not None and len(series) == 1:
        # Office titles a one-series chart with no title text by the series name.
        title = series[0].name
    axes = _axis_titles(plot_area, xy) if plot_area is not None else ()
    return Chart(title=title, kind=" and ".join(kinds) or "chart", series=tuple(series), axes=axes, xy=xy)


# -- Office 2016 charts (chartex) ------------------------------------------


def _chartex_dimension(dim: ElementTree.Element, numeric: bool) -> List[str]:
    levels = dim.findall(f"{_CX}lvl")
    if not levels:
        return []
    if numeric or len(levels) == 1:
        level = levels[0]
        fmt = level.get("formatCode") or ""
        count = int(level.get("ptCount") or 0)
        points: Dict[int, str] = {}
        for pt in level.findall(f"{_CX}pt"):
            text = pt.text or ""
            points[int(pt.get("idx") or 0)] = _format_value(text, fmt, False) if numeric else _clip(text)
        count = max(count, max(points) + 1 if points else 0)
        return [points.get(i, "") for i in range(count)]
    # Hierarchical categories (treemap, sunburst): first level innermost, as
    # in a classic multi-level cache; outer labels fill forward.
    filled: List[List[str]] = []
    count = max(int(level.get("ptCount") or 0) for level in levels)
    for depth, level in enumerate(levels):
        points = {int(pt.get("idx") or 0): _clip(pt.text or "") for pt in level.findall(f"{_CX}pt")}
        row: List[str] = []
        current = ""
        for i in range(count):
            if i in points:
                current = points[i]
            elif depth == 0:
                current = ""
            row.append(current)
        filled.append(row)
    return [" / ".join(lvl[i] for lvl in reversed(filled) if lvl[i]) for i in range(count)]


def _read_chartex(root: ElementTree.Element) -> Chart:
    data: Dict[str, Tuple[List[str], List[str]]] = {}
    chart_data = root.find(f"{_CX}chartData")
    if chart_data is not None:
        for block in chart_data.findall(f"{_CX}data"):
            cats: List[str] = []
            vals: List[str] = []
            for dim in block:
                if dim.tag == f"{_CX}strDim" and dim.get("type") == "cat":
                    cats = _chartex_dimension(dim, numeric=False)
                elif dim.tag == f"{_CX}numDim" and dim.get("type") in ("val", "size"):
                    if not vals:
                        vals = _chartex_dimension(dim, numeric=True)
                elif dim.tag == f"{_CX}numDim" and dim.get("type") == "cat" and not cats:
                    cats = _chartex_dimension(dim, numeric=True)
            data[block.get("id") or ""] = (cats, vals)
    chart = root.find(f"{_CX}chart")
    if chart is None:
        raise ValueError("no cx:chart")
    title_node = chart.find(f"{_CX}title")
    title = ""
    if title_node is not None:
        title = _clip(title_node.findtext(f"{_CX}tx/{_CX}txData/{_CX}v") or "") or _rich_text(title_node.find(f"{_CX}tx/{_CX}rich"))
    kinds: List[str] = []
    series: List[Series] = []
    for ser in chart.iter(f"{_CX}series"):
        layout = ser.get("layoutId") or ""
        kind = _CHARTEX_KINDS.get(layout, layout or "chart")
        if kind not in kinds:
            kinds.append(kind)
        if layout == "paretoLine":
            # Office computes the cumulative line from the bars; it has no data of its own.
            continue
        name = _clip(ser.findtext(f"{_CX}tx/{_CX}txData/{_CX}v") or "") or f"Series {len(series) + 1}"
        data_id = ser.find(f"{_CX}dataId")
        cats, vals = data.get(data_id.get("val") if data_id is not None else "", ([], []))
        series.append(Series(name, tuple(cats), tuple(vals)))
    note = ""
    if "histogram" in kinds or "box and whisker" in kinds:
        note = "raw values; Office computes the bins or quartiles when it draws the chart"
    return Chart(title=title, kind=" and ".join(kinds) or "chart", series=tuple(series), note=note)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _escape(text: str) -> str:
    return text.replace("|", "\\|")


def _header(chart: Chart, alt: str) -> str:
    title = chart.title or _clip(alt)
    kind = f"{chart.kind} chart" if not chart.kind.endswith("chart") else chart.kind
    lead = f"{title} ({kind})" if title else kind[0].upper() + kind[1:]
    return lead


def _shared_categories(series: Iterable[Series]) -> Optional[Tuple[str, ...]]:
    """The one category list every series with categories uses, ``()`` when
    no series has categories, or ``None`` when they differ. A series without
    categories takes the shared list."""
    shared: Tuple[str, ...] = ()
    for s in series:
        if not s.categories:
            continue
        if not shared:
            shared = s.categories
        elif s.categories != shared:
            return None
    return shared


def _point_count(series: Series) -> int:
    return max(len(series.values), len(series.categories), len(series.sizes))


def chart_markdown(chart: Optional[Chart], *, alt: str = "") -> str:
    """A marker line, the axis titles and a markdown table of the chart's
    saved data. ``alt`` (the drawing's alt text) titles a chart that has no
    title of its own. ``None`` (a chart part that could not be read) renders
    as the marker alone."""
    if chart is None:
        label = _clip(alt)
        return f"[chart: {label}]" if label else "[chart]"
    lines = [f"[chart: {_header(chart, alt)}]"]
    if chart.axes:
        lines.append(" · ".join(f"{label}: {title}" for label, title in chart.axes))
    series = [s for s in chart.series if s.values or s.categories]
    if not series:
        lines.append("(The chart has no data saved with it.)")
        return "\n".join(lines)
    notes: List[str] = []
    if chart.note:
        notes.append(chart.note)
    if len(series) > MAX_SERIES:
        notes.append(f"first {MAX_SERIES} of {len(series)} series")
        series = series[:MAX_SERIES]
    shared = _shared_categories(series)
    has_sizes = any(s.sizes for s in series)
    table: List[List[str]]
    if shared is not None and not has_sizes:
        first = (_axis_label(chart, "X axis" if chart.xy else "Category axis") or ("X" if chart.xy else "Category")) if shared else "Point"
        header = [first] + [s.name for s in series]
        rows = max(len(shared), max(len(s.values) for s in series))
        if rows > MAX_ROWS:
            notes.append(f"first {MAX_ROWS} of {rows} points")
        table = [header]
        for i in range(min(rows, MAX_ROWS)):
            label = shared[i] if i < len(shared) else str(i + 1)
            table.append([label] + [s.values[i] if i < len(s.values) else "" for s in series])
    else:
        x_label = "X" if chart.xy else "Category"
        header = ["Series", x_label, "Y" if chart.xy else "Value"] + (["Size"] if has_sizes else [])
        table = [header]
        total = sum(_point_count(s) for s in series)
        if total > MAX_ROWS:
            notes.append(f"first {MAX_ROWS} of {total} points")
        for s in series:
            for i in range(_point_count(s)):
                if len(table) > MAX_ROWS:
                    break
                row = [
                    s.name,
                    s.categories[i] if i < len(s.categories) else str(i + 1),
                    s.values[i] if i < len(s.values) else "",
                ]
                if has_sizes:
                    row.append(s.sizes[i] if i < len(s.sizes) else "")
                table.append(row)
    if notes:
        lines[0] = lines[0][:-1] + f"; {'; '.join(notes)}]"
    lines.append("")
    width = len(table[0])
    for n, row in enumerate(table):
        lines.append("| " + " | ".join(_escape(c) for c in row + [""] * (width - len(row))) + " |")
        if n == 0:
            lines.append("|" + "|".join(["---"] * width) + "|")
    return "\n".join(lines)


def _axis_label(chart: Chart, label: str) -> str:
    for name, title in chart.axes:
        if name == label:
            return title
    return ""


def chart_line(chart: Optional[Chart], *, alt: str = "") -> str:
    """The chart's facts on one line: ``[chart: Title (column chart).
    2024: Q1 10, Q2 12. 2025: Q1 11, Q2 13]``. Same bounds as
    :func:`chart_markdown`."""
    if chart is None:
        label = _clip(alt)
        return f"[chart: {label}]" if label else "[chart]"
    parts = [_header(chart, alt)]
    if chart.axes:
        parts.append(", ".join(f"{label}: {title}" for label, title in chart.axes))
    series = [s for s in chart.series if s.values or s.categories]
    if not series:
        parts.append("no data saved with it")
    budget = MAX_ROWS
    for s in series[:MAX_SERIES]:
        points = []
        for i in range(_point_count(s)):
            if budget <= 0:
                break
            budget -= 1
            label = s.categories[i] if i < len(s.categories) else str(i + 1)
            value = s.values[i] if i < len(s.values) else ""
            size = f" (size {s.sizes[i]})" if i < len(s.sizes) and s.sizes[i] else ""
            points.append(f"{label} {value}{size}".strip())
        cut = "" if len(points) == _point_count(s) else f" … of {_point_count(s)} points"
        parts.append(f"{s.name}: {', '.join(points)}{cut}")
    if len(series) > MAX_SERIES:
        parts.append(f"first {MAX_SERIES} of {len(series)} series")
    if chart.note:
        parts.append(chart.note)
    return "[chart: " + ". ".join(p for p in parts if p) + "]"


# ---------------------------------------------------------------------------
# PowerPoint: markers the sandbox extraction leaves for the host to fill
# ---------------------------------------------------------------------------

#: Written by ``read_powerpoint_presentation``'s sandbox script where a slide
#: holds a chart, with the chart's part name; :func:`fill_chart_markers`
#: replaces it with the rendered chart from the same bytes.
CHART_MARKER_OPEN = "⟦aps-chart:"
CHART_MARKER_CLOSE = "⟧"
_MARKER = re.compile(re.escape(CHART_MARKER_OPEN) + r"([^⟧\n]*)" + re.escape(CHART_MARKER_CLOSE))


def fill_chart_markers(text: str, package: bytes) -> str:
    """Replace each chart marker in ``text`` with that chart rendered from the
    package's bytes (:func:`chart_markdown`). A marker naming a part that is
    missing or unreadable becomes ``[chart]``; text without markers is returned
    unchanged."""
    if CHART_MARKER_OPEN not in text:
        return text
    import io

    try:
        archive = zipfile.ZipFile(io.BytesIO(package))
    except (zipfile.BadZipFile, ValueError):
        return _MARKER.sub("[chart]", text)
    with archive:
        names = set(archive.namelist())
        cache: Dict[str, str] = {}

        def render(match: "re.Match[str]") -> str:
            body = match.group(1)
            name, _, alt = body.partition("|")
            name = name.strip().lstrip("/")
            if name not in cache:
                chart = _read_member(archive, name) if name in names and "/charts/" in f"/{name}" else None
                cache[name] = chart_markdown(chart, alt=alt) if chart is not None else ""
            return cache[name] or (f"[chart: {_clip(alt)}]" if alt.strip() else "[chart]")

        return _MARKER.sub(render, text)


__all__ = [
    "CHART_MARKER_CLOSE",
    "CHART_MARKER_OPEN",
    "CHART_REFERENCE_TAGS",
    "Chart",
    "MAX_ROWS",
    "MAX_SERIES",
    "Series",
    "chart_line",
    "chart_markdown",
    "chart_reference",
    "chart_targets",
    "fill_chart_markers",
    "load_charts",
    "read_chart",
]
