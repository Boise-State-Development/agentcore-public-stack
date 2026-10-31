"""Quota extension arithmetic: stacking, consumption order, funding keys.

Pure functions, no I/O, so the checker (turn path), the status read
(app-api) and the chargeback report (PR-4) all compute the same numbers
from the same inputs. See ``docs/specs/quota-requests-and-extensions.md``
§4.1 (stacking), §8.2 (checker), §9.1 (waterfall) and §7.3 (funding key).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

from apis.shared.timestamps import from_iso

from .models import FundingNotice, FundingSource, QuotaExtension, QuotaTier

# A tier or override at or above this monthly limit is unlimited. The checker,
# the status read and ``get_my_quota`` have all spelled it this way since
# unlimited overrides shipped as ``float('inf')`` tiers.
UNLIMITED_LIMIT_SENTINEL = 999999


def is_unlimited(tier: QuotaTier) -> bool:
    limit = tier.monthly_cost_limit
    return limit == float("inf") or limit >= UNLIMITED_LIMIT_SENTINEL


def period_base_limit(tier: QuotaTier) -> float:
    """The tier's limit for its own period: daily on a daily tier, else monthly."""
    if tier.period_type == "daily" and tier.daily_cost_limit is not None:
        return float(tier.daily_cost_limit)
    return float(tier.monthly_cost_limit)


def active_extensions(
    extensions: Iterable[QuotaExtension], now_iso: str
) -> List[QuotaExtension]:
    """The extensions in force at ``now_iso``.

    A resolution is cached for minutes, so one that was active at fill time
    can have ended since; filtering against the clock here costs nothing.
    """
    return [e for e in extensions if e.is_active(now_iso)]


def effective_limit(
    base_limit: float, extensions: Sequence[QuotaExtension]
) -> Optional[float]:
    """Base plus every extension's amount; None under an uncapped one (§8.2)."""
    if any(e.amount is None for e in extensions):
        return None
    return base_limit + sum(float(e.amount) for e in extensions)


def _order_group(ext: QuotaExtension) -> int:
    if ext.standing:
        return 2
    if ext.funding_mode == "chargeback":
        return 1
    return 0


def consumption_order(extensions: Iterable[QuotaExtension]) -> List[QuotaExtension]:
    """Central first, then capped chargeback, then standing (§9.1, decision 15).

    Within a group, shortest-lived first, then oldest. Money the institution
    already committed is spent before any fund is touched, and an uncapped
    arrangement absorbs whatever is left.
    """
    return sorted(
        extensions,
        key=lambda e: (_order_group(e), e.valid_until, e.created_at, e.extension_id),
    )


def attribute_overage(
    over: float, extensions: Iterable[QuotaExtension]
) -> List[Tuple[QuotaExtension, float]]:
    """Split spend above the base limit across extensions, in consumption order.

    Returns ``(extension, consumed)`` for every extension, consumed possibly 0.
    Whatever is left after the last extension is unattributed (a ``warn`` tier
    run past its effective limit), and the caller can derive it.
    """
    remaining = max(0.0, over)
    attributed: List[Tuple[QuotaExtension, float]] = []
    for ext in consumption_order(extensions):
        consumed = remaining if ext.amount is None else min(float(ext.amount), remaining)
        remaining -= consumed
        attributed.append((ext, consumed))
    return attributed


def funding_label(source: Optional[FundingSource]) -> str:
    """What a person reads for a fund: its label, else its fields in order."""
    if source is None:
        return "Central budget"
    if source.label and source.label.strip():
        return source.label.strip()
    parts = [f"{k} {v}".strip() for k, v in source.fields.items() if str(v).strip()]
    return " · ".join(parts) or "Funding source"


def build_funding_notice(
    current_usage: float,
    base_limit: float,
    extensions: Sequence[QuotaExtension],
) -> Optional[FundingNotice]:
    """The funding notice, when spend above the base lands on a chargeback extension.

    Names the chargeback extension currently being consumed (the last one in
    consumption order with any spend on it). Overage absorbed entirely by
    centrally funded extensions charges nobody and produces no notice.
    """
    if current_usage <= base_limit:
        return None

    charging: Optional[Tuple[QuotaExtension, float]] = None
    for ext, consumed in attribute_overage(current_usage - base_limit, extensions):
        if consumed > 0 and ext.funding_mode == "chargeback":
            charging = (ext, consumed)
    if charging is None:
        return None

    ext, consumed = charging
    cap = float(ext.amount) if ext.amount is not None else None
    return FundingNotice(
        label=funding_label(ext.funding_source),
        charged_this_period=round(consumed, 6),
        cap=cap,
        remaining_under_cap=round(max(0.0, cap - consumed), 6) if cap is not None else None,
        extension_id=ext.extension_id,
        standing=ext.standing,
    )


def compute_funding_key(
    fields: dict, schema_keys: Optional[Sequence[str]] = None
) -> Optional[str]:
    """Canonical ``key=value|key=value`` form of a funding source (§7.3).

    Schema order when the schema is known, otherwise sorted keys; values
    trimmed; blank values omitted. None when nothing is left, so a central
    extension never carries a key.
    """
    if not fields:
        return None
    keys = list(schema_keys) if schema_keys else sorted(fields)
    # A key the schema no longer lists still belongs to this fund.
    keys += sorted(k for k in fields if k not in keys)
    parts = []
    for key in keys:
        value = str(fields.get(key, "") or "").strip()
        if value:
            parts.append(f"{key.strip()}={value}")
    return "|".join(parts) or None


def periods_overlapping(valid_from: str, valid_until: str) -> List[str]:
    """Every ``YYYY-MM`` the window touches, inclusive at both ends (§7.2)."""
    start = from_iso(valid_from)
    end = from_iso(valid_until)
    if end < start:
        return []
    periods: List[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        periods.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return periods


def end_of_day_iso(day: date) -> str:
    """The last microsecond of a UTC calendar day, in the stored ISO spelling.

    A request's "through Nov 30" is inclusive, so the extension's
    ``validUntil`` is the end of that day.
    """
    moment = datetime(day.year, day.month, day.day, 23, 59, 59, 999999, tzinfo=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")
