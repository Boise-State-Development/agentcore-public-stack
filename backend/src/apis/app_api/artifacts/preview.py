"""A short excerpt of an artifact, for its card on a project's Outputs (3.3).

The Outputs card shows the top of the artifact: the opening of a Markdown
document, the first rows of a CSV, the text of a web page, or a small SVG
itself. The excerpt is computed once from the stored content and kept on the
``OUTPUT#`` pointer, so listing a project's outputs never reads S3 again.

Every excerpt is bounded here, whatever the artifact's size, because the
listing returns one per output. The SPA renders each as text (an SVG only as
an ``<img>`` source, where scripts never run), so nothing here sanitizes.
"""

from __future__ import annotations

import html
import re

MARKDOWN_CHARS = 900
TEXT_CHARS = 600
CSV_ROWS = 8
CSV_ROW_CHARS = 200
SVG_BYTES = 12 * 1024
# A page's text starts near its top; scanning more only costs regex time.
HTML_SCAN_CHARS = 200_000

_HIDDEN_HTML = re.compile(r"<(head|script|style|noscript|template)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def _bare(content_type: str) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def _cut(text: str, limit: int) -> str:
    """At most ``limit`` characters, ending on a line or word break when one is near."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("\n", " "):
        at = head.rfind(sep)
        if at > limit * 0.6:
            return head[:at].rstrip()
    return head


def _html_text(content: str) -> str:
    content = content[:HTML_SCAN_CHARS]
    stripped = _TAG.sub(" ", _HIDDEN_HTML.sub(" ", _COMMENT.sub(" ", content)))
    return _SPACE.sub(" ", html.unescape(stripped)).strip()


def excerpt(content: str, content_type: str) -> str:
    """The card's excerpt of ``content``, or "" when there is nothing worth showing."""
    kind = _bare(content_type)
    if kind in ("text/markdown", "text/x-markdown"):
        return _cut(content.strip(), MARKDOWN_CHARS)
    if kind == "text/csv":
        rows = [line[:CSV_ROW_CHARS] for line in content.strip().splitlines()[:CSV_ROWS]]
        return "\n".join(rows)
    if kind == "image/svg+xml":
        svg = content.strip()
        return svg if len(svg.encode("utf-8")) <= SVG_BYTES else ""
    if kind in ("text/html", "application/xhtml+xml"):
        return _cut(_html_text(content), TEXT_CHARS)
    return _cut(content.strip(), TEXT_CHARS)
