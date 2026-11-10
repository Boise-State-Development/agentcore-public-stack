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


def _bytes_tokens(source: Any) -> int:
    if not isinstance(source, dict):
        return 0
    raw = source.get("bytes")
    if isinstance(raw, (bytes, bytearray)):
        return len(raw) // CHARS_PER_TOKEN
    return 0


def _block_tokens(block: Any) -> int:
    if not isinstance(block, dict):
        return _text_tokens(block) if isinstance(block, str) else 0
    if "text" in block:
        return _text_tokens(block.get("text"))
    if "image" in block:
        return IMAGE_TOKEN_ESTIMATE
    if "document" in block:
        doc = block.get("document") or {}
        return _bytes_tokens(doc.get("source")) if isinstance(doc, dict) else 0
    if "toolUse" in block:
        tool_use = block.get("toolUse") or {}
        return _json_tokens(tool_use.get("input")) + 8
    if "toolResult" in block:
        result = block.get("toolResult") or {}
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            return sum(_block_tokens(inner) for inner in content) + 8
        return _json_tokens(content) + 8
    if "json" in block:
        return _json_tokens(block.get("json"))
    if "reasoningContent" in block:
        rc = block.get("reasoningContent") or {}
        text = rc.get("reasoningText", {}) if isinstance(rc, dict) else {}
        return _text_tokens(text.get("text") if isinstance(text, dict) else None)
    if "cachePoint" in block:
        return 0
    return _json_tokens(block)


def estimate_message_tokens(message: Dict[str, Any]) -> int:
    """Heuristic token estimate for one Converse message (never raises)."""
    if not isinstance(message, dict):
        return 0
    content = message.get("content")
    if isinstance(content, str):
        return _text_tokens(content) + MESSAGE_OVERHEAD_TOKENS
    if not isinstance(content, list):
        return MESSAGE_OVERHEAD_TOKENS
    return sum(_block_tokens(block) for block in content) + MESSAGE_OVERHEAD_TOKENS


# ---------------------------------------------------------------------------
# Held documents (unified-attachment-delivery §5.2, measurement only)
# ---------------------------------------------------------------------------

#: ``estimate_document_tokens`` by ``(format, hash of the bytes)``: a PDF's
#: estimate parses its page count, so each held document is measured once per
#: process, not once per turn.
_DOCUMENT_TOKENS: Dict[Tuple[str, int], int] = {}
_DOCUMENT_TOKENS_MAX = 64


def _document_block_tokens(document: Any) -> int:
    if not isinstance(document, dict):
        return 0
    source = document.get("source")
    raw = source.get("bytes") if isinstance(source, dict) else None
    if not isinstance(raw, (bytes, bytearray)):
        return 0
    fmt = str(document.get("format") or "").strip().lower()
    key = (fmt, hash(bytes(raw)))
    cached = _DOCUMENT_TOKENS.get(key)
    if cached is not None:
        return cached
    try:
        from apis.shared.files.document_tokens import estimate_document_tokens

        tokens = int(estimate_document_tokens(fmt, raw))
    except Exception:  # noqa: BLE001 - estimator must never raise
        tokens = len(raw) // CHARS_PER_TOKEN
    if len(_DOCUMENT_TOKENS) >= _DOCUMENT_TOKENS_MAX:
        _DOCUMENT_TOKENS.clear()
    _DOCUMENT_TOKENS[key] = tokens
    return tokens


def held_document_tokens(messages: Optional[Sequence[Dict[str, Any]]]) -> int:
    """Estimated tokens of the document blocks the conversation holds in context.

    Only the user-attached blocks (top level of a message): a ``document_read``
    slice is a tool result and counts as conversation. The estimate is
    ``estimate_document_tokens`` (pages × 1,500 for a PDF, bytes/4 otherwise)
    until phase 2's row facts exist. Never raises.
    """
    total = 0
    for message in messages or ():
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and "document" in block:
                total += _document_block_tokens(block.get("document"))
    return total


def chat_input_tokens(
    config: "CompactionConfig",
    input_tokens: int,
    messages: Optional[Sequence[Dict[str, Any]]],
) -> Tuple[int, int]:
    """``(tokens the chat ceiling compares, held document tokens subtracted)``.

    With ``ceiling_excludes_documents`` off, which is every deployment, this
    is ``(input_tokens, 0)`` and nothing is read. On, the held documents are
    subtracted (never below zero); the hard ceiling keeps the full input.
    """
    if not getattr(config, "ceiling_excludes_documents", False):
        return int(input_tokens), 0
    held = held_document_tokens(messages)
    return max(0, int(input_tokens) - held), held


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

    estimates = [estimate_message_tokens(m) for m in messages]
    raw_total = sum(estimates)
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
