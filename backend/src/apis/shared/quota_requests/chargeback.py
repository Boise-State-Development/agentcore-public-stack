"""The chargeback report: §9.1 per period, the lazy close, re-close, funds, CSV.

docs/specs/quota-requests-and-extensions.md §9. Two paths, one shape:

- **Open period** (the current month, or a past one inside its 48-hour grace
  window): read the period's pointer items, load each extension, read each
  user's period spend once, run the waterfall, and return the lines marked
  ``provisional``. Nothing is written.
- **Closed period**: the materialised lines under ``QUOTA_CHARGE#{period}``.
  A past period's first read after the grace window closes it, by the same
  computation, conditional on nobody else having closed it.

All of this runs on app-api's admin surface. Nothing here is on the turn path.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from agents.main_agent.quota.extensions import period_waterfall
from agents.main_agent.quota.models import QuotaExtension
from apis.shared.audit.models import TARGET_QUOTA_PERIOD, AuditAction
from apis.shared.audit.service import get_audit_service
from apis.shared.auth.models import User
from apis.shared.timestamps import from_iso, to_iso

from .ledger import (
    ChargeLedgerRepository,
    get_charge_ledger_repository,
    line_sort_key,
)
from .models import ChargebackPeriod, QuotaChargeLine, QuotaRequestPolicy

logger = logging.getLogger(__name__)

# Cost rows written in the last minutes of a month land within this (§9.4).
CLOSE_GRACE = timedelta(hours=48)
# A claim older than this whose close never finished is taken over.
STALE_CLAIM = timedelta(minutes=5)
MAX_REPORT_PERIODS = 24
_CENTS = Decimal("0.000001")
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

CONSUMPTION_ORDER_NOTE = (
    "Spend above the base limit is attributed to extensions in this order: centrally funded, "
    "then capped chargeback, then standing; within each, earliest end date first, then oldest. "
    "Consumed is actual spend, never the granted amount."
)

SpendReader = Callable[[str, str], Awaitable[Decimal]]


class ChargebackError(Exception):
    status_code = 400

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.message = message
        self.extra = extra


class ChargebackConflict(ChargebackError):
    status_code = 409


# ── periods ────────────────────────────────────────────────────────────


def validate_period(period: str) -> str:
    if not _PERIOD_RE.match(period or ""):
        raise ChargebackError("A period is YYYY-MM")
    return period


def _next_month(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    return f"{year + (month == 12):04d}-{month % 12 + 1:02d}"


def period_start(period: str) -> datetime:
    return datetime(int(period[:4]), int(period[5:7]), 1, tzinfo=timezone.utc)


def period_end(period: str) -> datetime:
    """The first instant after the period."""
    return period_start(_next_month(period))


def closes_at(period: str) -> datetime:
    return period_end(period) + CLOSE_GRACE


def current_period(now: datetime) -> str:
    return now.strftime("%Y-%m")


def last_complete_period(now: datetime) -> str:
    first = now.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def periods_between(start: str, end: str) -> List[str]:
    validate_period(start)
    validate_period(end)
    if end < start:
        raise ChargebackError("The range ends before it starts")
    periods = [start]
    while periods[-1] < end:
        periods.append(_next_month(periods[-1]))
    if len(periods) > MAX_REPORT_PERIODS:
        raise ChargebackError(f"A report covers at most {MAX_REPORT_PERIODS} months")
    return periods


def _overlaps(extension: QuotaExtension, period: str) -> bool:
    """Active at any moment of the period, on its current (revoke-clipped) window."""
    return (
        extension.enabled
        and from_iso(extension.valid_from) < period_end(period)
        and from_iso(extension.valid_until) >= period_start(period)
    )


def ledger_fund_key(extension: QuotaExtension) -> Optional[str]:
    """The fund a line is filed under: None for central, else the funding key.

    A deployment with no funding fields can still charge back to a fund named
    only by its label; that fund has no ``fundingKey``, so it is keyed by its
    label. Schema keys start with a letter, so ``_label=`` cannot collide.
    """
    if extension.funding_mode != "chargeback":
        return None
    if extension.funding_key:
        return extension.funding_key
    label = ((extension.funding_source.label if extension.funding_source else None) or "").strip()
    return f"_label={label}" if label else "_unspecified"


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENTS)


# ── filters ────────────────────────────────────────────────────────────


@dataclass
class ChargebackFilters:
    # A funding key, or "central" for the central budget.
    funding_key: Optional[str] = None
    group: Optional[str] = None
    user_id: Optional[str] = None
    authorizer: Optional[str] = None
    # central | chargeback | standing
    mode: Optional[str] = None
    # f_<field>=value, keyed by the field key without the prefix.
    fields: Dict[str, str] = field(default_factory=dict)

    def index_family(self) -> Optional[Tuple[str, str]]:
        """The first filter an index can answer across periods (§9.3)."""
        for family, value in (
            # The central budget has no fund index; its lines are per period.
            ("fundingKey", self.funding_key if self.funding_key != "central" else None),
            ("userId", self.user_id),
            ("authorizer", self.authorizer),
            ("group", self.group),
        ):
            if value:
                return family, value
        return None

    def matches(self, line: QuotaChargeLine) -> bool:
        if self.funding_key and line.fund_sort_key != self.funding_key:
            return False
        if self.group and line.group_value != self.group:
            return False
        if self.user_id and line.user_id != self.user_id:
            return False
        if self.authorizer and (line.authorizer_email or "").lower() != self.authorizer.lower():
            return False
        if self.mode == "standing" and not line.standing:
            return False
        if self.mode in ("central", "chargeback") and line.funding_mode != self.mode:
            return False
        for key, value in self.fields.items():
            if (line.fields.get(key) or "") != value:
                return False
        return True


def fund_totals(lines: Iterable[QuotaChargeLine]) -> Dict[str, str]:
    """Consumed per fund (``central`` for the central budget), for the re-close audit."""
    totals: Dict[str, Decimal] = {}
    for line in lines:
        totals[line.fund_sort_key] = totals.get(line.fund_sort_key, Decimal("0")) + line.consumed
    return {k: f"{v:.2f}" for k, v in sorted(totals.items())}


# ── the service ────────────────────────────────────────────────────────


async def _default_spend(user_id: str, period: str) -> Decimal:
    """The user's aggregated spend in the period. Raises on a read failure.

    Unlike the request context's usage read, a failure here must not turn
    into $0: a close would write a statement that charges nobody.
    """
    from apis.shared.quota import get_cost_aggregator

    summary = await get_cost_aggregator().get_user_cost_summary(user_id=user_id, period=period)
    return Decimal(str(round(float(summary.total_cost), 6)))


class ChargebackService:
    def __init__(
        self,
        *,
        ledger: Optional[ChargeLedgerRepository] = None,
        policy_loader: Optional[Callable[[], Awaitable[QuotaRequestPolicy]]] = None,
        spend_reader: Optional[SpendReader] = None,
        clock: Optional[Callable[[], datetime]] = None,
        extension_lister: Optional[Callable[[], Awaitable[List[QuotaExtension]]]] = None,
    ):
        self._ledger = ledger
        self._policy_loader = policy_loader
        self._spend = spend_reader or _default_spend
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._extension_lister = extension_lister

    @property
    def ledger(self) -> ChargeLedgerRepository:
        if self._ledger is None:
            self._ledger = get_charge_ledger_repository()
        return self._ledger

    async def _policy(self) -> QuotaRequestPolicy:
        if self._policy_loader is not None:
            return await self._policy_loader()
        from .repository import get_quota_request_repository

        return await get_quota_request_repository().get_policy()

    async def _all_extensions(self) -> List[QuotaExtension]:
        if self._extension_lister is not None:
            return await self._extension_lister()
        from apis.shared.quota import get_quota_repository

        return await get_quota_repository().list_extensions()

    # ── §9.1 for one period ─────────────────────────────────────────────

    async def compute_period(
        self, period: str, policy: QuotaRequestPolicy
    ) -> Tuple[List[QuotaChargeLine], Decimal]:
        """Every extension active in the period, as lines, plus unattributed spend."""
        ids = await self.ledger.period_extension_ids(period)
        extensions = [e for e in await self.ledger.get_extensions(ids) if _overlaps(e, period)]
        by_user: Dict[str, List[QuotaExtension]] = {}
        for ext in extensions:
            by_user.setdefault(ext.user_id, []).append(ext)

        users = sorted(by_user)
        limiter = asyncio.Semaphore(16)

        async def _read(user_id: str) -> Decimal:
            async with limiter:
                return await self._spend(user_id, period)

        spends = await asyncio.gather(*(_read(u) for u in users))

        group_field = policy.report_group_field
        lines: List[QuotaChargeLine] = []
        unattributed = Decimal("0")
        for user_id, spend in zip(users, spends):
            waterfall = period_waterfall(spend, by_user[user_id])
            unattributed += waterfall.unattributed
            for ext, consumed in waterfall.consumed:
                source = ext.funding_source if ext.funding_mode == "chargeback" else None
                fields = {k: str(v).strip() for k, v in (source.fields if source else {}).items() if str(v).strip()}
                lines.append(QuotaChargeLine(
                    period=period,
                    user_id=ext.user_id,
                    user_email=ext.user_email,
                    extension_id=ext.extension_id,
                    request_id=ext.request_id,
                    funding_mode=ext.funding_mode,
                    funding_key=ledger_fund_key(ext),
                    funding_label=(source.label or "").strip() if source else "",
                    authorizer_email=(source.authorizer_email or None) if source else None,
                    fields=fields,
                    group_value=fields.get(group_field) if group_field else None,
                    standing=ext.standing,
                    granted=ext.amount,
                    consumed=_q(consumed),
                    base_limit=waterfall.base,
                    base_limit_ambiguous=waterfall.base_ambiguous,
                    period_spend=_q(waterfall.spend),
                    approved_by=ext.created_by_email or ext.created_by,
                    approved_at=ext.created_at,
                    valid_until=ext.valid_until,
                    revoked_at=ext.revoked_at,
                ))
        lines.sort(key=line_sort_key)
        return lines, _q(unattributed)

    # ── closing (§9.4) ──────────────────────────────────────────────────

    async def _close(
        self, period: str, policy: QuotaRequestPolicy, *, version_claim: Callable[[str], Awaitable[Optional[int]]],
        closed_by: str,
    ) -> Tuple[ChargebackPeriod, List[QuotaChargeLine], bool]:
        """Compute, claim, write, finish. Returns (period, lines, wrote).

        The computation runs before the claim, so a failed spend read leaves
        the period unclaimed. When another close holds the claim, the computed
        lines come back provisional and nothing is written.
        """
        lines, unattributed = await self.compute_period(period, policy)
        now = to_iso(self._clock())
        version = await version_claim(now)
        if version is None:
            return (
                ChargebackPeriod(period=period, status="provisional", closable=True, unattributed=unattributed),
                lines,
                False,
            )
        for line in lines:
            line.status = "closed"
            line.closed_at = now
            line.close_version = version
        existing = await self.ledger.period_lines(period)
        await self.ledger.replace_lines(period, lines, [line_sort_key(l) for l in existing])
        finished = await self.ledger.finish_close(
            period, version=version, claimed_at=now, closed_at=now, closed_by=closed_by,
            summary={"unattributed": unattributed, "lineCount": len(lines)},
        )
        if not finished:
            # Our claim was taken over as stale while we wrote; the taker's lines win.
            raise ChargebackConflict("Another close of this period took over; reload the report")
        logger.info(
            "Chargeback period closed",
            extra={"event": "quota_period_closed", "period": period, "close_version": version, "lines": len(lines)},
        )
        return (
            ChargebackPeriod(
                period=period, status="closed", closed_at=now, close_version=version,
                closable=True, unattributed=unattributed,
            ),
            lines,
            True,
        )

    async def _lazy_close(self, period: str, policy: QuotaRequestPolicy) -> Tuple[ChargebackPeriod, List[QuotaChargeLine]]:
        stale_before = to_iso(self._clock() - STALE_CLAIM)
        result, lines, _ = await self._close(
            period, policy, closed_by="system",
            version_claim=lambda now: self.ledger.claim_first_close(period, now=now, stale_before=stale_before),
        )
        return result, lines

    @staticmethod
    def _closed_period(marker: dict) -> ChargebackPeriod:
        return ChargebackPeriod(
            period=marker["period"],
            status="closed",
            closed_at=marker.get("closedAt"),
            close_version=int(marker["closeVersion"]),
            closable=True,
            unattributed=marker.get("unattributed", Decimal("0")),
        )

    # ── the report (§9.3) ───────────────────────────────────────────────

    def default_period(self) -> str:
        """The last complete month, the report's default (§6.6)."""
        return last_complete_period(self._clock())

    async def report(self, periods: List[str], filters: ChargebackFilters) -> Dict[str, Any]:
        now = self._clock()
        if periods[-1] > current_period(now):
            raise ChargebackError("The report cannot cover a period that has not started")
        policy = await self._policy()

        states: List[ChargebackPeriod] = []
        lines: List[QuotaChargeLine] = []
        already_closed: List[str] = []
        for period in periods:
            if now < closes_at(period):
                computed, unattributed = await self.compute_period(period, policy)
                states.append(ChargebackPeriod(period=period, status="provisional", unattributed=unattributed))
                lines.extend(computed)
                continue
            marker = await self.ledger.get_marker(period)
            if marker and marker.get("status") == "closed":
                states.append(self._closed_period(marker))
                already_closed.append(period)
                continue
            state, computed = await self._lazy_close(period, policy)
            states.append(state)
            lines.extend(computed)

        lines.extend(await self._read_closed(already_closed, filters))
        lines = [l for l in lines if filters.matches(l)]
        lines.sort(key=lambda l: (l.period, line_sort_key(l)))
        return self._shape(states, lines, policy)

    async def _read_closed(self, periods: List[str], filters: ChargebackFilters) -> List[QuotaChargeLine]:
        if not periods:
            return []
        indexed = filters.index_family()
        if indexed and len(periods) > 1:
            family, value = indexed
            wanted = set(periods)
            found = await self.ledger.index_lines(family, value, periods[0], periods[-1])
            return [l for l in found if l.period in wanted]
        out: List[QuotaChargeLine] = []
        for period in periods:
            out.extend(await self.ledger.period_lines(period, fund_prefix=filters.funding_key))
        return out

    @staticmethod
    def _shape(states: List[ChargebackPeriod], lines: List[QuotaChargeLine], policy: QuotaRequestPolicy) -> Dict[str, Any]:
        central = sum((l.consumed for l in lines if l.funding_mode == "central"), Decimal("0"))
        charged = sum((l.consumed for l in lines if l.funding_mode == "chargeback"), Decimal("0"))
        return {
            "periods": [s.model_dump(by_alias=True, mode="json") for s in states],
            "lines": [l.model_dump(by_alias=True, mode="json") for l in lines],
            "totals": {
                "central": f"{central:.2f}",
                "chargeback": f"{charged:.2f}",
                "consumed": f"{central + charged:.2f}",
                "unattributed": f"{sum((s.unattributed for s in states), Decimal('0')):.2f}",
            },
            "fundingFields": [f.model_dump(by_alias=True) for f in policy.funding_fields],
            "groupField": policy.report_group_field,
            "consumptionOrder": CONSUMPTION_ORDER_NOTE,
        }

    # ── re-close ────────────────────────────────────────────────────────

    async def reclose(self, admin: User, period: str) -> Dict[str, Any]:
        """Recalculate a closed period (§9.4): bump the version, rewrite, audit."""
        validate_period(period)
        now = self._clock()
        if now < closes_at(period):
            raise ChargebackConflict(
                "This period is still open. It closes 48 hours after it ends; until then the report is provisional.",
                closesAt=to_iso(closes_at(period)),
            )
        policy = await self._policy()
        marker = await self.ledger.get_marker(period)
        before_lines = await self.ledger.period_lines(period) if marker else []

        if marker and marker.get("status") == "closed":
            from_version = int(marker["closeVersion"])
            state, after_lines, wrote = await self._close(
                period, policy, closed_by=admin.user_id,
                version_claim=lambda at: self.ledger.claim_reclose(period, from_version=from_version, now=at),
            )
        else:
            state, after_lines = await self._lazy_close(period, policy)
            wrote = state.status == "closed"
        if not wrote:
            raise ChargebackConflict("Another close of this period is in progress; try again in a moment")

        before, after = fund_totals(before_lines), fund_totals(after_lines)
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        get_audit_service().record(
            action=AuditAction.QUOTA_PERIOD_RECLOSED,
            actor=admin,
            target_type=TARGET_QUOTA_PERIOD,
            target_id=period,
            changes=changed,
            before={"closeVersion": int(marker["closeVersion"]) if marker else None, "funds": before},
            after={"closeVersion": state.close_version, "funds": after},
        )
        return {
            "period": state.model_dump(by_alias=True, mode="json"),
            "before": before,
            "after": after,
            "changedFunds": changed,
        }

    # ── funds (the report's fund picker) ────────────────────────────────

    async def funds(self) -> List[Dict[str, Any]]:
        """Every funding key ever granted against, with lifetime consumed in closed periods."""
        from agents.main_agent.quota.extensions import periods_overlapping

        by_key: Dict[str, Dict[str, Any]] = {}
        for ext in await self._all_extensions():
            key = ledger_fund_key(ext)
            if key is None:
                continue
            periods = periods_overlapping(ext.valid_from, ext.valid_until) or [ext.valid_from[:7]]
            entry = by_key.setdefault(key, {
                "fundingKey": key, "label": "", "fields": {}, "authorizerEmail": None,
                "firstPeriod": periods[0], "lastPeriod": periods[-1], "extensions": 0, "_latest": "",
            })
            entry["extensions"] += 1
            entry["firstPeriod"] = min(entry["firstPeriod"], periods[0])
            entry["lastPeriod"] = max(entry["lastPeriod"], periods[-1])
            if ext.created_at >= entry["_latest"]:
                # The most recent grant names the fund the way people now know it.
                entry["_latest"] = ext.created_at
                source = ext.funding_source
                entry["label"] = (source.label or "").strip() if source else ""
                entry["fields"] = dict(source.fields) if source else {}
                entry["authorizerEmail"] = source.authorizer_email if source else None

        async def _consumed(key: str) -> Decimal:
            return sum((l.consumed for l in await self.ledger.fund_lines(key)), Decimal("0"))

        keys = sorted(by_key)
        totals = await asyncio.gather(*(_consumed(k) for k in keys))
        out = []
        for key, total in zip(keys, totals):
            entry = by_key[key]
            entry.pop("_latest")
            entry["consumedClosed"] = f"{total:.2f}"
            out.append(entry)
        return out


# ── CSV (§9.5) ─────────────────────────────────────────────────────────

_FIXED_COLUMNS = (
    "period", "userEmail", "extensionId", "requestId", "fundingMode",
)
_TRAILING_COLUMNS = (
    "authorizerEmail", "granted", "consumed", "approvedBy", "approvedAt", "revokedAt",
    "fundingLabel", "standing", "baseLimit", "baseLimitChanged", "periodStatus",
)


def _cell(value: Any) -> str:
    """A CSV cell that a spreadsheet will not evaluate as a formula."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _money(value: Optional[Decimal]) -> str:
    return "" if value is None else f"{value:.2f}"


def report_csv(report: Dict[str, Any], schema_keys: List[str]) -> str:
    """One row per line, funding fields flattened into one column per schema key.

    Keys a line carries that the schema no longer lists follow the schema's
    own, so an old row stays readable; a line that predates a key leaves its
    column blank.
    """
    lines = [QuotaChargeLine(**l) for l in report["lines"]]
    extra = sorted({k for l in lines for k in l.fields} - set(schema_keys))
    field_keys = list(schema_keys) + extra
    reserved = set(_FIXED_COLUMNS) | set(_TRAILING_COLUMNS)
    headers = (
        list(_FIXED_COLUMNS)
        + [f"f_{k}" if k in reserved else k for k in field_keys]
        + list(_TRAILING_COLUMNS)
    )
    status = {p["period"]: p["status"] for p in report["periods"]}

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(headers)
    for l in lines:
        writer.writerow([_cell(v) for v in (
            l.period, l.user_email, l.extension_id, l.request_id or "", l.funding_mode,
            *(l.fields.get(k, "") for k in field_keys),
            l.authorizer_email or "",
            "uncapped" if l.granted is None else _money(l.granted),
            _money(l.consumed),
            l.approved_by, l.approved_at, l.revoked_at or "",
            l.funding_label,
            "true" if l.standing else "false",
            _money(l.base_limit),
            "true" if l.base_limit_ambiguous else "false",
            status.get(l.period, l.status),
        )])
    return out.getvalue()


_service: Optional[ChargebackService] = None


def get_chargeback_service() -> ChargebackService:
    global _service
    if _service is None:
        _service = ChargebackService()
    return _service

