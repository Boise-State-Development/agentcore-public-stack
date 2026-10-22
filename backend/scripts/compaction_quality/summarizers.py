"""Candidate summarizers, screened against production's ``bound_summary``.

These are prototypes, not production code. Each has ``bound_summary``'s
signature and returns its ``BoundedSummary``. An arm that names one runs
the *production* cut with only the summarizer swapped, so a difference in
the result is the summarizer's alone.

- ``compress_only(model_id)``: option 2. Production's compression prompt,
  budget and fallback, sending ``temperature`` alone. It was written when
  production's ``compress_with_model`` also sent ``topP``, which Claude 4.5+
  rejects ("`temperature` and `top_p` cannot both be specified"), so a Haiku
  ``summary_model_id`` silently fell back to truncation. Production now sends
  ``temperature`` alone too, so this arm calls it directly.

Option 3, extract-then-compress, moved into production
(``agents/main_agent/session/compaction_summary.py``, behind
``COMPACTION_SUMMARY_EXTRACT_ENABLED``). Its arms set
``summary_extract_enabled`` on the config, so they screen the production
code, not a copy.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional, Sequence

from agents.main_agent.session.compaction_summary import (
    BoundedSummary,
    approx_tokens,
    compress_with_model,
    truncate_records_newest_first,
)

Summarizer = Callable[..., Awaitable[BoundedSummary]]


def compress_only(model_id: str) -> Summarizer:
    """Option 2 on any model: production's ``bound_summary`` flow with ``compress_with_model``."""
    chosen = model_id

    async def summarize(
        records: Sequence[str],
        budget_tokens: int,
        *,
        model_enabled: bool,
        region: Optional[str] = None,
        **_: object,
    ) -> BoundedSummary:
        records = [r for r in records if isinstance(r, str) and r.strip()]
        joined = "\n\n".join(records) if records else None
        before = approx_tokens(joined)
        if not joined:
            return BoundedSummary(None, "empty", 0, 0)
        if before <= budget_tokens:
            return BoundedSummary(joined, "within_budget", before, before)
        compressed = await compress_with_model(records, budget_tokens, model_id=chosen, region=region)
        if compressed is not None and approx_tokens(compressed) <= budget_tokens:
            return BoundedSummary(compressed, "model", before, approx_tokens(compressed))
        truncated = truncate_records_newest_first([compressed] if compressed else records, budget_tokens)
        return BoundedSummary(truncated, "truncated_after_model", before, approx_tokens(truncated))

    return summarize
