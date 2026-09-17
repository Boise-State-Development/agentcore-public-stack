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
# Cut selection
# ---------------------------------------------------------------------------

def choose_checkpoint(
    messages: Sequence[Dict[str, Any]],
    cutoffs: Sequence[int],
    protected_turns: int,
    floor_tokens: int,
    history_tokens: Optional[int],
) -> Tuple[int, Optional[int]]:
    """Pick the cut (index into ``messages``) that lands retained history at or
    below ``floor_tokens`` while keeping as much as fits.

    Returns ``(relative_cut, retained_estimate)``. ``relative_cut == 0`` means
    nothing to cut. Candidates are ``cutoffs`` (tool-pair-safe turn starts) no
    newer than ``cutoffs[-protected_turns]``; the last ``protected_turns``
    turns are always kept. Among candidates that satisfy the floor the
    **oldest** wins. If none does — the protected tail alone exceeds the floor
    — the minimum-protection cut is returned and the caller logs it (evicting
    inside the tail is the offload escalation, a later PR, not a deeper cut).

    ``history_tokens`` is the measured size of the conversation portion of
    the prompt this turn; per-message estimates are rescaled to sum to it so
    only their ratios matter. ``None`` leaves the raw estimates as-is.
    """
    protected_turns = max(0, int(protected_turns))
    if not cutoffs or len(cutoffs) <= protected_turns:
        return 0, None

    newest_allowed = cutoffs[-protected_turns] if protected_turns > 0 else cutoffs[-1]
    n = len(messages)
    if n == 0:
        return int(newest_allowed), None

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

    candidates = [c for c in cutoffs if c <= newest_allowed]
    fitting = [c for c in candidates if retained(c) <= floor_tokens]
    if fitting:
        cut = min(fitting)
        return int(cut), retained(cut)

    logger.info(
        "compaction_floor_unreachable: protected tail alone is ~%d tokens "
        "(floor=%d); taking the minimum-protection cut at %d",
        retained(newest_allowed), floor_tokens, newest_allowed,
    )
    return int(newest_allowed), retained(newest_allowed)
