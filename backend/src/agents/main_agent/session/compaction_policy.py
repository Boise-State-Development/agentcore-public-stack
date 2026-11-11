"""
Model-relative compaction policy.

Spec: docs/specs/compaction-model-relative-thresholds.md.

Three numbers per model, derived from the catalog's ``maxInputTokens``:

- **ceiling** — the trigger. ``min(window * CEILING_RATIO, CEILING_CAP_TOKENS)``.
  Capped on purpose: a 1M window does not mean a 500k chat prefix is a good
  idea — every warm turn re-reads it and every cache bust re-writes it.
- **floor** — the target size after a cut. ``ceiling * FLOOR_RATIO``. Under
  Bedrock caching a cut costs one re-write of what *survives*, so a small floor
  (deeper, rarer cuts) beats a shallow one (frequent re-writes).
- **hard ceiling** — force a cut even when hysteresis says wait.
  ``min(window * HARD_CEILING_RATIO, ceiling * HARD_CEILING_MULTIPLIER)``.

``COMPACTION_TOKEN_THRESHOLD`` (the historical fixed 100k) is the ceiling when
the window is unknown. With ``COMPACTION_MODEL_RELATIVE_ENABLED=false`` the
policy degrades to exactly the legacy behavior: fixed threshold, the
``cutoffs[-protected_turns]`` turn-count cut, no hysteresis.

The module also owns the token-aware cut selection (``choose_checkpoint``) and
the per-message estimator it uses. Estimates are calibrated against the turn's
real history token count, so the *ratio* between messages is what the
estimator has to get right, not the absolute number.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING

from agents.main_agent.core.model_config import (
    REASONING_REPLAY_BOUND,
    REASONING_REPLAY_PRESERVED,
    REASONING_REPLAY_STRIPPED,
    reasoning_replay_mode,
)

from .document_estimate import EMPTY_INDEX, DocumentIndex, slice_reference

if TYPE_CHECKING:
    from .compaction_models import CompactionConfig

logger = logging.getLogger(__name__)

# chars/4 is the same heuristic Strands falls back to when native CountTokens
# is unavailable; good enough because the estimates are rescaled to the
# measured history size before use.
CHARS_PER_TOKEN = 4
# Bedrock/Anthropic image tokens ~ (w*h)/750; dimensions are unknown here, so
# a flat figure that is roughly a 1000x1100 image.
IMAGE_TOKEN_ESTIMATE = 1_500
# Per-message framing overhead (role, block boundaries).
MESSAGE_OVERHEAD_TOKENS = 4
# Signature characters per billed token of a replayed Anthropic reasoning
# block. "Summarized" thinking stores a short summary as the block's text, but
# the signature carries the full thinking, so its length is what tracks the
# bill. Measured on dev 2026-11-11 (Haiku 5.5): 100,476 signature characters
# re-billed as 32,000 tokens (a thinking-only turn that hit the output cap),
# 86,168 as ~30.2k; a 4,060-character Sonnet 4.6 block billed 1,338.
REASONING_SIGNATURE_CHARS_PER_TOKEN = 3.1


@dataclass(frozen=True)
class CompactionPolicy:
    """Resolved thresholds for one turn on one model."""

    ceiling: int
    floor: Optional[int]
    hard_ceiling: Optional[int]
    context_window: Optional[int]
    # "model_relative" (window known), "fixed" (window unknown, using the
    # configured threshold), or "legacy" (kill switch off — turn-count cut).
    source: str

    @property
    def hysteresis_enabled(self) -> bool:
        return self.floor is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ceiling": self.ceiling,
            "floor": self.floor,
            "hardCeiling": self.hard_ceiling,
            "contextWindow": self.context_window,
            "source": self.source,
        }

    @classmethod
    def resolve(
        cls,
        config: "CompactionConfig",
        context_window: Optional[int],
    ) -> "CompactionPolicy":
        """Derive the policy for a model window under ``config``.

        ``context_window`` is the catalog's ``maxInputTokens`` for the model
        that served the turn, or ``None`` when the lookup missed.
        """
        window: Optional[int] = None
        if context_window is not None:
            try:
                window = int(context_window)
            except (TypeError, ValueError):
                window = None
            if window is not None and window <= 0:
                window = None

        if not config.model_relative_enabled:
            return cls(
                ceiling=max(1, int(config.token_threshold)),
                floor=None,
                hard_ceiling=None,
                context_window=window,
                source="legacy",
            )

        if window is not None:
            ceiling = int(min(window * config.ceiling_ratio, config.ceiling_cap_tokens))
            hard = int(min(window * config.hard_ceiling_ratio, ceiling * config.hard_ceiling_multiplier))
            source = "model_relative"
        else:
            ceiling = int(config.token_threshold)
            hard = int(ceiling * config.hard_ceiling_multiplier)
            source = "fixed"

        ceiling = max(1, ceiling)
        # The floor must leave hysteresis room below the ceiling.
        floor = max(0, min(ceiling - 1, int(ceiling * config.floor_ratio)))
        hard = max(ceiling, hard)
        return cls(
            ceiling=ceiling,
            floor=floor,
            hard_ceiling=hard,
            context_window=window,
            source=source,
        )


# ---------------------------------------------------------------------------
# Per-message token estimate
# ---------------------------------------------------------------------------

def _text_tokens(text: Any) -> int:
    if not isinstance(text, str):
        return 0
    return len(text) // CHARS_PER_TOKEN


def _json_tokens(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str)) // CHARS_PER_TOKEN
    except Exception:  # noqa: BLE001 - estimator must never raise
        return 0


def _scaled(tokens: int, documents: "DocumentIndex") -> int:
    """Characters / 4 at the conversation model's tokenizer factor, so text
    weighs against a document's page images as the model bills them."""
    factor = documents.text_factor
    return tokens if factor == 1.0 else int(round(tokens * factor))


def _reasoning_tokens(rc: Any, documents: "DocumentIndex", billed: Optional[bool]) -> int:
    """One replayed ``reasoningContent`` block.

    ``billed`` says whether the request this estimate stands for bills the
    block (``reasoning_billed``); ``None`` keeps the model-blind estimate, the
    stored text at chars/4. A billed signed block is its signature at
    ``REASONING_SIGNATURE_CHARS_PER_TOKEN``: the tokenizer factor does not
    apply, the constant is already in billed tokens. An unsigned block (not an
    Anthropic model) is its text.
    """
    if not isinstance(rc, dict):
        return 0
    reasoning_text = rc.get("reasoningText")
    reasoning_text = reasoning_text if isinstance(reasoning_text, dict) else {}
    signature = reasoning_text.get("signature")
    redacted = rc.get("redactedContent")
    if billed is None:
        return _scaled(_text_tokens(reasoning_text.get("text")), documents)
    if not billed:
        return 0
    if isinstance(signature, str) and signature:
        return int(len(signature) / REASONING_SIGNATURE_CHARS_PER_TOKEN)
    if isinstance(redacted, (bytes, bytearray)):
        # Opaque; not measured. Sized like a signature, the nearest thing.
        return int(len(redacted) / REASONING_SIGNATURE_CHARS_PER_TOKEN)
    return _scaled(_text_tokens(reasoning_text.get("text")), documents)


def reasoning_billed(model_id: Optional[str], *, live: bool, in_flight: bool, after_cut: bool) -> Optional[bool]:
    """Whether a request on ``model_id`` bills a replayed reasoning block.

    ``live``: nothing ahead of the block (system prompt, earlier history) has
    changed since it was produced. ``in_flight``: the block is in the turn the
    measured request belongs to (after its last user prompt). ``after_cut``:
    the request is the first after a compaction cut, which prepends the summary
    to the first retained message and so changes the history ahead of every
    retained block. Returns ``None`` when the model is unknown. The modes and
    their measurements are on ``model_config.REASONING_REPLAY_BOUND``.
    """
    if not model_id:
        return None
    mode = reasoning_replay_mode(model_id)
    if mode == REASONING_REPLAY_BOUND:
        return live and not after_cut
    if mode == REASONING_REPLAY_PRESERVED:
        return True
    if mode == REASONING_REPLAY_STRIPPED:
        return in_flight and not after_cut
    return True


def _block_tokens(block: Any, documents: "DocumentIndex", billed: Optional[bool] = None) -> int:
    if not isinstance(block, dict):
        return _scaled(_text_tokens(block), documents) if isinstance(block, str) else 0
    if "text" in block:
        return _scaled(_text_tokens(block.get("text")), documents)
    if "image" in block:
        return IMAGE_TOKEN_ESTIMATE
    if "document" in block:
        # The row's facts, or the bytes when there are none — never bytes/4
        # for a PDF, which missed the page images (§1.3 of the spec).
        return documents.block_tokens(block.get("document"))
    if "toolUse" in block:
        tool_use = block.get("toolUse") or {}
        return _scaled(_json_tokens(tool_use.get("input")), documents) + 8
    if "toolResult" in block:
        result = block.get("toolResult") or {}
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            return _tool_result_tokens(content, documents) + 8
        return _scaled(_json_tokens(content), documents) + 8
    if "json" in block:
        return _scaled(_json_tokens(block.get("json")), documents)
    if "reasoningContent" in block:
        return _reasoning_tokens(block.get("reasoningContent"), documents, billed)
    if "cachePoint" in block:
        return 0
    return _scaled(_json_tokens(block), documents)


def _tool_result_tokens(content: List[Any], documents: "DocumentIndex") -> int:
    """A tool result's blocks. A ``document_read`` slice is estimated as its
    pages' share of the whole document, which its JSON payload names."""
    upload_id, pages = slice_reference(content)
    total = 0
    for inner in content:
        if isinstance(inner, dict) and "document" in inner:
            total += documents.block_tokens(inner.get("document"), upload_id=upload_id, pages_covered=pages)
        else:
            total += _block_tokens(inner, documents)
    return total


def estimate_message_tokens(
    message: Dict[str, Any],
    documents: Optional["DocumentIndex"] = None,
    reasoning: Optional[bool] = None,
) -> int:
    """Heuristic token estimate for one Converse message (never raises).

    ``documents`` holds the row facts the conversation's documents are
    estimated from; without it every document falls back to its bytes. Its
    ``model_id`` prices text at that model's tokenizer factor, the planner's
    (``tokenizer_costs``): a document's page images do not scale with the
    tokenizer, so text and images are weighed as the model bills them.

    ``reasoning`` is whether the request being estimated bills the message's
    reasoning blocks (``reasoning_billed``): their signature length when it
    does, 0 when it does not. ``None`` keeps the stored text at chars/4.
    """
    if not isinstance(message, dict):
        return 0
    index = documents if documents is not None else EMPTY_INDEX
    content = message.get("content")
    if isinstance(content, str):
        return _scaled(_text_tokens(content), index) + MESSAGE_OVERHEAD_TOKENS
    if not isinstance(content, list):
        return MESSAGE_OVERHEAD_TOKENS
    return sum(_block_tokens(block, index, reasoning) for block in content) + MESSAGE_OVERHEAD_TOKENS


# ---------------------------------------------------------------------------
# Cut selection
# ---------------------------------------------------------------------------

def choose_checkpoint(
    messages: Sequence[Dict[str, Any]],
    cutoffs: Sequence[int],
    protected_turns: int,
    floor_tokens: int,
    history_tokens: Optional[int],
    tail_bound_tokens: Optional[int] = None,
    documents: Optional[DocumentIndex] = None,
    reasoning_live_from: Optional[int] = None,
) -> Tuple[int, Optional[int]]:
    """Pick the cut (index into ``messages``) that lands retained history at or
    below ``floor_tokens`` while keeping as much as fits.

    Returns ``(relative_cut, retained_estimate)``. ``relative_cut == 0`` means
    nothing to cut. Candidates are ``cutoffs`` (tool-pair-safe turn starts) no
    newer than ``cutoffs[-protected_turns]``; the last ``protected_turns``
    turns are kept. Among candidates that satisfy the floor the **oldest**
    wins.

    If none does — the protected tail alone exceeds the floor — the
    protection is still kept whole while the tail fits ``tail_bound_tokens``
    (the policy's ceiling): the minimum-protection cut is returned and the
    caller logs it as ``floor_unreachable``. When the tail alone exceeds that
    bound, the protection **shrinks by whole turns**: the cut moves to the
    oldest turn start inside the tail whose retained estimate fits the bound,
    and never past ``cutoffs[-1]`` — the newest turn, whose own user message
    always survives. A tail of one turn that is still over the bound keeps
    that one turn. ``tail_bound_tokens=None`` (the kill switch, and every
    caller before the bound existed) keeps the whole tail whatever its size.

    The same bound is what lets a conversation with no more than
    ``protected_turns`` turns be cut at all: without it such a conversation
    returns ``(0, None)``, with it the whole history is the tail and shrinks
    the same way.

    The result is a pure function of the arguments, which the caller derives
    from the live list and the turn's measured usage, so whichever agent
    instance serves the turn computes the same cut; the caller only ever moves
    the checkpoint forward.

    ``history_tokens`` is the measured size of the conversation portion of
    the prompt this turn; per-message estimates are rescaled to sum to it so
    only their ratios matter. ``None`` leaves the raw estimates as-is.

    ``documents`` holds the row facts the document blocks are estimated from
    (``document_estimate``). It only changes estimates, so it only moves where
    a cut lands, never the bytes of what the cut keeps. Its ``model_id`` also
    says how the model bills replayed reasoning (``reasoning_billed``).

    Reasoning is priced twice, because the measured request and the request
    after the cut bill it differently. The calibration sum prices what the
    turn's last call billed: the blocks it re-sent, and not the reasoning of
    the turn's final assistant message, which that call wrote. The
    retained estimate is what the first call after the cut will bill: on
    Haiku 5.5 / Opus 5.5 nothing, since the summary changes the history ahead
    of every retained block. ``reasoning_live_from`` is the live-list index
    from which the history ahead of a block has not changed since it was
    produced (``CompactionState.reasoning_live_from``); ``None`` means
    nothing has changed since the conversation began.
    """
    protected_turns = max(0, int(protected_turns))
    if not cutoffs:
        return 0, None
    young = len(cutoffs) <= protected_turns
    if young and tail_bound_tokens is None:
        return 0, None

    # Position (in ``cutoffs``) of the oldest protected turn start.
    tail_start = 0 if young else (len(cutoffs) - protected_turns if protected_turns > 0 else len(cutoffs) - 1)
    newest_allowed = cutoffs[tail_start]
    n = len(messages)
    if n == 0:
        return (0, None) if young else (int(newest_allowed), None)

    model_id = documents.model_id if documents is not None else None
    live_from = max(0, int(reasoning_live_from or 0))
    in_flight_from = int(cutoffs[-1])

    # The turn's last call wrote the final assistant message rather than read
    # it, so that message's reasoning is not in the measured size.
    answer = n - 1 if isinstance(messages[-1], dict) and messages[-1].get("role") == "assistant" else None

    def _estimate(i: int, after_cut: bool) -> int:
        billed = reasoning_billed(
            model_id, live=i >= live_from, in_flight=i >= in_flight_from, after_cut=after_cut,
        )
        if billed and not after_cut and i == answer:
            billed = False
        return estimate_message_tokens(messages[i], documents, billed)

    raw_total = sum(_estimate(i, after_cut=False) for i in range(n))
    estimates = [_estimate(i, after_cut=True) for i in range(n)]
    scale = 1.0
    if history_tokens is not None and history_tokens > 0 and raw_total > 0:
        scale = history_tokens / raw_total

    # suffix[i] = estimated tokens retained if we cut at i (keep messages[i:])
    suffix = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] + estimates[i]

    def retained(cut: int) -> int:
        idx = min(max(int(cut), 0), n)
        return int(round(suffix[idx] * scale))

    if not young:
        candidates = [c for c in cutoffs if c <= newest_allowed]
        fitting = [c for c in candidates if retained(c) <= floor_tokens]
        if fitting:
            cut = min(fitting)
            return int(cut), retained(cut)

    tail_tokens = retained(newest_allowed)
    shrink = [c for c in cutoffs[tail_start + 1:] if c > 0]
    if tail_bound_tokens is None or tail_tokens <= int(tail_bound_tokens) or not shrink:
        if young:
            return 0, None
        logger.info(
            "compaction_floor_unreachable: protected tail alone is ~%d tokens "
            "(floor=%d); taking the minimum-protection cut at %d",
            tail_tokens, floor_tokens, newest_allowed,
        )
        return int(newest_allowed), tail_tokens

    bound = int(tail_bound_tokens)
    fitting_tail = [c for c in shrink if retained(c) <= bound]
    cut = min(fitting_tail) if fitting_tail else shrink[-1]
    kept_turns = len(cutoffs) - cutoffs.index(cut)
    logger.info(
        "compaction_tail_bounded: protected tail is ~%d tokens (> bound=%d, "
        "floor=%d); keeping the newest %d turn(s) from %d, retained≈%d",
        tail_tokens, bound, floor_tokens, kept_turns, cut, retained(cut),
    )
    return int(cut), retained(cut)
