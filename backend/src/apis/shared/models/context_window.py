"""Resolve a model's context window from the catalog, with an SDK fallback.

Issue #267, re-scoped. The original ask was "fall back to Strands' lookup
table when ``maxInputTokens`` is ``None``". That is half the fix, and taken
literally the other half is dangerous — see :func:`resolve_context_window`
for why **our** value wins whenever we have one.

**Why this exists.** ``maxInputTokens`` on a managed-model record governs two
things: the SPA's context badge, and — since #1125 — the *model-relative
compaction policy*, which cuts at ``window * COMPACTION_CEILING_RATIO``. A
stale row therefore does not merely mis-render a badge; it compacts
conversations early, paying a prefix re-write and a summarizer call sooner
than the model requires, and dropping context the model could have held.

That is not hypothetical. Measured 2026-09-21: the curated catalog had
Claude Sonnet 4.6 and Claude Opus 4.7 inheriting the 200,000 default while
both model cards publish **1M**, and dev's Claude Sonnet 5 row carried
200,000 against a catalog that declares 1,000,000 — three enabled models
cutting at 100k instead of 500k. Nothing detected it, because a plausible
number is indistinguishable from a correct one.

**The precedence, and the trap in reversing it.** Our catalog value wins.
It is operator-editable and can encode a *deliberate* cap that is not the
model's real window: ``maxInputTokens: 272_000`` on the hosted OpenAI rows
is load-bearing pricing (those models have a 1M window, but AWS bills input
at 2x and output at 1.5x above 272K, and ``CuratedModel`` holds one flat
rate per bucket). Preferring the SDK there would silently open the second
price card and under-charge every long turn. So the SDK table is a
**fallback for absence**, never an override — and a disagreement is
surfaced for a human rather than resolved in code.
"""

from __future__ import annotations

import logging
from typing import Optional, Set, Tuple

logger = logging.getLogger(__name__)

#: Model ids whose catalog/SDK disagreement has already been logged. A
#: disagreement is a property of the pair, not of the turn, so logging it
#: once per process keeps a deliberate cap (the 272K pricing tier) from
#: emitting a line on every model call.
_DISAGREEMENT_LOGGED: Set[str] = set()


def sdk_context_window(model_id: Optional[str]) -> Optional[int]:
    """Strands' built-in context-window table for ``model_id``, or ``None``.

    Covers Anthropic-on-Bedrock and handles the cross-region prefix strip
    (``us.anthropic.claude-sonnet-4-6`` resolves via ``anthropic.…``). Since
    strands-agents 1.56 the strip also walks nested prefixes, so the hosted
    OpenAI ids resolve too: ``us.openai.gpt-6-astra`` and the GPT-5.6 family
    return their full 1,050,000 window. That is the real window but **not**
    our pricing cap — the curated rows' ``maxInputTokens: 272_000`` still wins
    in :func:`resolve_context_window`, and a row with the field *absent* would
    now fall through to 1,050,000. Kimi K3 and DeepSeek still return ``None``,
    so this remains a partial safety net, not a second source of truth.

    ⚠️ ``strands.models._defaults`` is a private module on an experimental
    surface. Import failure is treated as "no fallback available", never as
    an error: a Strands bump that moves it degrades this to today's
    behaviour instead of breaking a turn. The standing watch on the
    context-manager package (`docs/kaizen/review-queue.md`, 2026-09-21)
    covers re-checking it on each bump.
    """
    if not model_id:
        return None
    try:
        from strands.models._defaults import get_context_window_limit

        value = get_context_window_limit(model_id)
        return int(value) if value else None
    except Exception:  # noqa: BLE001 - a missing fallback is not a failure
        logger.debug("Strands context-window lookup unavailable", exc_info=True)
        return None


def resolve_context_window(
    model_id: Optional[str], catalog_value: Optional[object]
) -> Tuple[Optional[int], Optional[str]]:
    """The window to use, and a note naming the source or the conflict.

    Precedence, and the reasoning is in the module docstring:

    1. ``catalog_value`` when present — ours wins, because it may encode a
       deliberate policy cap that is *not* the model's real window.
    2. Strands' table when ours is absent — issue #267's actual ask.
    3. ``None`` — the caller reports "not tracked" and the compaction policy
       falls back to its fixed threshold, as before.

    When both are present and differ, the catalog value is still returned
    and a warning names both, **once per model id per process**. The guard
    is a detector, not a resolver: silently taking either side would have
    either kept the three stale rows above or broken the 272K pricing cap.
    """
    catalog: Optional[int] = None
    if catalog_value:
        try:
            catalog = int(catalog_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            catalog = None
        if catalog is not None and catalog <= 0:
            catalog = None

    sdk = sdk_context_window(model_id)

    if catalog is not None and sdk is not None and catalog != sdk:
        key = str(model_id)
        if key not in _DISAGREEMENT_LOGGED:
            _DISAGREEMENT_LOGGED.add(key)
            logger.warning(
                "context_window_disagreement model_id=%s catalog=%d sdk=%d using=catalog | "
                "Verify against the AWS model card before changing anything: a deliberate "
                "pricing cap (e.g. the 272K tier) looks identical to a stale row here, and "
                "the catalog value drives model-relative compaction.",
                key,
                catalog,
                sdk,
            )
        return catalog, "disagreement"

    if catalog is not None:
        return catalog, "catalog"
    if sdk is not None:
        logger.info(
            "context_window_fallback model_id=%s sdk=%d | no maxInputTokens on the "
            "managed-model record; using Strands' table",
            model_id,
            sdk,
        )
        return sdk, "sdk"
    return None, None
