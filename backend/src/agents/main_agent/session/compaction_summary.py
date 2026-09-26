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

A generation that hits its output ceiling is **salvaged**, not discarded:
its complete lines are kept, trimmed from the end to the budget. The prompt
orders the summary by value (standing instructions first), so a head cut
short still beats newest-first truncation of the raw records, which drops
the oldest instructions first. Nova 2 Lite's narrative length varies several
times over on the same records, so the ceiling is hit in normal use.

Whatever comes out is persisted verbatim in ``CompactionState.summary`` and
prepended byte-identically at every restore, so the byte-stability contract
is unchanged: the summary still only mutates at checkpoint advance.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .compaction_policy import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

# Bound the *input* to the compression call too: a wide history of records is
# fed newest-first up to this many chars, so the side-channel's own spend is
# flat regardless of how long the session has run.
MAX_COMPRESSION_INPUT_CHARS = 120_000
# ``maxTokens`` for the compression call is min(the model's ceiling, the
# budget); the budget check after generation is what enforces the budget.
# Unlisted models get 4k, under Nova Micro's 5k output ceiling (the smallest of
# the models this has run on), so a ``summary_model_id`` override is never
# rejected for asking too much. Listed models are matched by substring, so the
# ``us.`` / ``global.`` profiles and bare ids all resolve.
_MODEL_MAX_OUTPUT_TOKENS = 4_000
_MAX_OUTPUT_TOKENS_BY_MODEL: Tuple[Tuple[str, int], ...] = (
    # Model card: 64K max output tokens. At the default 8k budget this lets
    # maxTokens match the budget the prompt's word limit is sized to.
    ("amazon.nova-2-lite", 64_000),
)

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


def approx_tokens(text: Optional[str]) -> int:
    """chars/4 — the same estimate the admin SUMMARY_OVER_BUDGET diagnosis uses."""
    if not text:
        return 0
    return len(text) // CHARS_PER_TOKEN


@dataclass(frozen=True)
class BoundedSummary:
    text: Optional[str]
    # "within_budget" | "model" | "model_salvaged" | "truncated"
    # | "truncated_after_model" | "empty"
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


def _max_output_tokens(model_id: str) -> int:
    for fragment, ceiling in _MAX_OUTPUT_TOKENS_BY_MODEL:
        if fragment in model_id:
            return ceiling
    return _MODEL_MAX_OUTPUT_TOKENS


def _keep_head_lines(text: str, budget_tokens: int) -> Optional[str]:
    """The leading whole lines of ``text`` that fit ``budget_tokens``."""
    budget_chars = max(0, int(budget_tokens)) * CHARS_PER_TOKEN
    if len(text) <= budget_chars:
        return text
    head = text[:budget_chars]
    cut = head.rfind("\n")
    head = head[:cut] if cut > 0 else ""
    return head.rstrip() or None


def _salvage(text: str, budget_tokens: int) -> Optional[str]:
    """The complete lines of a generation cut off by ``maxTokens``, within budget.

    Its last line is dropped as possibly partial. Anything still over budget
    is trimmed from the end, never the head: the head holds the standing
    instructions, and the end is already missing.
    """
    cut = text.rfind("\n")
    head = text[:cut].rstrip() if cut > 0 else ""
    return _keep_head_lines(head, budget_tokens) if head else None


async def _compress(
    records: Sequence[str],
    budget_tokens: int,
    *,
    model_id: str,
    region: Optional[str],
) -> Tuple[Optional[str], bool]:
    """``compress_with_model``, plus whether its text was salvaged from a cut-off generation."""
    text = _compression_input(records)
    if not text.strip():
        return None, False
    try:
        import boto3
    except ImportError:  # pragma: no cover - dev without boto3
        return None, False
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
                "maxTokens": min(_max_output_tokens(model_id), max(256, int(budget_tokens))),
            },
        )
        out = response["output"]["message"]["content"][0]["text"].strip()
        if response.get("stopReason") == "max_tokens":
            salvaged = _salvage(out, budget_tokens)
            logger.info(
                "compaction_summary_model_truncated: generation hit the token ceiling; kept %d of %d chars",
                len(salvaged or ""), len(out),
            )
            return salvaged, salvaged is not None
        return out or None, False
    except Exception:  # noqa: BLE001 - a summary is never worth an error
        logger.warning("compaction_summary_model_failed: falling back to truncation", exc_info=True)
        return None, False


async def compress_with_model(
    records: Sequence[str],
    budget_tokens: int,
    *,
    model_id: str,
    region: Optional[str] = None,
) -> Optional[str]:
    """One bounded Bedrock ``converse`` call. Returns ``None`` on any failure.

    A generation cut off by ``maxTokens`` returns its complete lines, held to
    ``budget_tokens`` (``None`` if none survive).

    Side-channel by construction: its own messages, never ``agent.messages``.
    """
    compressed, _ = await _compress(records, budget_tokens, model_id=model_id, region=region)
    return compressed


async def bound_summary(
    records: Sequence[str],
    budget_tokens: int,
    *,
    model_enabled: bool,
    model_id: str,
    region: Optional[str] = None,
) -> BoundedSummary:
    """Hold the summary built from ``records`` at or under ``budget_tokens``."""
    records = [r for r in records if isinstance(r, str) and r.strip()]
    joined = "\n\n".join(records) if records else None
    before = approx_tokens(joined)
    if not joined:
        return BoundedSummary(None, "empty", 0, 0)
    if before <= budget_tokens:
        return BoundedSummary(joined, "within_budget", before, before)

    if model_enabled:
        compressed, salvaged = await _compress(records, budget_tokens, model_id=model_id, region=region)
        if compressed is not None:
            after = approx_tokens(compressed)
            if after <= budget_tokens:
                outcome = "model_salvaged" if salvaged else "model"
                logger.info(
                    "compaction_summary_bounded: %s %d -> %d tokens (budget=%d)",
                    outcome, before, after, budget_tokens,
                )
                return BoundedSummary(compressed, outcome, before, after)
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
