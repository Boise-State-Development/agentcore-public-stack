"""Hard cap on retrieval query length.

Amazon Bedrock Managed Knowledge Base caps ``Retrieve`` query input at **10,000
characters** and that quota is **not adjustable**. Exceeding it is a request
error, not a degraded result — so an unclamped query is the difference between a
slightly-truncated answer and no answer at all.

Why this lives above the seam
-----------------------------
The clamp is applied in the facade, before backend dispatch, so both backends see
an identically-shaped query. Clamping only the managed path would mean the two
backends answered *different questions* whenever a query ran long, which would
quietly invalidate the dual-read comparison this migration depends on: a rank
disagreement would be indistinguishable from a genuine retrieval difference.

That does mean the legacy path is now clamped too, where previously it was not.
Titan v2 tolerates roughly 32,000 characters, so queries between 10,000 and that
ceiling used to be embedded whole and now are not. This is deliberate — parity is
worth more than the tail of a pathological query — and it is why the truncation
emits a metric rather than passing silently.

Why it never raises
-------------------
A query too long is a fixable input, not a failure. Raising would turn a
recoverable situation into a 500 on a chat turn. The function is total: every
input maps to an output of at most :data:`MAX_QUERY_CHARS` characters.
"""

from __future__ import annotations

import logging
from typing import Tuple

from apis.shared.kb_backend.metrics import METRIC_QUERY_CLAMPED, emit_count

logger = logging.getLogger(__name__)

#: Managed KB's ``Retrieve`` input limit. Not adjustable — do not raise this
#: hoping for a quota increase; there is not one to request.
MAX_QUERY_CHARS = 10_000


def clamp_query(query: str) -> Tuple[str, bool]:
    """Return ``(clamped_query, was_truncated)``.

    Truncates from the end, keeping the head. For a natural-language query the
    beginning carries the intent, so a tail-truncated query still retrieves
    something sensible; head-truncating would change the question entirely.

    A ``None`` or non-string input is coerced rather than rejected, because the
    caller is a request path and the clamp is a guard, not a validator.
    """
    if not query:
        return "", False

    if not isinstance(query, str):
        query = str(query)

    if len(query) <= MAX_QUERY_CHARS:
        return query, False

    original_length = len(query)
    clamped = query[:MAX_QUERY_CHARS]

    # Error-level would overstate it (the request still succeeds) and debug would
    # hide it. A clamped query means someone's answer is based on a partial
    # question, which an operator should be able to see without turning on debug.
    logger.warning(
        f"Query clamped from {original_length} to {MAX_QUERY_CHARS} characters "
        f"(Managed KB Retrieve limit, not adjustable)"
    )
    emit_count(METRIC_QUERY_CLAMPED)

    return clamped, True
