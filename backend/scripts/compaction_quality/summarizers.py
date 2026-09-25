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
  ``temperature`` alone too, so this arm matches it call for call.

- ``extract_then_compress(model_id)``: option 3. It first extracts standing
  instructions, decisions, identifiers and changed values verbatim into a
  pinned block. The narrative is then compressed with production's
  prompt (``_compress``) into whatever budget is left.

If one wins, it moves into ``agents/main_agent/session/compaction_summary.py``
in its own PR. It is not wired from here.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional, Sequence

from agents.main_agent.session.compaction_summary import (
    _COMPRESSION_SYSTEM_PROMPT,
    _MODEL_MAX_OUTPUT_TOKENS,
    BoundedSummary,
    _compression_input,
    approx_tokens,
    truncate_records_newest_first,
)

logger = logging.getLogger(__name__)

Summarizer = Callable[..., Awaitable[BoundedSummary]]

EXTRACTION_PROMPT = """You extract the facts a long conversation between a user and an AI assistant must never lose. You are given the conversation's summary notes, oldest first.

Copy each fact VERBATIM, character for character: names, IDs, codes, amounts, dates, numbers, file names and quoted wording exactly as written. Do not paraphrase a value.

Output exactly these four headings, one bullet per fact:
STANDING INSTRUCTIONS: every instruction, preference, rule or constraint the user gave (formatting, naming, things to always or never do).
DECISIONS: every value or choice that was settled, with what it is for.
IDENTIFIERS: every exact identifier, code, ID, number, amount, date or name that was stated.
CHANGED VALUES: every value that was changed, with ONLY its current value, noting that it replaced an earlier one.

If a later note changes a fact, keep only the latest version. No preamble, no commentary. If a heading has nothing, write "- none"."""

PINNED_HEADER = "PINNED FACTS (verbatim; these override anything below):"
NARRATIVE_HEADER = "SUMMARY:"


async def _compress(records: Sequence[str], budget_tokens: int, *, model_id: str, region: Optional[str]) -> Optional[str]:
    """Production's ``compress_with_model`` prompt and limits, without ``topP``."""
    import asyncio

    import boto3

    text = _compression_input(records)
    if not text.strip():
        return None
    try:
        client = boto3.client("bedrock-runtime", region_name=region or "us-west-2")
        word_budget = max(150, int(budget_tokens * 0.55))
        response = await asyncio.to_thread(
            client.converse,
            modelId=model_id,
            system=[{"text": _COMPRESSION_SYSTEM_PROMPT.replace("{word_budget}", f"{word_budget:,}")}],
            messages=[{"role": "user", "content": [{"text": "Summary notes, oldest first:\n\n" + text}]}],
            inferenceConfig={"temperature": 0.1, "maxTokens": min(_MODEL_MAX_OUTPUT_TOKENS, max(256, int(budget_tokens)))},
        )
        if response.get("stopReason") == "max_tokens":
            return None
        out = response["output"]["message"]["content"][0]["text"].strip()
        return out or None
    except Exception:  # noqa: BLE001 - a candidate never breaks the screen
        logger.warning("compression failed", exc_info=True)
        return None


def compress_only(model_id: str) -> Summarizer:
    """Option 2 on any model: production's ``bound_summary`` flow with ``_compress``."""
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
        compressed = await _compress(records, budget_tokens, model_id=chosen, region=region)
        if compressed is not None and approx_tokens(compressed) <= budget_tokens:
            return BoundedSummary(compressed, "model", before, approx_tokens(compressed))
        truncated = truncate_records_newest_first([compressed] if compressed else records, budget_tokens)
        return BoundedSummary(truncated, "truncated_after_model", before, approx_tokens(truncated))

    return summarize


async def _extract(records: Sequence[str], *, model_id: str, region: Optional[str], max_tokens: int) -> Optional[str]:
    import asyncio

    import boto3

    text = _compression_input(records)
    if not text.strip():
        return None
    try:
        client = boto3.client("bedrock-runtime", region_name=region or "us-west-2")
        response = await asyncio.to_thread(
            client.converse,
            modelId=model_id,
            system=[{"text": EXTRACTION_PROMPT}],
            messages=[{"role": "user", "content": [{"text": "Summary notes, oldest first:\n\n" + text}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": max_tokens},
        )
        if response.get("stopReason") == "max_tokens":
            logger.info("extraction hit max_tokens; keeping what fits")
        out = response["output"]["message"]["content"][0]["text"].strip()
        return out or None
    except Exception:  # noqa: BLE001 - a candidate never breaks the screen
        logger.warning("extraction failed", exc_info=True)
        return None


def extract_then_compress(model_id: str, *, pinned_max_tokens: int = 3_000) -> Summarizer:
    """Option 3: a verbatim pinned block first, then the compressed narrative in the rest.

    ``model_id`` here wins over the one ``update_after_turn`` passes (the
    config's ``summary_model_id``), so one config can screen several models.
    """
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

        pinned = await _extract(records, model_id=chosen, region=region, max_tokens=pinned_max_tokens)
        if pinned and approx_tokens(pinned) > budget_tokens // 2:
            # The pinned block may never crowd out the narrative entirely.
            pinned = truncate_records_newest_first([pinned], budget_tokens // 2)
        pinned_block = f"{PINNED_HEADER}\n{pinned}" if pinned else ""
        remaining = max(256, budget_tokens - approx_tokens(pinned_block) - 16)
        narrative = await _compress(records, remaining, model_id=chosen, region=region)
        if narrative is None or approx_tokens(narrative) > remaining:
            narrative = truncate_records_newest_first([narrative] if narrative else records, remaining)
        parts = [p for p in (pinned_block, f"{NARRATIVE_HEADER}\n{narrative}" if narrative else "") if p]
        text = "\n\n".join(parts)
        outcome = "extract_then_compress" if pinned else "extract_failed"
        return BoundedSummary(text, outcome, before, approx_tokens(text))

    return summarize
