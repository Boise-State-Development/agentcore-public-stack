"""What a model bills for a document's text and page images, per model id.

The one place the per-model document costs live
(``docs/specs/unified-attachment-delivery.md`` §2.1, §5.2). The planner
(``attachment_plan.document_cost``) prices a file from these, and so does every
estimate compaction, offload and the cost row's footprint make from row facts
(``document_estimate.facts_tokens``), so the two can never disagree about what
a held document weighs.

Measured on dev on 2026-11-10 with direct Converse calls:

* **A PDF page image** costs about :data:`PDF_PAGE_IMAGE_TOKENS` on every model
  probed: 15,624 to 15,639 for ten near-empty pages on Haiku 4.5, Haiku 5.5 and
  Sonnet 4.6, and 3,159 for two on Opus 4.7 and Opus 5.5.
* **Text** is billed on top, at the model's own tokenizer. Our ``tokens.text``
  fact is characters / 4. The 32-page ``delivery_report_30k.pdf`` fixture
  (``tokens.text`` 30,779) cost 29,298 tokens of text on Haiku 4.5 and Sonnet
  4.6 and 43,225 on Haiku 5.5, Opus 4.7 and Opus 5.5; the native PDF was its page
  images plus that text to within 0.1%.

Numbers only, no I/O, no imports beyond the standard library.
"""

from __future__ import annotations

import math
from typing import Optional

#: Input tokens one PDF page's image costs, on every model probed.
PDF_PAGE_IMAGE_TOKENS = 1_565

#: Tokens the legacy tokenizer bills per ``tokens.text`` (characters / 4).
LEGACY_TOKENIZER_FACTOR = 0.95
#: The newer tokenizer, and the default for any model id not listed: it
#: over-counts a legacy tokenizer by ~45%, which only sends a file
#: ``on_demand`` sooner, or a cut deeper (the cheaper side either way).
DEFAULT_TOKENIZER_FACTOR = 1.40
#: Model ids (by substring) that use the legacy tokenizer.
_LEGACY_TOKENIZER_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-6")


def tokenizer_factor(model_id: Optional[str]) -> float:
    """Tokens the model bills per ``tokens.text``: :data:`LEGACY_TOKENIZER_FACTOR`
    for the listed models, :data:`DEFAULT_TOKENIZER_FACTOR` for every other.

    An empty or missing id is an unlisted model here. Callers that do not know
    the model at all and want the facts' own count pass no factor instead
    (``document_estimate.facts_tokens`` with ``model_id=None``).
    """
    mid = (model_id or "").lower()
    if any(key in mid for key in _LEGACY_TOKENIZER_MODELS):
        return LEGACY_TOKENIZER_FACTOR
    return DEFAULT_TOKENIZER_FACTOR


def text_cost(text_tokens: int, factor: float) -> int:
    """``text_tokens`` (characters / 4) as the model bills them."""
    return int(math.ceil(max(0, int(text_tokens)) * factor))


def native_pdf_cost(
    text_tokens: int,
    pages: Optional[int],
    image_tokens_fallback: int,
    factor: float,
) -> int:
    """A PDF delivered natively: its text at ``factor`` plus one image per page.

    ``image_tokens_fallback`` is the image channel when the page count is
    unknown (the row's ``tokens.native``, which the facts writer floors by
    bytes / 4).
    """
    images = int(pages) * PDF_PAGE_IMAGE_TOKENS if pages else max(0, int(image_tokens_fallback or 0))
    return text_cost(text_tokens, factor) + images


__all__ = [
    "DEFAULT_TOKENIZER_FACTOR",
    "LEGACY_TOKENIZER_FACTOR",
    "PDF_PAGE_IMAGE_TOKENS",
    "native_pdf_cost",
    "text_cost",
    "tokenizer_factor",
]
