"""An HTML attachment rendered as markdown text: the page, not its markup.

Bedrock turns an ``html`` document block into text before the model reads it.
Sending the raw source as text instead is not equivalent: a saved web page
spends most of its bytes on ``<style>``, ``<script>``, attributes and
navigation markup, and measured on dev (2026-11-06) a 23 KB page cost 9.7k
input tokens raw against 0.8k as a native document. This keeps what the page
says and drops what only a browser needs:

* ``<head>``, ``<script>``, ``<style>``, ``<noscript>``, ``<template>`` and
  ``<svg>`` contribute nothing;
* headings become ``#`` levels, list items ``-`` or a running ``N.`` with
  their nesting, ``<br>`` and block elements become line breaks;
* tables become markdown pipe tables, ``colspan`` / ``rowspan`` repeated into
  every grid slot they cover, the same shape ``docx_markdown`` gives a Word
  table;
* an ``<img>`` becomes ``[image: <alt>]``;
* whitespace collapses as a browser collapses it, except inside ``<pre>``.

Pure stdlib (``html.parser``), deterministic, and tolerant: the parser never
raises on malformed markup, it renders what it can.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

_SKIP = frozenset({"head", "script", "style", "noscript", "template", "svg", "iframe", "object"})
_BLOCK = frozenset({
    "p", "div", "section", "article", "main", "header", "footer", "nav", "aside",
    "blockquote", "figure", "figcaption", "form", "fieldset", "address", "dl", "dt", "dd",
    "hr", "body", "html",
})
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_VOID = frozenset({"br", "img", "hr", "meta", "link", "input", "col", "area", "base", "wbr", "source", "track", "embed", "param"})
_SPACE = re.compile(r"[ \t\r\n\f\v]+")


class _Table:
    def __init__(self) -> None:
        self.rows: List[List[Tuple[str, int, int]]] = []
        self.cell: Optional[List[str]] = None
        self.span = (1, 1)

    def grid(self) -> List[List[str]]:
        grid: List[List[str]] = []
        pending: Dict[int, Tuple[str, int]] = {}  # column -> (text, rows still covered)
        for row in self.rows:
            out: List[str] = []
            cells = list(row)
            col = 0
            while cells or any(c >= col for c in pending):
                if col in pending:
                    text, left = pending[col]
                    out.append(text)
                    if left <= 1:
                        del pending[col]
                    else:
                        pending[col] = (text, left - 1)
                    col += 1
                    continue
                if not cells:
                    break
                text, colspan, rowspan = cells.pop(0)
                for c in range(col, col + colspan):
                    out.append(text)
                    if rowspan > 1:
                        pending[c] = (text, rowspan - 1)
                col += colspan
            grid.append(out)
        return grid


class _Renderer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[str] = []
        self.line: List[str] = []
        self.prefix = ""
        self.skip_depth = 0
        self.pre_depth = 0
        self.lists: List[List] = []  # [ordered, counter]
        self.tables: List[_Table] = []
        self.heading = 0

    # -- output -------------------------------------------------------------

    def _text(self) -> str:
        raw = "".join(self.line)
        if self.pre_depth:
            return raw.strip("\n")
        return _SPACE.sub(" ", raw).strip()

    def flush(self) -> None:
        text = self._text()
        self.line = []
        if not text:
            return
        if self.tables and self.tables[-1].cell is not None:
            self.tables[-1].cell.append(text)
            return
        if self.heading:
            text = f"{'#' * self.heading} {text}"
        elif self.prefix:
            text = f"{self.prefix}{text}"
            self.prefix = ""
        self.blocks.append(text)

    # -- parser hooks ---------------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP:
            if tag not in _VOID:
                self.skip_depth += 1
            return
        if self.skip_depth:
            return
        attr = dict(attrs)
        if tag in _HEADINGS:
            self.flush()
            self.heading = _HEADINGS[tag]
        elif tag in ("ul", "ol", "menu"):
            self.flush()
            start = attr.get("start") or "1"
            self.lists.append([tag == "ol", int(start) - 1 if start.isdigit() else 0])
        elif tag == "li":
            self.flush()
            depth = max(len(self.lists) - 1, 0)
            if self.lists and self.lists[-1][0]:
                self.lists[-1][1] += 1
                marker = f"{self.lists[-1][1]}."
            else:
                marker = "-"
            self.prefix = f"{'  ' * depth}{marker} "
        elif tag == "table":
            self.flush()
            self.tables.append(_Table())
        elif tag == "tr" and self.tables:
            self.flush()
            self.tables[-1].rows.append([])
        elif tag in ("td", "th") and self.tables:
            self.flush()
            table = self.tables[-1]
            if not table.rows:
                table.rows.append([])
            table.cell = []
            table.span = (_int(attr.get("colspan")), _int(attr.get("rowspan")))
        elif tag == "pre":
            self.flush()
            self.pre_depth += 1
        elif tag == "br":
            if self.pre_depth:
                self.line.append("\n")
            else:
                self.flush()
        elif tag == "img":
            alt = (attr.get("alt") or "").strip()
            self.line.append(f" [image: {alt}] " if alt else " [image] ")
        elif tag in _BLOCK:
            self.flush()

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            if tag not in _VOID and self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in _HEADINGS:
            self.flush()
            self.heading = 0
        elif tag in ("ul", "ol", "menu"):
            self.flush()
            if self.lists:
                self.lists.pop()
        elif tag == "li":
            self.flush()
        elif tag in ("td", "th") and self.tables:
            self.flush()
            table = self.tables[-1]
            if table.cell is not None:
                text = "<br>".join(table.cell).replace("|", "\\|")
                colspan, rowspan = table.span
                table.rows[-1].append((text, colspan, rowspan))
                table.cell = None
        elif tag == "table" and self.tables:
            self.flush()
            table = self.tables.pop()
            if self.tables and self.tables[-1].cell is not None:
                # A nested table reads row by row inside its parent's cell,
                # the way ``docx_markdown`` reads one.
                for row in table.grid():
                    line = " ; ".join(c for c in row if c)
                    if line:
                        self.tables[-1].cell.append(line)
            else:
                rendered = _render_table(table)
                if rendered:
                    self.blocks.append(rendered)
        elif tag == "pre":
            self.flush()
            self.pre_depth = max(self.pre_depth - 1, 0)
        elif tag in _BLOCK:
            self.flush()

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.line.append(data)


def _int(value: Optional[str]) -> int:
    try:
        return max(1, min(int(value or 1), 1000))
    except ValueError:
        return 1


def _render_table(table: _Table) -> str:
    grid = [r for r in table.grid() if r]
    if not grid or not any(any(c for c in r) for r in grid):
        return ""
    width = max(len(r) for r in grid)
    lines = []
    for i, row in enumerate(grid):
        cells = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("|" + "|".join(["---"] * width) + "|")
    return "\n".join(lines)


def html_to_markdown(text: str) -> str:
    """The page's content as markdown (see the module docstring)."""
    renderer = _Renderer()
    renderer.feed(text)
    renderer.close()
    renderer.flush()
    return _join(renderer.blocks)


def _join(blocks: List[str]) -> str:
    parts: List[str] = []
    previous_item = False
    for block in blocks:
        is_item = bool(re.match(r"^\s*(?:-|\d+\.) ", block))
        if parts:
            parts.append("\n" if (is_item and previous_item) else "\n\n")
        parts.append(block)
        previous_item = is_item
    return "".join(parts)


__all__ = ["html_to_markdown"]
