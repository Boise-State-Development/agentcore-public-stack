"""G3 baseline probe — what does today's document path actually see?

`docs/specs/document-offload-evaluation.md` §1 requires this before any offload
quality scoring: the validation pass found we send no citations config in
production, and assumed that on Bedrock the visual (page-image) PDF path is
tied to citations-enabled document handling. If that were true, production
would be blind to charts today and every offload comparison would inherit a
degraded baseline.

This probe settles it. It builds a self-contained corpus, then asks each
question twice against Bedrock Converse:

  arm "bare"  — the document block exactly as `DocumentHandler` builds it
                today (format / name / source.bytes, no citations)
  arm "cited" — the same block with `citations: {enabled: True}` added,
                nothing else changed

Four of the five documents are image-only (PIL writes no text layer), so a
correct answer proves the model saw pixels. `text_layer.pdf` is the probe's
own canary: if it fails, the probe is broken rather than the model. The mixed
document is the realistic production shape — prose plus a figure.

Usage:
    cd backend
    AWS_PROFILE=dev-ai uv run python scripts/probe_document_citations.py
    AWS_PROFILE=dev-ai uv run python scripts/probe_document_citations.py \
        us.anthropic.claude-haiku-4-5-20251001-v1:0 us.anthropic.claude-sonnet-5

Findings as of 2026-08-12 are recorded in
`docs/specs/document-citations-probe-findings.md`.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import boto3
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("g3-probe")

REGION = "us-west-2"
DEFAULT_MODELS = ["us.anthropic.claude-haiku-4-5-20251001-v1:0"]

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

REFUSAL_MARKERS = [
    "unable to", "cannot see", "can't see", "no image", "not able to see",
    "don't have access", "do not have access", "unable to view", "cannot view",
    "not visible", "cannot read", "can't read",
]


# --------------------------------------------------------------------- corpus

def _font(size: int) -> Any:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _chart_image() -> Image.Image:
    """Bar chart whose values exist only as pixel heights against an axis."""
    width, height = 1200, 900
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.text((60, 40), "Quarterly Enrollment, Department of Ceramics",
              font=_font(34), fill="black")

    bars = [("Fall 2023", 412), ("Spring 2024", 268), ("Fall 2024", 631), ("Spring 2025", 349)]
    base_y, left, bar_w, gap, top_val = 780, 140, 150, 90, 700
    scale = (base_y - 160) / top_val

    draw.line([(left - 40, base_y), (width - 60, base_y)], fill="black", width=3)
    draw.line([(left - 40, base_y), (left - 40, 150)], fill="black", width=3)
    for grid in range(0, top_val + 1, 100):
        y = base_y - grid * scale
        draw.line([(left - 48, y), (left - 40, y)], fill="black", width=3)
        draw.text((left - 120, y - 14), str(grid), font=_font(24), fill="black")

    for i, (label, value) in enumerate(bars):
        x0 = left + i * (bar_w + gap)
        draw.rectangle([x0, base_y - value * scale, x0 + bar_w, base_y], fill=(41, 84, 143))
        draw.text((x0 + 6, base_y + 14), label, font=_font(22), fill="black")
    return img


def _table_image() -> Image.Image:
    width, height = 1200, 800
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.text((60, 40), "Table 3. Course Fees by Program (2025-26)", font=_font(32), fill="black")
    rows = [
        ["Program", "Lab Fee", "Materials", "Total"],
        ["Ceramics", "$145", "$310", "$455"],
        ["Printmaking", "$92", "$188", "$280"],
        ["Metalsmithing", "$237", "$401", "$638"],
        ["Photography", "$118", "$264", "$382"],
    ]
    x0, y0, col_w, row_h = 70, 130, 265, 76
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            x, y = x0 + c * col_w, y0 + r * row_h
            draw.rectangle([x, y, x + col_w, y + row_h], outline="black", width=2)
            if r == 0:
                draw.rectangle([x + 2, y + 2, x + col_w - 2, y + row_h - 2], fill=(230, 230, 235))
            draw.text((x + 16, y + 24), cell, font=_font(26), fill="black")
    return img


def _scan_image() -> Image.Image:
    width, height = 1200, 1000
    img = Image.new("RGB", (width, height), (252, 251, 246))
    draw = ImageDraw.Draw(img)
    lines = [
        "MEMORANDUM", "", "To all department chairs:", "",
        "Effective the start of the spring term, the equipment replacement",
        "reserve will be held at eleven percent of each department's annual",
        "operating allocation. Requests to draw against the reserve must be",
        "filed with the facilities office no later than the fourteenth day of",
        "the month preceding the intended purchase.", "",
        "The prior threshold of six percent is retired and should not be used",
        "in any budget projection after this date.", "",
        "  -- Office of the Provost, document reference PR-2291",
    ]
    y = 90
    for line in lines:
        draw.text((100, y), line, font=_font(30), fill=(28, 28, 32))
        y += 56
    img = img.rotate(-0.7, expand=False, fillcolor=(252, 251, 246))
    ImageDraw.Draw(img).rectangle([0, 0, width - 1, height - 1], outline=(205, 203, 197), width=6)
    return img


def _assemble_pdf(objects: Dict[int, bytes]) -> bytes:
    """Serialize numbered PDF objects into a valid single-file PDF."""
    out = b"%PDF-1.4\n"
    offsets: Dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode("latin-1") + objects[num] + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode("latin-1")
    for num in sorted(objects):
        out += f"{offsets[num]:010d} 00000 n \n".encode("latin-1")
    out += (f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode("latin-1")
    return out


def _text_layer_pdf() -> bytes:
    body = (
        "BT /F1 16 Tf 60 720 Td (Facilities Standards Handbook, Section 9) Tj ET\n"
        "BT /F1 12 Tf 60 690 Td (The freight elevator in the Liberal Arts building has a rated) Tj ET\n"
        "BT /F1 12 Tf 60 672 Td (capacity of 3,400 pounds and is inspected twice per year.) Tj ET\n"
        "BT /F1 12 Tf 60 654 Td (The passenger elevators are inspected annually.) Tj ET\n"
    )
    return _assemble_pdf({
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        4: f"<< /Length {len(body)} >>\nstream\n{body}endstream".encode("latin-1"),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    })


def _mixed_pdf() -> bytes:
    """Page 1 real text layer, page 2 chart image — the production shape."""
    img = _chart_image()
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    jpg, iw, ih = buf.getvalue(), img.width, img.height

    text = (
        "BT /F1 16 Tf 60 720 Td (Annual Report: Department of Ceramics) Tj ET\n"
        "BT /F1 12 Tf 60 690 Td (The department operates a shared kiln facility rated for) Tj ET\n"
        "BT /F1 12 Tf 60 672 Td (cone 10 reduction firing. The replacement cost of the) Tj ET\n"
        "BT /F1 12 Tf 60 654 Td (primary kiln is estimated at 47,500 dollars as of this year.) Tj ET\n"
        "BT /F1 12 Tf 60 624 Td (Quarterly enrollment is shown in the figure on page 2.) Tj ET\n"
    )
    disp_w = 512.0
    disp_h = disp_w * ih / iw
    stream = f"q {disp_w:.2f} 0 0 {disp_h:.2f} 50 400 cm /Im0 Do Q\n"

    return _assemble_pdf({
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Resources << /Font << /F1 7 0 R >> >> /Contents 5 0 R >>",
        4: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
           b"/Resources << /XObject << /Im0 8 0 R >> >> /Contents 6 0 R >>",
        5: f"<< /Length {len(text)} >>\nstream\n{text}endstream".encode("latin-1"),
        6: f"<< /Length {len(stream)} >>\nstream\n{stream}endstream".encode("latin-1"),
        7: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        8: (f"<< /Type /XObject /Subtype /Image /Width {iw} /Height {ih} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
            f"/Length {len(jpg)} >>\nstream\n").encode("latin-1") + jpg + b"\nendstream",
    })


def build_corpus(out_dir: str) -> Dict[str, Tuple[str, str]]:
    """Write the corpus, returning {key: (path, bedrock document name)}."""
    def image_pdf(img: Image.Image, name: str) -> str:
        path = os.path.join(out_dir, name)
        img.convert("RGB").save(path, "PDF", resolution=150.0)
        return path

    def raw_pdf(data: bytes, name: str) -> str:
        path = os.path.join(out_dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    corpus = {
        "chart": (image_pdf(_chart_image(), "chart_only.pdf"), "chart only"),
        "table": (image_pdf(_table_image(), "table_in_image.pdf"), "table in image"),
        "scan": (image_pdf(_scan_image(), "scanned_page.pdf"), "scanned page"),
        "canary": (raw_pdf(_text_layer_pdf(), "text_layer.pdf"), "text layer canary"),
        "mixed": (raw_pdf(_mixed_pdf(), "mixed_text_and_chart.pdf"), "annual report"),
    }
    for key, (path, _) in corpus.items():
        logger.info("corpus %-7s %-26s %8d bytes", key, os.path.basename(path),
                    os.path.getsize(path))
    return corpus


# ------------------------------------------------------------------ questions
# accept:     any listed substring present -> correct
# accept_all: every listed substring must be present
# num:        any integer in the answer inside (lo, hi) -> correct
#             (chart bars carry no printed labels, so values are read off an
#             axis and an exact-match bar would be unfair)
QUESTIONS: List[Dict[str, Any]] = [
    dict(id="c1", doc="chart", family="chart-value", num=(600, 660), truth="631",
         q="What is the value of the Fall 2024 bar in this chart? Answer with the number only."),
    dict(id="c2", doc="chart", family="chart-compare", accept=["spring 2024"], truth="Spring 2024",
         q="Which period in this chart has the lowest enrollment? Answer with the period label only."),
    dict(id="c3", doc="chart", family="chart-value", num=(300, 430), truth="~363",
         q="Approximately how much higher is the Fall 2024 bar than the Spring 2024 bar? "
           "Answer with a number only."),
    dict(id="c4", doc="chart", family="chart-structure",
         accept_all=["fall 2023", "spring 2024", "fall 2024", "spring 2025"],
         truth="all four labels",
         q="List the four period labels along the horizontal axis, left to right."),
    dict(id="t1", doc="table", family="table-cell", accept=["401"], truth="$401",
         q="What is the Materials fee for Metalsmithing? Answer with the amount only."),
    dict(id="t2", doc="table", family="table-compare", accept=["printmaking"], truth="Printmaking",
         q="Which program has the lowest Total? Answer with the program name only."),
    dict(id="t3", doc="table", family="table-cell", accept=["118"], truth="$118",
         q="What is the Lab Fee for Photography? Answer with the amount only."),
    dict(id="s1", doc="scan", family="scan-fact", accept=["eleven", "11%", "11 percent"],
         truth="eleven percent",
         q="What percentage of each department's annual operating allocation is the "
           "equipment replacement reserve held at?"),
    dict(id="s2", doc="scan", family="scan-fact", accept=["pr-2291", "pr 2291", "pr2291"],
         truth="PR-2291",
         q="What is the document reference identifier shown on this memo?"),
    dict(id="k1", doc="canary", family="text-layer-canary", accept=["3,400", "3400"],
         truth="3,400 lb",
         q="What is the rated capacity of the freight elevator? Answer with the number only."),
    dict(id="m1", doc="mixed", family="mixed-text", accept=["47,500", "47500"], truth="$47,500 (p1)",
         q="What is the estimated replacement cost of the primary kiln? Answer with the amount only."),
    dict(id="m2", doc="mixed", family="mixed-chart", num=(600, 660), truth="631 (p2 image)",
         q="In the figure, what is the value of the Fall 2024 bar? Answer with the number only."),
    dict(id="m3", doc="mixed", family="mixed-chart", accept=["spring 2024"],
         truth="Spring 2024 (p2)",
         q="In the figure, which period has the lowest enrollment? Answer with the label only."),
    dict(id="m4", doc="mixed", family="mixed-cross", accept_all=["yes"], truth="yes / yes",
         q="Does the report state a kiln replacement cost, and does the figure show Fall 2024 "
           "above 600? Answer yes/no to each."),
]


# -------------------------------------------------------------------- runner

def document_block(corpus, doc_key: str, citations: bool) -> Dict[str, Any]:
    """Mirror `DocumentHandler.create_content_block`, optionally + citations."""
    path, label = corpus[doc_key]
    with open(path, "rb") as fh:
        data = fh.read()
    block: Dict[str, Any] = {
        "document": {"format": "pdf", "name": label, "source": {"bytes": data}}
    }
    if citations:
        block["document"]["citations"] = {"enabled": True}
    return block


def extract(blocks: List[Dict[str, Any]]) -> Tuple[str, bool]:
    """Pull answer text out of a response.

    With citations enabled the answer moves INSIDE ``citationsContent`` and the
    top-level ``text`` blocks go empty — a consumer reading only ``text`` sees
    nothing. Returns (text, response carried citation blocks).
    """
    parts: List[str] = []
    cited = False
    for block in blocks:
        if "text" in block:
            parts.append(block["text"])
        if "citationsContent" in block:
            cited = True
            parts += [c.get("text", "") for c in block["citationsContent"].get("content", [])]
    return " ".join(parts).strip(), cited


def score(question: Dict[str, Any], answer: str) -> bool:
    lowered = answer.lower()
    if "num" in question:
        lo, hi = question["num"]
        found = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", lowered)]
        return any(lo <= n <= hi for n in found)
    if "accept_all" in question:
        return all(token in lowered for token in question["accept_all"])
    return any(token in lowered for token in question["accept"])


def ask(client, model_id: str, corpus, question, citations: bool) -> Dict[str, Any]:
    message = {
        "role": "user",
        "content": [document_block(corpus, question["doc"], citations), {"text": question["q"]}],
    }
    started = time.time()
    error: Optional[str] = None
    # Claude 5-family models reject `temperature` outright; fall back rather
    # than scoring a 400 as a wrong answer.
    for config in ({"maxTokens": 500, "temperature": 0}, {"maxTokens": 500}):
        try:
            response = client.converse(modelId=model_id, messages=[message],
                                       inferenceConfig=config)
            break
        except Exception as exc:  # noqa: BLE001 - probe reports, never raises
            error = f"{type(exc).__name__}: {exc}"
            if "temperature" not in str(exc):
                return dict(error=error, answer="", cited=False)
    else:
        return dict(error=error, answer="", cited=False)

    text, cited = extract(response["output"]["message"]["content"])
    return dict(
        error=None,
        answer=text,
        cited=cited,
        refused=any(m in text.lower() for m in REFUSAL_MARKERS),
        usage=response.get("usage", {}),
        ms=int((time.time() - started) * 1000),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="*", default=None,
                        help=f"Bedrock model ids (default: {DEFAULT_MODELS[0]})")
    parser.add_argument("--out", default=None, help="directory for corpus + results JSON")
    args = parser.parse_args()

    models = args.models or DEFAULT_MODELS
    out_dir = args.out or tempfile.mkdtemp(prefix="g3-probe-")
    os.makedirs(out_dir, exist_ok=True)
    logger.info("corpus + results -> %s", out_dir)

    corpus = build_corpus(out_dir)
    client = boto3.client("bedrock-runtime", region_name=REGION)
    results: List[Dict[str, Any]] = []

    for model_id in models:
        print(f"\n{'=' * 86}\nMODEL {model_id}\n{'=' * 86}")
        for question in QUESTIONS:
            row: Dict[str, Any] = dict(model=model_id, id=question["id"],
                                       doc=question["doc"], family=question["family"],
                                       truth=question["truth"])
            for arm, citations in (("bare", False), ("cited", True)):
                res = ask(client, model_id, corpus, question, citations)
                res["correct"] = (not res["error"]) and score(question, res["answer"])
                row[arm] = res
            bare, cited = row["bare"], row["cited"]
            marker = "" if bare["correct"] == cited["correct"] else "   <-- ARMS DIVERGE"
            print(f"[{question['id']:>3}] {question['family']:<20} "
                  f"truth={question['truth']:<18} "
                  f"bare={'PASS' if bare['correct'] else 'fail'}  "
                  f"cited={'PASS' if cited['correct'] else 'fail'}"
                  f"{'+cit' if cited['cited'] else '    '}{marker}")
            if bare["error"] or cited["error"]:
                print(f"       ERROR bare={bare['error']} cited={cited['error']}")
            results.append(row)

    results_path = os.path.join(out_dir, "results.json")
    with open(results_path, "w") as fh:
        json.dump(results, fh, indent=2, default=str)

    print(f"\n{'=' * 86}\nSUMMARY\n{'=' * 86}")
    for model_id in models:
        rows = [r for r in results if r["model"] == model_id]
        bare_ok = sum(r["bare"]["correct"] for r in rows)
        cited_ok = sum(r["cited"]["correct"] for r in rows)
        with_cit = sorted(r["id"] for r in rows if r["cited"]["cited"])
        print(f"{model_id}")
        print(f"  bare  {bare_ok}/{len(rows)} correct")
        print(f"  cited {cited_ok}/{len(rows)} correct")
        print(f"  responses carrying citation blocks: {len(with_cit)} {with_cit}")
    print(f"\nresults -> {results_path}")


if __name__ == "__main__":
    main()
