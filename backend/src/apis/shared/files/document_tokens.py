"""Token weight of an inline attachment — PDFs are dual-encoded, bytes/4 is not.

``docs/specs/document-context-offload.md`` §6.1 reports a document's weight as
``documentTokens`` and §4C decides whether an eviction is worth its prefix
re-write from the same number. Both used the compaction estimator's ``bytes/4``,
which is **wrong for PDFs by an order of magnitude**.

Why: Bedrock understands a PDF page as an image *and* an extracted text layer
(the dual encoding §3 of the spec relies on, and the reason the design returns
native blocks rather than flattened text). The image channel dominates, and it
has nothing to do with the file's byte size — a 27 KB 60-page PDF and a 27 MB
60-page scan cost about the same. ``bytes/4`` sees only the bytes.

Measured on dev 2026-09-16 (session ``61de2256``): a 27,578-byte, 60-page PDF
produced a **109.1K-token** first write against a 14.6K static prefix, so the
document was ≈94K tokens. ``bytes/4`` reported **6,894** — low by ~14×. At that
error the spec's "document share of the prefix" reads ~6% where the truth was
~86%, which inverts the ship/abandon call in the evaluation spec §4.2.

The model here: **pages x ``PDF_PAGE_TOKEN_ESTIMATE``**, floored by ``bytes/4``
so a byte-heavy PDF is never scored below the old number. The default (1,500) is
``compaction_policy.IMAGE_TOKEN_ESTIMATE``'s own rationale — Anthropic image
tokens are ~(w*h)/750, and a letter page at ~1000x1100 lands there. Against the
measurement above it is 1,500 vs 1,573 actual per page: ~5% low, deliberately,
because this number gates an eviction and over-stating it would evict documents
whose re-write has not earned it.

Still a heuristic — Bedrock reports no per-block usage — but one whose dominant
term is now the right term. Non-PDF formats keep ``bytes/4``: Bedrock extracts
them to text, so there is no image channel to miss.

Never raises: an unparseable PDF falls back to ``bytes/4``, which is exactly
today's behaviour.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Same heuristic as ``compaction_policy.CHARS_PER_TOKEN``; duplicated because
#: ``apis.shared`` must not import the agent layer.
CHARS_PER_TOKEN = 4

#: Estimated tokens for one rendered PDF page (the image channel). Env-tunable
#: so the number can be re-fit from measured rows without a deploy.
PDF_PAGE_TOKEN_ESTIMATE = int(os.environ.get("PDF_PAGE_TOKEN_ESTIMATE", 1_500))


def pdf_page_count(raw: bytes) -> Optional[int]:
    """Page count of ``raw``, or ``None`` if it will not open.

    Costs ~0.1 ms even for a 200-page file (measured), so callers on the
    per-turn path do not need to memoize it.
    """
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return None
    try:
        import io

        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(io.BytesIO(bytes(raw)))
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception:  # noqa: BLE001 - estimator must never raise
        logger.debug("pdf_page_count: unparseable PDF, falling back to bytes/4", exc_info=True)
        return None


def estimate_document_tokens(fmt: Any, raw: Any) -> int:
    """Estimated tokens an inline document block costs in the prefix.

    PDFs: ``max(pages * PDF_PAGE_TOKEN_ESTIMATE, bytes/4)``. Everything else:
    ``bytes/4``.
    """
    if not isinstance(raw, (bytes, bytearray)):
        return 0
    byte_tokens = len(raw) // CHARS_PER_TOKEN
    if str(fmt or "").lower() != "pdf":
        return byte_tokens
    pages = pdf_page_count(raw)
    if not pages:
        return byte_tokens
    return max(pages * PDF_PAGE_TOKEN_ESTIMATE, byte_tokens)


__all__ = [
    "CHARS_PER_TOKEN",
    "PDF_PAGE_TOKEN_ESTIMATE",
    "estimate_document_tokens",
    "pdf_page_count",
]
