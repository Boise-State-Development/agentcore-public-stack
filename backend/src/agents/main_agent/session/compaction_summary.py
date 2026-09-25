"""
Bounded compaction summary.

Spec: docs/specs/compaction-model-relative-thresholds.md §3.6 and
docs/specs/compaction-over-threshold-cache-spiral.md PR-2 (defect D2).

The compaction summary used to be an unbounded join of AgentCore Long-Term
Memory ``ConversationSummary`` records — a log that grows for the life of the
session (164,991 chars ≈ 40k tokens in the incident). A summary that is 40%
of the threshold guarantees compaction can never get back under it. This
module holds the persisted summary at or under a token budget:

1. **Within budget** → unchanged.
2. **Over budget** → one call to the summary model (Nova 2 Lite, the same
   side-channel pattern as titles and tool-batch summaries) that compresses
   the records into a bounded, instruction-preserving summary. It runs once,
   at checkpoint advance — the turn that already pays a prefix re-write.
3. **Model unavailable / failed / still over budget** → newest-first
   truncation of the records: keep the most recent records that fit, and if
   even the newest alone does not fit, keep its tail. Never oldest-first —
   recent context is what the model needs.

**Extract-then-compress** (``extract_enabled``, in development, default off).
Compression alone drops the facts a conversation cannot lose: on the quality
harness Nova Micro kept constraints at 0.72 and identifiers at 0.58 of the
uncompacted control. With the flag on, step 2 becomes two calls:

a. An **extraction** call copies standing instructions, decisions,
   identifiers and changed values (latest value only) verbatim into a pinned
   block, capped at half the budget.
b. **Concurrently**, the narrative is compressed, with the prompt above,
   into the other half.

The persisted text is ``PINNED FACTS …`` followed by ``SUMMARY: …``. The two
calls run side by side, so the cut costs the slower one, not the sum. If
extraction fails the narrative alone is the summary (a plain compression);
if the narrative fails the pinned block is kept and the records are truncated
into the rest; if both fail, the records are truncated. Never a third call.
The model matters: the harness screen kept 100% of planted facts on Nova 2
Lite and Haiku 4.5, and 88% on Nova Micro (docs/kaizen/scoping/
2026-09-21-quality-veto-harness.md §9).

Whatever comes out is persisted verbatim in ``CompactionState.summary`` and
prepended byte-identically at every restore, so the byte-stability contract
is unchanged: the summary still only mutates at checkpoint advance.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .compaction_policy import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

# Bound the *input* to the compression call too: a wide history of records is
# fed newest-first up to this many chars, so the side-channel's own spend is
# flat regardless of how long the session has run.
MAX_COMPRESSION_INPUT_CHARS = 120_000
# Nova Micro's output ceiling is 5k tokens, the smallest of the models this has
# run on; stay under it with margin so the generation is not cut mid-sentence. The budget check after generation is
# what enforces the configured budget.
_MODEL_MAX_OUTPUT_TOKENS = 4_000
# The extraction's own ceiling. At the default 8k budget the pinned block's
# cap (half the budget) is 4k, so this is what binds; it is the figure the
# harness screen ran with.
_EXTRACTION_MAX_OUTPUT_TOKENS = 3_000

_COMPRESSION_SYSTEM_PROMPT = """You maintain the running summary of a long conversation between a user and an AI assistant. You are given the existing summary notes (oldest first). Rewrite them into ONE compact summary the assistant can continue the conversation from.

Keep, in this order, and keep them exact:
1. Standing instructions, preferences and constraints the user gave (tone, format, length, language, things to avoid, people or systems to treat carefully). Quote them; do not paraphrase away specifics.
2. Decisions made and their reasons.
3. The current state of anything being built or edited (a document, essay, code, plan, dataset): what exists now, what is done, what is still open.
4. Open questions, pending tasks, and what the user asked for most recently.
5. Exact identifiers: file names, URLs, course or project names, IDs, numbers, dates, names of people.

Drop: greetings, pleasantries, superseded drafts and revisions, step-by-step narration of tool calls, and anything already covered by a later note.

Rules:
- Plain text with short headings or bullets. No preamble, no closing remark.
- Write in the language the conversation is in.
- Never invent facts; if notes conflict, the later note wins and say so briefly.
- Stay under {word_budget} words."""


_EXTRACTION_SYSTEM_PROMPT = """You extract the facts a long conversation between a user and an AI assistant must never lose. You are given the conversation's summary notes, oldest first.

Copy each fact VERBATIM, character for character: names, IDs, codes, amounts, dates, numbers, file names and quoted wording exactly as written. Do not paraphrase a value.

Output exactly these four headings, one bullet per fact:
STANDING INSTRUCTIONS: every instruction, preference, rule or constraint the user gave (formatting, naming, things to always or never do).
DECISIONS: every value or choice that was settled, with what it is for.
IDENTIFIERS: every exact identifier, code, ID, number, amount, date or name that was stated.
CHANGED VALUES: every value that was changed, with ONLY its current value, noting that it replaced an earlier one.

If a later note changes a fact, keep only the latest version. No preamble, no commentary. If a heading has nothing, write "- none"."""

PINNED_HEADER = "PINNED FACTS (verbatim; these override anything below):"
NARRATIVE_HEADER = "SUMMARY:"


def approx_tokens(text: Optional[str]) -> int:
    """chars/4 — the same estimate the admin SUMMARY_OVER_BUDGET diagnosis uses."""
    if not text:
        return 0
    return len(text) // CHARS_PER_TOKEN


@dataclass(frozen=True)
class BoundedSummary:
    text: Optional[str]
    # "within_budget" | "model" | "truncated" | "truncated_after_model" | "empty"
    # | "extract_then_compress" | "extract_then_truncate"
    outcome: str
    tokens_before: int
    tokens_after: int


def truncate_records_newest_first(records: Sequence[str], budget_tokens: int) -> Optional[str]:
    """Keep the newest records that fit the budget (joined oldest→newest).

    If even the newest record alone exceeds the budget, keep its *tail*.
    """
    budget_chars = max(0, int(budget_tokens)) * CHARS_PER_TOKEN
    kept: List[str] = []
    used = 0
    for record in reversed([r for r in records if r]):
        cost = len(record) + (2 if kept else 0)  # "\n\n" joiner
        if used + cost > budget_chars:
            break
        kept.append(record)
        used += cost
    if kept:
        kept.reverse()
        return "\n\n".join(kept)
    newest = next((r for r in reversed(records) if r), None)
    if not newest:
        return None
    if budget_chars <= 0:
        return None
    tail = newest[-budget_chars:]
    return tail


def _compression_input(records: Sequence[str]) -> str:
    """Records joined oldest→newest, trimmed to the input cap newest-first."""
    joined = "\n\n".join(r for r in records if r)
    if len(joined) <= MAX_COMPRESSION_INPUT_CHARS:
        return joined
    return joined[-MAX_COMPRESSION_INPUT_CHARS:]


async def compress_with_model(
    records: Sequence[str],
    budget_tokens: int,
    *,
    model_id: str,
    region: Optional[str] = None,
) -> Optional[str]:
    """One bounded Bedrock ``converse`` call. Returns ``None`` on any failure.

    Side-channel by construction: its own messages, never ``agent.messages``.
    """
    text = _compression_input(records)
    if not text.strip():
        return None
    try:
        import boto3
    except ImportError:  # pragma: no cover - dev without boto3
        return None
    try:
        region = region or os.environ.get("AWS_REGION", "us-west-2")
        client = boto3.client("bedrock-runtime", region_name=region)
        # ~0.75 words/token; aim well under the budget so the chars/4 check
        # below passes with margin.
        word_budget = max(150, int(budget_tokens * 0.55))
        response = await asyncio.to_thread(
            client.converse,
            modelId=model_id,
            system=[{"text": _COMPRESSION_SYSTEM_PROMPT.replace("{word_budget}", f"{word_budget:,}")}],
            messages=[{"role": "user", "content": [{"text": "Summary notes, oldest first:\n\n" + text}]}],
            # Temperature only: Claude 4.5+ rejects `temperature` and `topP`
            # together, and the blanket except below would turn that into a
            # silent fall back to truncation on every compression.
            inferenceConfig={
                "temperature": 0.1,
                "maxTokens": min(_MODEL_MAX_OUTPUT_TOKENS, max(256, int(budget_tokens))),
            },
        )
        if response.get("stopReason") == "max_tokens":
            logger.info("compaction_summary_model_truncated: generation hit the token ceiling; discarding")
            return None
        out = response["output"]["message"]["content"][0]["text"].strip()
        return out or None
    except Exception:  # noqa: BLE001 - a summary is never worth an error
        logger.warning("compaction_summary_model_failed: falling back to truncation", exc_info=True)
        return None


def _ceil_tokens(chars: int) -> int:
    return -(-int(chars) // CHARS_PER_TOKEN)


def _keep_head_lines(text: str, budget_tokens: int) -> Optional[str]:
    """The leading whole lines of ``text`` that fit ``budget_tokens``.

    The pinned block is ordered by value — standing instructions first — so
    it is trimmed from the end, and at a line so no fact is cut mid-value.
    """
    budget_chars = max(0, int(budget_tokens)) * CHARS_PER_TOKEN
    if len(text) <= budget_chars:
        return text
    head = text[:budget_chars]
    cut = head.rfind("\n")
    head = head[:cut] if cut > 0 else ""
    return head.rstrip() or None


async def extract_with_model(
    records: Sequence[str],
    max_tokens: int,
    *,
    model_id: str,
    region: Optional[str] = None,
) -> Optional[str]:
    """The verbatim extraction call. Returns ``None`` on any failure.

    A generation that hits ``max_tokens`` keeps its complete lines: a pinned
    block missing its last few facts still beats none.
    """
    text = _compression_input(records)
    if not text.strip():
        return None
    try:
        import boto3
    except ImportError:  # pragma: no cover - dev without boto3
        return None
    try:
        region = region or os.environ.get("AWS_REGION", "us-west-2")
        client = boto3.client("bedrock-runtime", region_name=region)
        response = await asyncio.to_thread(
            client.converse,
            modelId=model_id,
            system=[{"text": _EXTRACTION_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": "Summary notes, oldest first:\n\n" + text}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": max(256, int(max_tokens))},
        )
        out = response["output"]["message"]["content"][0]["text"].strip()
        if response.get("stopReason") == "max_tokens":
            logger.info("compaction_summary_extract_truncated: keeping the complete lines")
            cut = out.rfind("\n")
            out = out[:cut].rstrip() if cut > 0 else ""
        return out or None
    except Exception:  # noqa: BLE001 - a summary is never worth an error
        logger.warning("compaction_summary_extract_failed: falling back to plain compression", exc_info=True)
        return None


async def _extract_then_compress(
    records: Sequence[str],
    budget_tokens: int,
    before: int,
    *,
    model_id: str,
    region: Optional[str],
) -> BoundedSummary:
    """Pinned facts and the compressed narrative, from two concurrent calls.

    The budget is split up front so neither call waits on the other: the
    pinned block gets at most half, the narrative the rest after its joiner
    and header. The cut therefore costs the slower call, not the sum, and
    never more than two calls whatever fails.
    """
    pinned_cap = budget_tokens // 2
    narrative_budget = budget_tokens - pinned_cap - _ceil_tokens(2 + len(NARRATIVE_HEADER) + 1)
    pinned, narrative = await asyncio.gather(
        extract_with_model(
            records,
            min(_EXTRACTION_MAX_OUTPUT_TOKENS, pinned_cap),
            model_id=model_id,
            region=region,
        ),
        compress_with_model(records, narrative_budget, model_id=model_id, region=region),
    )
    if pinned:
        # The pinned block, header included, stays inside its half.
        pinned = _keep_head_lines(pinned, (pinned_cap * CHARS_PER_TOKEN - len(PINNED_HEADER) - 1) // CHARS_PER_TOKEN)

    if not pinned:
        # Extraction failed: the narrative alone is a plain compressed summary.
        if narrative is not None:
            text = narrative if approx_tokens(narrative) <= budget_tokens else truncate_records_newest_first([narrative], budget_tokens)
            outcome = "model"
        else:
            text = truncate_records_newest_first(records, budget_tokens)
            outcome = "truncated_after_model"
        after = approx_tokens(text)
        logger.info(
            "compaction_summary_bounded: extraction failed; %s %d -> %d tokens (budget=%d)",
            outcome, before, after, budget_tokens,
        )
        return BoundedSummary(text, outcome, before, after)

    pinned_block = f"{PINNED_HEADER}\n{pinned}"
    outcome = "extract_then_compress"
    if narrative is None:
        narrative = truncate_records_newest_first(records, narrative_budget)
        outcome = "extract_then_truncate"
    elif approx_tokens(narrative) > narrative_budget:
        narrative = truncate_records_newest_first([narrative], narrative_budget)
    text = pinned_block
    if narrative:
        text = f"{pinned_block}\n\n{NARRATIVE_HEADER}\n{narrative}"
    after = approx_tokens(text)
    logger.info(
        "compaction_summary_bounded: %s %d -> %d tokens (pinned=%d, budget=%d)",
        outcome, before, after, approx_tokens(pinned_block), budget_tokens,
    )
    return BoundedSummary(text, outcome, before, after)


async def bound_summary(
    records: Sequence[str],
    budget_tokens: int,
    *,
    model_enabled: bool,
    model_id: str,
    region: Optional[str] = None,
    extract_enabled: bool = False,
) -> BoundedSummary:
    """Hold the summary built from ``records`` at or under ``budget_tokens``.

    ``extract_enabled`` pins verbatim facts ahead of the compressed narrative
    (see the module docstring); it needs ``model_enabled``. Never raises.
    """
    records = [r for r in records if isinstance(r, str) and r.strip()]
    joined = "\n\n".join(records) if records else None
    before = approx_tokens(joined)
    if not joined:
        return BoundedSummary(None, "empty", 0, 0)
    if before <= budget_tokens:
        return BoundedSummary(joined, "within_budget", before, before)

    if model_enabled and extract_enabled:
        return await _extract_then_compress(records, budget_tokens, before, model_id=model_id, region=region)

    if model_enabled:
        compressed = await compress_with_model(records, budget_tokens, model_id=model_id, region=region)
        if compressed is not None:
            after = approx_tokens(compressed)
            if after <= budget_tokens:
                logger.info(
                    "compaction_summary_bounded: model %d -> %d tokens (budget=%d)",
                    before, after, budget_tokens,
                )
                return BoundedSummary(compressed, "model", before, after)
            # The model overshot: truncate ITS output newest-first (its tail
            # holds the open items), rather than the raw records.
            trimmed = truncate_records_newest_first([compressed], budget_tokens)
            logger.info(
                "compaction_summary_bounded: model overshot (%d > %d); tail-trimmed to %d",
                after, budget_tokens, approx_tokens(trimmed),
            )
            return BoundedSummary(trimmed, "truncated_after_model", before, approx_tokens(trimmed))
        outcome = "truncated_after_model"
    else:
        outcome = "truncated"

    truncated = truncate_records_newest_first(records, budget_tokens)
    after = approx_tokens(truncated)
    logger.info(
        "compaction_summary_bounded: %s %d -> %d tokens (budget=%d)",
        outcome, before, after, budget_tokens,
    )
    return BoundedSummary(truncated, outcome, before, after)
