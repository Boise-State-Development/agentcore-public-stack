"""Write the marked-up review PDF: the panel's notes placed on the proposal's pages.

Usage:
    python scripts/build_review.py <proposal.pdf> <review.json> <out.pdf>

<review.json> is what the panel wrote, as JSON:

    {
      "recommendation": "Competitive: ...",
      "fix_first": ["...", "..."],
      "notes": [
        {"n": 1, "section": "Project Summary", "quote": "exact phrase from the proposal",
         "note": "what to change", "reviewers": "Generalist, Emotional Outsider"}
      ],
      "audit_fails": [
        {"section": "Formatting", "criterion": "Project Description within 15 pages",
         "page": 18, "quote": "exact phrase, if there is one", "note": "what to do"}
      ],
      "audit_counts": [
        {"section": "Eligibility", "pass": 8, "fail": 0, "cant_tell": 0, "not_applicable": 0}
      ]
    }

Each note's quote is found in the PDF's text (ignoring case, punctuation and line
breaks). If the whole quote is not there, the longest run of at least four of its
words is used. An item with a "page" (the audit's Where column, the proposal's own
PDF page) is looked for on that page first, and placed there as a margin comment
when its words are not found; an item with neither falls back to its section
heading. A found quote gets a highlight over the words and a comment annotation in
the margin; the original pages are otherwise unchanged. A "Panel review" page goes
first, listing the recommendation, the fixes, every note with the page it was placed
on, and the audit counts.

Prints one line per note and audit fail saying where it went, then a summary line.
"""

import json
import re
import sys
from io import BytesIO

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight, Text
from pypdf.generic import ArrayObject, FloatObject, NameObject, TextStringObject
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

NOTE_COLOUR = "ffe066"
AUDIT_COLOUR = "ff9b8a"
MIN_RUN = 4


def norm(word: str) -> str:
    return re.sub(r"[^\w$%.]", "", word.lower()).strip(".")


def quote_words(quote: str):
    return [w for w in (norm(t) for t in re.split(r"\s+", quote or "")) if w]


def page_words(plumber_pdf):
    pages = []
    for page in plumber_pdf.pages:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=True)
        pages.append([(norm(w["text"]), w) for w in words if norm(w["text"])])
    return pages


def find_run(pages, wanted, only=None):
    """(page_index, words) for the first place the wanted words appears in order, or None."""
    n = len(wanted)
    for p, words in enumerate(pages):
        if only is not None and p != only:
            continue
        texts = [t for t, _ in words]
        for i in range(len(texts) - n + 1):
            if texts[i : i + n] == wanted:
                return p, [w for _, w in words[i : i + n]]
    return None


def page_index(page, count):
    """A 1-based page number ("18", "p. 18", "p. ~18", 18) as an index into the proposal, or None."""
    match = re.search(r"\d+", str(page or ""))
    if not match:
        return None
    index = int(match.group()) - 1
    return index if 0 <= index < count else None


def locate(pages, quote, section, page=None):
    wanted = quote_words(quote)
    on_page = page_index(page, len(pages))
    for only in ([on_page] if on_page is not None else []) + [None]:
        if not wanted:
            break
        hit = find_run(pages, wanted, only)
        if hit:
            return hit + ("exact",)
        for size in range(len(wanted) - 1, MIN_RUN - 1, -1):
            for start in range(0, len(wanted) - size + 1):
                hit = find_run(pages, wanted[start : start + size], only)
                if hit:
                    return hit + (f"{size} of {len(wanted)} words",)
    if on_page is not None:
        return on_page, [], "page given"
    heading = quote_words(section)
    if heading:
        hit = find_run(pages, heading)
        if hit:
            return hit + ("section heading",)
    return None


def line_boxes(words, page_height):
    """One PDF-space box per text line covered by the words."""
    lines = []
    for w in words:
        for line in lines:
            if abs(line["top"] - w["top"]) < 3:
                line["x0"] = min(line["x0"], w["x0"])
                line["x1"] = max(line["x1"], w["x1"])
                line["bottom"] = max(line["bottom"], w["bottom"])
                break
        else:
            lines.append(dict(x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"]))
    return [(l["x0"], page_height - l["bottom"], l["x1"], page_height - l["top"]) for l in lines]


def annotate(writer, page_index, offset, words, page_height, page_width, label, body, author, colour):
    margin_x = page_width - 0.55 * inch
    if not words:
        # Only a page: a comment in the top margin, no highlight.
        top = page_height - 0.6 * inch
        comment = Text(rect=(margin_x, top - 18, margin_x + 18, top), text=f"{label}: {body}", open=False)
        comment[NameObject("/T")] = TextStringObject(author)
        comment[NameObject("/C")] = ArrayObject(FloatObject(int(colour[i : i + 2], 16) / 255) for i in (0, 2, 4))
        writer.add_annotation(page_number=page_index + offset, annotation=comment)
        return
    boxes = line_boxes(words, page_height)
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    quads = ArrayObject()
    for bx0, by0, bx1, by1 in boxes:
        quads.extend(FloatObject(v) for v in (bx0, by1, bx1, by1, bx0, by0, bx1, by0))
    highlight = Highlight(rect=(x0, y0, x1, y1), quad_points=quads, highlight_color=colour)
    highlight[NameObject("/Contents")] = TextStringObject(f"{label}: {body}")
    highlight[NameObject("/T")] = TextStringObject(author)
    writer.add_annotation(page_number=page_index + offset, annotation=highlight)
    comment = Text(rect=(margin_x, y1 - 18, margin_x + 18, y1), text=f"{label}: {body}", open=False)
    comment[NameObject("/T")] = TextStringObject(author)
    comment[NameObject("/C")] = ArrayObject(FloatObject(int(colour[i : i + 2], 16) / 255) for i in (0, 2, 4))
    writer.add_annotation(page_number=page_index + offset, annotation=comment)


def cover_page(review, placed):
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=8.5, leading=10.5)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, leading=13)
    esc = lambda s: (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    story = [Paragraph("Panel review", styles["Title"])]
    if review.get("recommendation"):
        story += [Paragraph(f"<b>Recommendation:</b> {esc(review['recommendation'])}", body), Spacer(1, 8)]
    if review.get("fix_first"):
        story.append(Paragraph("<b>Fix first</b>", body))
        story += [Paragraph(f"{i}. {esc(f)}", body) for i, f in enumerate(review["fix_first"], 1)]
        story.append(Spacer(1, 8))
    rows = [["#", "Note", "Page"]]
    for label, text, page in placed:
        rows.append([label, Paragraph(esc(text), small), page])
    if len(rows) > 1:
        story.append(Paragraph("<b>Notes placed on the proposal</b> (page numbers are this file's)", body))
        table = Table(rows, colWidths=[0.45 * inch, 5.4 * inch, 0.7 * inch], repeatRows=1)
        table.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.lightgrey),
        ]))
        story += [table, Spacer(1, 8)]
    counts = review.get("audit_counts") or []
    if counts:
        story.append(Paragraph("<b>Compliance audit</b>", body))
        crow = [["Section", "Pass", "Fail", "Can't tell", "N/A"]]
        totals = [0, 0, 0, 0]
        for c in counts:
            vals = [int(c.get(k) or 0) for k in ("pass", "fail", "cant_tell", "not_applicable")]
            totals = [a + b for a, b in zip(totals, vals)]
            crow.append([c.get("section", "")] + vals)
        crow.append(["Total"] + totals)
        table = Table(crow, colWidths=[2.6 * inch] + [0.9 * inch] * 4, repeatRows=1)
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black),
            ("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ]
        for r, row in enumerate(crow[1:-1], 1):
            if row[2]:
                style.append(("TEXTCOLOR", (2, r), (2, r), colors.HexColor("#b93226")))
        table.setStyle(TableStyle(style))
        story.append(table)
    buf = BytesIO()
    SimpleDocTemplate(buf, pagesize=letter, leftMargin=inch, rightMargin=inch, topMargin=0.8 * inch,
                      bottomMargin=0.8 * inch, title="Panel review").build(story)
    buf.seek(0)
    return PdfReader(buf)


def main(src, review_path, out):
    review = json.load(open(review_path))
    with pdfplumber.open(src) as plumber_pdf:
        pages = page_words(plumber_pdf)
        sizes = [(float(p.width), float(p.height)) for p in plumber_pdf.pages]

    reader = PdfReader(src)
    items = []
    for note in review.get("notes") or []:
        label = f"Note {note.get('n', len(items) + 1)}"
        body = f"{note.get('note', '')} ({note.get('reviewers', '')})"
        items.append((label, body, note.get("reviewers") or "Panel", note.get("quote"), note.get("section"),
                      note.get("page"), NOTE_COLOUR))
    for i, fail in enumerate(review.get("audit_fails") or [], 1):
        label = f"A{i}"
        body = f"Audit, {fail.get('criterion', '')}: {fail.get('note') or 'failed the compliance audit.'}"
        items.append((label, body, "Compliance audit", fail.get("quote"), fail.get("section"), fail.get("page"),
                      AUDIT_COLOUR))

    hits = [locate(pages, quote, section, page) for *_, quote, section, page, _c in items]

    # Build the cover first so we know how many pages it adds before the proposal.
    placed = [(lbl.replace("Note ", ""), body, f"p. {h[0] + 1}" if h else "not found")
              for (lbl, body, *_), h in zip(items, hits)]
    cover = cover_page(review, placed)
    offset = len(cover.pages)
    placed = [(lbl, body, f"p. {h[0] + 1 + offset}" if h else "not found")
              for (lbl, body, *_), h in zip(items, hits)]
    cover = cover_page(review, [(l.replace("Note ", ""), b, p) for l, b, p in placed])

    writer = PdfWriter()
    for page in cover.pages:
        writer.add_page(page)
    for page in reader.pages:
        writer.add_page(page)
    for (label, body, author, _q, _s, _p, colour), hit in zip(items, hits):
        if hit is None:
            print(f"{label}: not found")
            continue
        at, words, how = hit
        width, height = sizes[at]
        annotate(writer, at, offset, words, height, width, label, body, author, colour)
        print(f"{label}: page {at + 1 + offset} ({how})")
    writer.add_metadata({"/Title": "Panel review"})
    with open(out, "wb") as fh:
        writer.write(fh)
    found = sum(1 for h in hits if h)
    print(f"Placed {found} of {len(items)} notes; wrote {out} ({len(writer.pages)} pages, panel page first)")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
