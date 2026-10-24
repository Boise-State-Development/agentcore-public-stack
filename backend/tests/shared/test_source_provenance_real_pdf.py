"""Source provenance against a real `pypdfium2` extraction, not a fixture.

Every other test in this family feeds the verifier unit texts chosen by hand.
That leaves one link unproven: what the production PDF path actually hands over.
It differs from a hand-written fixture in ways that matter — the text layer
comes back with CRLF line endings, and the page split has to line up with the
locator — so this builds a real two-page PDF, runs it through the same
`_units_text` the tool calls, and asserts the states and page numbers.

The PDF is written by hand (a few content streams) rather than with a generator
library, so this adds no dependency.
"""

from __future__ import annotations

import pytest

from apis.shared.files.document_read import _units_text
from apis.shared.files.source_provenance import (
    STATE_QUOTE_UNSUPPORTED,
    STATE_SUPPORTED,
    STATE_UNVERIFIED,
    Claim,
    document_units,
    render_locator,
    verify_claims,
)

PAGE_1 = [
    "NOTICE OF AWARD",
    "The total approved budget is $487,500 for the period of performance.",
    "Indirect costs are calculated at 54% of MTDC.",
]
PAGE_2 = [
    "The project period begins January 15, 2026.",
    "Direct costs total $316,200.",
]


def _minimal_pdf(pages: list[list[str]]) -> bytes:
    """A valid PDF with one Helvetica text stream per page."""
    objs: dict[int, bytes] = {}
    page_ids = [4 + 2 * i for i in range(len(pages))]
    kids = b" ".join(f"{pid} 0 R".encode() for pid in page_ids)
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Count {len(pages)} /Kids [".encode() + kids + b"] >>"
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for index, lines in enumerate(pages):
        page_id = page_ids[index]
        content_id = page_id + 1
        body = ["BT /F1 11 Tf 72 700 Td 16 TL"]
        for line in lines:
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            body.append(f"({escaped}) Tj T*")
        body.append("ET")
        stream = "\n".join(body).encode("latin-1")
        objs[page_id] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        objs[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref = len(out)
    top = max(objs) + 1
    out += f"xref\n0 {top}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, top):
        out += (
            f"{offsets[num]:010d} 00000 n \n".encode()
            if num in offsets
            else b"0000000000 65535 f \n"
        )
    out += f"trailer\n<< /Size {top} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@pytest.fixture(scope="module")
def extracted() -> list[str]:
    return _units_text("pdf", _minimal_pdf([PAGE_1, PAGE_2]))


def test_the_pdf_path_yields_one_text_per_page(extracted):
    assert len(extracted) == 2
    assert "NOTICE OF AWARD" in extracted[0]
    assert "Direct costs total" in extracted[1]


def test_the_real_text_layer_uses_crlf_which_normalization_must_absorb(extracted):
    """The reason this test exists: a model quotes a sentence with a single
    space where the text layer has CRLF, and a fixture written with "\\n" would
    not catch a normalizer that only handled that."""
    assert "\r\n" in extracted[0]
    units = document_units("pdf", extracted)
    spanning = "NOTICE OF AWARD The total approved budget is $487,500"
    result = verify_claims([Claim("c", "$487,500", spanning)], units)[0]
    assert result.state == STATE_SUPPORTED


@pytest.mark.parametrize(
    "claim_id,value,quote,expected_state,expected_locator",
    [
        (
            "total",
            "$487,500",
            "The total approved budget is $487,500",
            STATE_SUPPORTED,
            "p. 1",
        ),
        # The case the feature exists for: a real sentence, an invented amount.
        (
            "fabricated_amount",
            "$912,000",
            "The total approved budget is $487,500",
            STATE_QUOTE_UNSUPPORTED,
            "p. 1",
        ),
        ("rate", "54%", "Indirect costs are calculated at 54% of MTDC.", STATE_SUPPORTED, "p. 1"),
        # The percent sign travels with the value, so the bare dollar amount
        # is not certified by the same sentence.
        (
            "rate_as_dollars",
            "$54",
            "Indirect costs are calculated at 54% of MTDC.",
            STATE_QUOTE_UNSUPPORTED,
            "p. 1",
        ),
        ("direct", "$316,200", "Direct costs total $316,200.", STATE_SUPPORTED, "p. 2"),
        # Written one way in the document, asserted another.
        (
            "start",
            "2026-01-15",
            "The project period begins January 15, 2026.",
            STATE_SUPPORTED,
            "p. 2",
        ),
        ("ghost", "$1", "A sentence that is nowhere in this award letter", STATE_UNVERIFIED, None),
    ],
)
def test_states_and_pages_against_a_real_extraction(
    extracted, claim_id, value, quote, expected_state, expected_locator
):
    units = document_units("pdf", extracted)
    result = verify_claims([Claim(claim_id, value, quote)], units)[0]
    assert result.state == expected_state
    assert render_locator(result.locator) == expected_locator


def test_measured_pages_are_not_hedged(extracted):
    """pypdfium2 measures every boundary, so nothing on this path renders a
    tilde. The hedge is reserved for positions that really were estimated."""
    units = document_units("pdf", extracted)
    results = verify_claims(
        [
            Claim("a", "$487,500", "The total approved budget is $487,500"),
            Claim("b", "$316,200", "Direct costs total $316,200."),
        ],
        units,
    )
    assert [render_locator(r.locator) for r in results] == ["p. 1", "p. 2"]
