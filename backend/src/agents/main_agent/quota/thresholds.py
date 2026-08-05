"""Warning-threshold ladder and per-session notice share.

Pure functions, no I/O — ``QuotaChecker`` calls them per turn and
``apis.shared.quota`` re-exports them (this module lives here only because
``apis.shared.quota`` imports from this package, so importing it back would
be circular).

**Why the ladder exists** (`docs/specs/compaction-over-threshold-cache-spiral.md`
§2 D5): in the 2026-08-05 prod incident every warning event — 80% and 90% —
fired on the same day the hard block landed. A month-long budget spent by one
pathological conversation gives the user hours of notice, which is not runway.
The 50%/75% rungs and the per-session notice below exist to move that signal
days earlier.
"""

import os
from decimal import Decimal
from typing import List, Optional, Sequence

# Kill switch for both halves of the runway (earlier rungs + session notice).
# Default ON — set to "false" to restore the pre-#833 soft-limit/90% pair and
# silence the per-session notice, without a deploy of new code.
QUOTA_RUNWAY_ENABLED_ENV = "QUOTA_RUNWAY_ENABLED"


def quota_runway_enabled() -> bool:
    """Whether the earlier-warning ladder and session notice are active.

    Read per call (no module-level caching) so tests and live config changes
    behave predictably; the env read is negligible next to the DynamoDB
    lookups it sits beside.
    """
    return os.environ.get(QUOTA_RUNWAY_ENABLED_ENV, "").lower() != "false"


# Rungs added below the tier's own soft limit. A tier may override them
# (``QuotaTier.early_warning_percentages``); an explicit empty list opts a
# tier out and restores the pre-#833 80%/90% behavior.
DEFAULT_EARLY_WARNING_PERCENTAGES: Sequence[float] = (50.0, 75.0)

# The rung that is always present at the top, independent of the tier's
# configured soft limit.
CRITICAL_WARNING_PERCENTAGE: float = 90.0

# Share of the *monthly* limit a single conversation has to reach before the
# user is told about that conversation specifically. Tier-overridable via
# ``QuotaTier.session_notice_percentage``; 0 disables the notice.
DEFAULT_SESSION_NOTICE_PERCENTAGE: float = 25.0


def _to_float(value) -> Optional[float]:
    """Best-effort numeric coercion — tier fields arrive as Decimal."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_warning_thresholds(tier) -> List[float]:
    """Build the ascending warning ladder for *tier*.

    The tier's ``soft_limit_percentage`` (default 80) and the fixed 90% rung
    are always present, so this is strictly additive to the behavior that
    shipped before the ladder existed. Duplicates collapse — a tier whose
    soft limit is 75 gets [50, 75, 90], not two 75s.
    """
    early = getattr(tier, "early_warning_percentages", None)
    if not quota_runway_enabled():
        early_values: List[float] = []
    elif early is None:
        early_values = list(DEFAULT_EARLY_WARNING_PERCENTAGES)
    else:
        early_values = [v for v in (_to_float(e) for e in early) if v is not None]

    soft_limit = _to_float(getattr(tier, "soft_limit_percentage", None))
    thresholds = {v for v in early_values if 0 < v <= 100}
    if soft_limit is not None and 0 < soft_limit <= 100:
        thresholds.add(soft_limit)
    thresholds.add(CRITICAL_WARNING_PERCENTAGE)

    return sorted(thresholds)


def format_threshold(threshold: float) -> str:
    """Render a rung the way the SSE event and the event log carry it.

    Whole percentages stay whole ("50%") so existing rows, dashboards, and
    the ``record_warning_if_needed`` dedup key keep matching.
    """
    if float(threshold).is_integer():
        return f"{int(threshold)}%"
    return f"{threshold:g}%"


def select_warning_level(percentage_used: float, thresholds: Sequence[float]) -> str:
    """Return the highest crossed rung as a label, or ``"none"``.

    Highest-crossed rather than nearest, so a user who jumps from 40% to 95%
    in one turn is told 90%, not 50%.
    """
    crossed = [t for t in thresholds if percentage_used >= t]
    if not crossed:
        return "none"
    return format_threshold(max(crossed))


def resolve_session_notice_percentage(tier) -> float:
    """Share of the monthly limit at which a single session is called out."""
    configured = _to_float(getattr(tier, "session_notice_percentage", None))
    if configured is None:
        return DEFAULT_SESSION_NOTICE_PERCENTAGE
    if configured < 0:
        return 0.0
    return configured


def session_notice_threshold_usd(limit: float, tier) -> Optional[float]:
    """Dollar value of the per-session notice share, or None when disabled."""
    if not quota_runway_enabled():
        return None
    percentage = resolve_session_notice_percentage(tier)
    if percentage <= 0 or limit <= 0:
        return None
    return limit * percentage / 100.0


def to_decimal(value: float) -> Decimal:
    """Decimal conversion for the DynamoDB-bound quota models."""
    return Decimal(str(value))
