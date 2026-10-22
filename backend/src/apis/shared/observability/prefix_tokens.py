"""Plausibility check for the stable ``{system, tools}`` prefix split.

``toolTokens`` is a **residual** between two independently sourced numbers
(Strands' projection for the upcoming request, minus our own CountTokens call
without tools), so any disagreement between those two estimators lands wholly
in it — see the module docstring of
``agents/main_agent/session/hooks/context_attribution.py``. That module already
refuses to compute the split on the two failure modes it can predict: a turn
carrying inline document/image bytes, and the OpenAI-surface providers whose
``count_tokens`` is a heuristic.

What was missing is a check on the *result*. Measured on prod session
``7f5f207f`` (2026-09-21): a row reported ``tools = 223,782`` next to a real
prompt of **55,783** tokens (``inputTokens + cacheRead + cacheWrite``) — the
tool schemas alone claiming 4x the entire prompt they sit inside. The same
user's other session the previous day reported 13,606 then 4,624 on
consecutive turns whose system prompt was byte-identical.

The invariant is arithmetic, not a heuristic: the static prefix is a *subset*
of the prompt, so ``system + tools`` can never exceed the prompt the provider
actually billed. A split that violates it is stale or corrupt, and the ledger's
established convention is that an **absent** ``prefixTokens`` reads "not
tracked" (which every consumer already handles) while a wrong one silently
corrupts every share computed from it.

Lives in ``apis.shared`` because both sides of the boundary need the same rule:
``agents/`` applies it before persisting a row, ``app_api/`` applies it when
reading rows written before the guard existed. Duplicating a three-line
invariant in two packages is exactly how the two copies drift apart.
"""

from typing import Any, Mapping, Optional

__all__ = ["prompt_tokens_from_usage", "prefix_split_is_plausible"]


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def prompt_tokens_from_usage(usage: Optional[Mapping[str, Any]]) -> Optional[int]:
    """The prompt the provider actually billed, from a ``tokenUsage`` mapping.

    ``inputTokens + cacheReadInputTokens + cacheWriteInputTokens`` — the same
    sum the SPA's context meter uses, and the only total sourced entirely from
    provider-reported usage rather than from an estimator.

    Returns ``None`` when the mapping is missing or sums to zero, which reads
    "unknown" rather than "empty prompt": an unknown total must not be used to
    reject a split.
    """
    if not isinstance(usage, Mapping):
        return None
    total = (
        _as_int(usage.get("inputTokens"))
        + _as_int(usage.get("cacheReadInputTokens"))
        + _as_int(usage.get("cacheWriteInputTokens"))
    )
    return total if total > 0 else None


def prefix_split_is_plausible(
    system_tokens: int,
    tool_tokens: int,
    prompt_tokens: Optional[int],
) -> bool:
    """Whether ``system + tools`` can be a real subset of ``prompt_tokens``.

    ``prompt_tokens=None`` means the turn's real total is unknown, and an
    unknown total proves nothing — the split is kept. Negative partitions are
    rejected outright; the hook clamps at 0, so a negative here means the value
    was decoded from somewhere else.

    This deliberately catches only the provably-impossible case. A residual
    that is merely *suspicious* (the 13,606 -> 4,624 swing above) still passes,
    because the arithmetic cannot distinguish it from a real tool-set change.
    Narrowing that gap needs the root cause, not a tighter bound here.
    """
    if system_tokens < 0 or tool_tokens < 0:
        return False
    if prompt_tokens is None:
        return True
    return (system_tokens + tool_tokens) <= prompt_tokens
