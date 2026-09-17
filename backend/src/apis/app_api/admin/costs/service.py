"""Admin cost dashboard service.

Provides methods for retrieving system-wide cost metrics, top users by cost,
model usage breakdowns, and cost trends for the admin dashboard.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List

from apis.shared.storage.dynamodb_storage import DynamoDBStorage
from .diagnoses import (
    CHARS_PER_TOKEN,
    Diagnosis,
    ProfileFacts,
    compaction_token_threshold,
    run_diagnoses,
    top_severity,
)
from .models import (
    CompactionEvent,
    DocumentReads,
    PrefixTokens,
    AttachmentProfile,
    ContextTrajectoryPoint,
    DataCoverage,
    FeedbackByTurnClass,
    FeedbackCounts,
    FeedbackProfile,
    FingerprintChanges,
    SessionDiagnosis,
    SessionProfile,
    ToolCensusEntry,
    TopUserCost,
    TopSessionCost,
    TopSessionsResponse,
    SystemCostSummary,
    ModelUsageSummary,
    TierUsageSummary,
    CostTrend,
    AdminCostDashboard,
    PrefixFingerprints,
    SessionCallRow,
    SessionCostAnatomy,
    UserSessionSummary,
    UserSessionsResponse,
)

logger = logging.getLogger(__name__)

# Bound on concurrent per-user lookups when enriching the top-users table.
_ENRICH_CONCURRENCY = 10


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _record_cost(record: Dict[str, Any]) -> Optional[float]:
    """A C# row's cost: a breakdown dict on the streaming path, a bare float on
    the legacy path, or absent (unknown — never zero)."""
    raw = record.get("cost")
    if isinstance(raw, dict):
        raw = raw.get("total")
    return _as_float(raw)


def _turn_class(record: Dict[str, Any]) -> Optional[str]:
    """Turn class of one ``C#`` row from its document-context fields (spec
    §6.1): ``full`` (``hasDocuments``), ``retrieved`` (``documentReads.pages
    > 0``), ``digestOnly`` (``documentDigests > 0``), else ``none``.

    Precedence is full > retrieved > digestOnly: a call that pulled pages back
    still holds the digest, so testing the digest first would leave the
    retrieved arm — the one the quality gate is about — permanently empty.
    ``None`` when the row carries none of the fields (written before #1137
    or with diagnostics off), so the caller says "not tracked", not "none".
    """
    has_documents = record.get("hasDocuments")
    digests = record.get("documentDigests")
    reads = record.get("documentReads")
    if has_documents is None and digests is None and reads is None:
        return None
    if has_documents:
        return "full"
    pages = _as_int(reads.get("pages")) if isinstance(reads, dict) else _as_int(reads)
    if (pages or 0) > 0:
        return "retrieved"
    if (_as_int(digests) or 0) > 0:
        return "digestOnly"
    return "none"


def _join_feedback(
    records: List[Dict[str, Any]],
    feedback_rows: List[Dict[str, Any]],
) -> FeedbackProfile:
    """Join ``F#`` rows to ``C#`` rows on ``messageId`` and bucket by turn
    class. Pure; the profile's numbers, never any content. Explicit thumbs
    only (``signal`` absent or ``"explicit"``)."""
    by_message: Dict[int, Dict[str, Any]] = {}
    for record in records:
        message_id = _as_int(record.get("messageId"))
        if message_id is not None:
            # The last call of a multi-call turn is the one the user thumbed;
            # rows share a messageId only across the turn's tool round trips
            # and later rows have the fuller context, so last write wins.
            by_message[message_id] = record

    any_turn_class = any(_turn_class(r) is not None for r in records)
    buckets = FeedbackByTurnClass() if any_turn_class else None
    profile = FeedbackProfile()
    # Every cost row per assistant message index, for pricing rework.
    cost_by_message: Dict[int, float] = {}
    for record in records:
        message_id = _as_int(record.get("messageId"))
        if message_id is not None:
            cost_by_message[message_id] = cost_by_message.get(message_id, 0.0) + (_record_cost(record) or 0.0)
    rework_total: Optional[float] = None
    for row in feedback_rows:
        # Explicit thumbs only — implicit signals (spec §10) share the row
        # family but answer a different question and must never be summed in.
        if row.get("signal") not in (None, "explicit"):
            continue
        value = _as_int(row.get("value"))
        if value not in (1, -1):
            continue
        if value == 1:
            profile.up += 1
        else:
            profile.down += 1
        message_id = _as_int(row.get("messageId"))
        retry_id = _as_int(row.get("retryMessageId"))
        if value == -1 and retry_id is not None:
            profile.retried += 1
            rework = _rework_cost(cost_by_message, message_id, retry_id)
            if rework is not None:
                rework_total = (rework_total or 0.0) + rework
        record = by_message.get(message_id) if message_id is not None else None
        if record is None:
            profile.unjoined += 1
            continue
        if buckets is None:
            continue
        klass = _turn_class(record) or "none"
        bucket: FeedbackCounts = {
            "full": buckets.full,
            "digestOnly": buckets.digest_only,
            "retrieved": buckets.retrieved,
        }.get(klass, buckets.none)
        if value == 1:
            bucket.up += 1
        else:
            bucket.down += 1
    profile.by_turn_class = buckets
    profile.rework_usd = round(rework_total, 6) if rework_total is not None else None
    return profile


def _rework_cost(
    cost_by_message: Dict[int, float],
    thumbed_message_id: Optional[int],
    retry_message_id: int,
) -> Optional[float]:
    """Dollars spent on a down-thumbed answer plus its retry: the thumbed
    message's call rows, plus the retry turn's assistant rows. The retry is
    a *user* message (no cost row); its turn's assistant messages are the
    consecutive indexes after it — a gap means the next user message. ``None``
    when neither side has a cost row to price."""
    found = False
    total = 0.0
    if thumbed_message_id is not None and thumbed_message_id in cost_by_message:
        total += cost_by_message[thumbed_message_id]
        found = True
    index = retry_message_id + 1
    while index in cost_by_message:
        total += cost_by_message[index]
        found = True
        index += 1
    return total if found else None


def _context_tokens(record: Dict[str, Any]) -> int:
    """True context occupancy of one call: uncached input + cached prefix +
    newly cached tokens (Bedrock reports the three disjointly)."""
    usage = record.get("tokenUsage") or {}
    return (
        int(usage.get("inputTokens") or 0)
        + int(usage.get("cacheReadInputTokens") or 0)
        + int(usage.get("cacheWriteInputTokens") or 0)
    )


from dataclasses import dataclass as _dataclass, field as _field


@_dataclass
class _CallLedger:
    """The context-ledger fields of one cost row, decoded and diffed."""

    prefix_tokens: Optional[PrefixTokens] = None
    removed: Optional[int] = None
    trimmed: Optional[int] = None
    events: List[CompactionEvent] = _field(default_factory=list)
    #: The row's document context fields, decoded (``None`` = not tracked).
    documents: Optional[Dict[str, Any]] = None
    document_reads: Optional[DocumentReads] = None


_DOCUMENT_INT_FIELDS = (
    "documentCount", "documentTokens", "documentDigests", "documentsAttached",
    "documentSlices", "documentSliceTokens",
)


def _call_documents(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The row's document context (``hasDocuments`` and the counts), or
    ``None`` when the row predates the fields. Ints coerced, the format map
    kept as ``{format: count}``."""
    if "hasDocuments" not in record:
        return None
    out: Dict[str, Any] = {"hasDocuments": bool(record.get("hasDocuments"))}
    for key in _DOCUMENT_INT_FIELDS:
        value = _as_int(record.get(key))
        if value is not None:
            out[key] = value
    mime = record.get("documentMime")
    if isinstance(mime, dict):
        out["documentMime"] = {
            str(k): (_as_int(v) or 0) for k, v in mime.items()
        }
    return out


def _call_document_reads(record: Dict[str, Any]) -> Optional[DocumentReads]:
    raw = record.get("documentReads")
    if not isinstance(raw, dict):
        return None
    return DocumentReads(
        calls=_as_int(raw.get("calls")) or 0,
        pages=_as_int(raw.get("pages")) or 0,
        bytes=_as_int(raw.get("bytes")) or 0,
    )


def _document_row_fields(ledger: "_CallLedger") -> Dict[str, Any]:
    """``SessionCallRow`` kwargs for the row's document context — empty when
    the row predates the fields, so they render as null ("not tracked")."""
    fields: Dict[str, Any] = {}
    docs = ledger.documents
    if docs is not None:
        fields["has_documents"] = docs.get("hasDocuments")
        fields["document_count"] = docs.get("documentCount")
        fields["document_tokens"] = docs.get("documentTokens")
        fields["document_digests"] = docs.get("documentDigests")
        fields["documents_attached"] = docs.get("documentsAttached")
        fields["document_slices"] = docs.get("documentSlices")
        fields["document_slice_tokens"] = docs.get("documentSliceTokens")
        fields["document_mime"] = docs.get("documentMime")
    if ledger.document_reads is not None:
        fields["document_reads"] = ledger.document_reads
    return fields


def _call_ledger(record: Dict[str, Any], previous_removed: Optional[int]) -> _CallLedger:
    """Decode a cost row's ``prefixTokens`` / ``windowRemovedMessages`` /
    ``compactionEvents`` / document context and derive ``trimmed`` (messages
    removed since the previous ledger-bearing row). Absent fields stay
    ``None`` — "not tracked", never 0 — and malformed ones are ignored rather
    than raised.
    """
    ledger = _CallLedger()
    ledger.documents = _call_documents(record)
    ledger.document_reads = _call_document_reads(record)
    raw_prefix = record.get("prefixTokens")
    if isinstance(raw_prefix, dict):
        try:
            ledger.prefix_tokens = PrefixTokens(
                system=int(raw_prefix.get("system") or 0),
                tools=int(raw_prefix.get("tools") or 0),
            )
        except (TypeError, ValueError):
            ledger.prefix_tokens = None
    removed = _as_int(record.get("windowRemovedMessages"))
    if removed is not None:
        ledger.removed = removed
        ledger.trimmed = (
            max(removed - previous_removed, 0) if previous_removed is not None else 0
        )
    raw_events = record.get("compactionEvents")
    if isinstance(raw_events, list):
        for entry in raw_events:
            if not isinstance(entry, dict) or not entry.get("kind"):
                continue
            try:
                ledger.events.append(CompactionEvent(**entry))
            except (TypeError, ValueError):
                continue
    return ledger


class AdminCostService:
    """Service for admin cost dashboard operations."""

    def __init__(
        self,
        storage: Optional[DynamoDBStorage] = None,
        user_repository: Any = None,
        quota_resolver: Any = None,
        file_repository: Any = None,
    ):
        """
        Initialize the admin cost service.

        Args:
            storage: Optional DynamoDB storage instance. If not provided,
                     a new instance will be created.
            user_repository: Optional ``UserRepository`` for top-users
                     enrichment (email). Resolved lazily when omitted.
            quota_resolver: Optional ``QuotaResolver`` for top-users
                     enrichment (tier, quota %). Resolved lazily when omitted.
            file_repository: Optional ``FileUploadRepository`` for the session
                     profile's attachment stats. Resolved lazily when omitted.
        """
        self.storage = storage or DynamoDBStorage()
        self._user_repository = user_repository
        self._quota_resolver = quota_resolver
        self._file_repository = file_repository

    # Lazy collaborators. `getattr` defaults so a test that builds the service
    # with `__new__` and sets only `storage` still works.

    def _users(self):
        repo = getattr(self, "_user_repository", None)
        if repo is None:
            from apis.shared.users.repository import UserRepository
            repo = UserRepository()
            self._user_repository = repo
        return repo

    def _quota(self):
        resolver = getattr(self, "_quota_resolver", None)
        if resolver is None:
            from apis.shared.quota import get_quota_resolver
            resolver = get_quota_resolver()
            self._quota_resolver = resolver
        return resolver

    def _files(self):
        repo = getattr(self, "_file_repository", None)
        if repo is None:
            from apis.shared.files.repository import get_file_upload_repository
            repo = get_file_upload_repository()
            self._file_repository = repo
        return repo

    def _get_current_period(self) -> str:
        """Get the current month period in YYYY-MM format."""
        now = datetime.now(timezone.utc)
        return f"{now.year}-{now.month:02d}"

    def _get_current_date(self) -> str:
        """Get the current date in YYYY-MM-DD format."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _get_period_date_range(self, period: str) -> tuple[str, str]:
        """
        Get the start and end dates for a monthly period.

        Args:
            period: Period in YYYY-MM format

        Returns:
            Tuple of (start_date, end_date) in YYYY-MM-DD format
        """
        year, month = map(int, period.split("-"))

        # First day of month
        start_date = f"{year}-{month:02d}-01"

        # Last day of month
        if month == 12:
            next_month_first = datetime(year + 1, 1, 1)
        else:
            next_month_first = datetime(year, month + 1, 1)

        last_day = next_month_first - timedelta(days=1)
        end_date = last_day.strftime("%Y-%m-%d")

        return start_date, end_date

    async def get_top_users(
        self,
        period: Optional[str] = None,
        limit: int = 100,
        min_cost: Optional[float] = None,
        tier_id: Optional[str] = None
    ) -> List[TopUserCost]:
        """
        Get top users by cost for a period.

        Uses the PeriodCostIndex GSI for efficient sorted queries.

        Args:
            period: The billing period (YYYY-MM format). Defaults to current month.
            limit: Maximum number of users to return (1-1000, default 100).
            min_cost: Optional minimum cost threshold in dollars.
            tier_id: Optional tier ID filter (not yet implemented).

        Returns:
            List of TopUserCost sorted by cost descending.
        """
        period = period or self._get_current_period()
        logger.info("Getting top users by cost for period")

        try:
            users_data = await self.storage.get_top_users_by_cost(
                period=period,
                limit=min(limit, 1000),
                min_cost=min_cost
            )

            result = []
            for user_data in users_data:
                result.append(TopUserCost(
                    user_id=user_data.get("userId", ""),
                    total_cost=user_data.get("totalCost", 0.0),
                    total_requests=user_data.get("totalRequests", 0),
                    last_updated=user_data.get("lastUpdated", ""),
                    email=None,
                    tier_name=None,
                    quota_limit=None,
                    quota_percentage=None
                ))

            await self._enrich_top_users(result)

            logger.info("Retrieved top users for period")
            return result

        except Exception as e:
            logger.error(f"Error getting top users: {e}")
            raise

    async def _enrich_top_users(self, rows: List[TopUserCost]) -> None:
        """Fill in ``email`` / ``tierName`` / ``quotaLimit`` / ``quotaPercentage``.

        The table's job is to make the user who is about to hit their quota
        visible without opening every row, so the quota share matters more
        than the email. Best-effort throughout: the users table may be
        unconfigured (a fork without the BFF user store), and one user's
        lookup failing must not blank the column for the rest. Bounded
        concurrency so a 100-row page is ~10 round-trips deep, not 100.
        """
        try:
            users = self._users()
        except Exception as e:  # noqa: BLE001 - enrichment is optional
            logger.debug("Top-users enrichment unavailable: %s", e)
            return
        if not getattr(users, "enabled", False) or not rows:
            return

        from apis.shared.auth import User

        quota = self._quota()
        semaphore = asyncio.Semaphore(_ENRICH_CONCURRENCY)

        async def enrich(row: TopUserCost) -> None:
            async with semaphore:
                try:
                    profile = await users.get_user_by_user_id(row.user_id)
                    if profile is None:
                        return
                    row.email = profile.email
                    resolved = await quota.resolve_user_quota(User(
                        user_id=profile.user_id,
                        email=profile.email,
                        name=profile.name,
                        roles=profile.roles,
                    ))
                    tier = getattr(resolved, "tier", None) if resolved else None
                    if tier is None:
                        return
                    row.tier_name = getattr(tier, "tier_name", None)
                    limit = getattr(tier, "monthly_cost_limit", None)
                    if limit is None or limit == float("inf"):
                        return
                    limit = float(limit)
                    if limit <= 0:
                        return
                    row.quota_limit = limit
                    row.quota_percentage = round(row.total_cost / limit * 100, 1)
                except Exception as e:  # noqa: BLE001 - one user must not blank the page
                    logger.debug("Top-users enrichment skipped for a user: %s", e)

        await asyncio.gather(*(enrich(row) for row in rows))

    async def get_system_summary(
        self,
        period: Optional[str] = None,
        period_type: str = "monthly"
    ) -> SystemCostSummary:
        """
        Get system-wide cost summary for a period.

        Uses pre-aggregated rollups from the SystemCostRollup table.

        Args:
            period: The period (YYYY-MM for monthly, YYYY-MM-DD for daily).
                   Defaults to current month/day based on period_type.
            period_type: Either "daily" or "monthly".

        Returns:
            SystemCostSummary with aggregated metrics.
        """
        if period_type == "daily":
            period = period or self._get_current_date()
        else:
            period = period or self._get_current_period()

        logger.info("Getting system summary for period")

        try:
            summary_data = await self.storage.get_system_summary(
                period=period,
                period_type=period_type
            )

            if not summary_data:
                # Return empty summary if no data exists
                logger.warning("No system summary found for period")
                return SystemCostSummary(
                    period=period,
                    period_type=period_type,
                    total_cost=0.0,
                    total_requests=0,
                    active_users=0,
                    total_input_tokens=0,
                    total_output_tokens=0,
                    total_cache_savings=0.0,
                    model_breakdown=None,
                    last_updated=datetime.now(timezone.utc).isoformat()
                )

            return SystemCostSummary(
                period=period,
                period_type=period_type,
                total_cost=summary_data.get("totalCost", 0.0),
                total_requests=summary_data.get("totalRequests", 0),
                active_users=summary_data.get("activeUsers", 0),
                total_input_tokens=summary_data.get("totalInputTokens", 0),
                total_output_tokens=summary_data.get("totalOutputTokens", 0),
                total_cache_savings=summary_data.get("totalCacheSavings", 0.0),
                model_breakdown=summary_data.get("modelBreakdown"),
                last_updated=summary_data.get("lastUpdated", "")
            )

        except Exception as e:
            logger.error(f"Error getting system summary: {e}")
            raise

    async def get_usage_by_model(
        self,
        period: Optional[str] = None
    ) -> List[ModelUsageSummary]:
        """
        Get cost breakdown by model for a period.

        Uses ROLLUP#MODEL items from the SystemCostRollup table.

        Args:
            period: The period (YYYY-MM format). Defaults to current month.

        Returns:
            List of ModelUsageSummary sorted by cost descending.
        """
        period = period or self._get_current_period()
        logger.info("Getting model usage for period")

        try:
            model_data = await self.storage.get_model_usage(period=period)

            result = []
            for model in model_data:
                total_requests = model.get("totalRequests", 0)
                total_cost = model.get("totalCost", 0.0)

                result.append(ModelUsageSummary(
                    model_id=model.get("modelId", ""),
                    model_name=model.get("modelName", ""),
                    provider=model.get("provider", "unknown"),
                    total_cost=total_cost,
                    total_requests=total_requests,
                    unique_users=model.get("uniqueUsers", 0),
                    avg_cost_per_request=(
                        total_cost / total_requests if total_requests > 0 else 0.0
                    ),
                    total_input_tokens=model.get("totalInputTokens", 0),
                    total_output_tokens=model.get("totalOutputTokens", 0)
                ))

            logger.info(f"Retrieved usage for {len(result)} models")
            return result

        except Exception as e:
            logger.error(f"Error getting model usage: {e}")
            raise

    async def get_usage_by_tier(
        self,
        period: Optional[str] = None
    ) -> List[TierUsageSummary]:
        """
        Get cost breakdown by quota tier for a period.

        Note: This is a placeholder for future implementation.
        Tier usage statistics require integration with the quota system.

        Args:
            period: The period (YYYY-MM format). Defaults to current month.

        Returns:
            List of TierUsageSummary (currently empty, placeholder).
        """
        _ = period or self._get_current_period()  # TODO: use once tier aggregation is implemented
        logger.info("Getting tier usage for period")

        # TODO: Implement tier usage aggregation
        # This requires:
        # 1. ROLLUP#TIER items in SystemCostRollup table
        # 2. Integration with QuotaRepository to get tier definitions
        # 3. Aggregating user costs by their assigned tiers

        return []

    async def get_daily_trends(
        self,
        start_date: str,
        end_date: str
    ) -> List[CostTrend]:
        """
        Get daily cost trends for a date range.

        Uses ROLLUP#DAILY items from the SystemCostRollup table.

        Args:
            start_date: Start date (YYYY-MM-DD format).
            end_date: End date (YYYY-MM-DD format).
                     Max range: 90 days.

        Returns:
            List of CostTrend sorted by date ascending.
        """
        logger.info("Getting daily trends for date range")

        # Validate date range (max 90 days)
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            if (end - start).days > 90:
                logger.warning("Date range exceeds 90 days, limiting to 90 days")
                end = start + timedelta(days=90)
                end_date = end.strftime("%Y-%m-%d")
        except ValueError as e:
            logger.error(f"Invalid date format: {e}")
            raise ValueError("Dates must be in YYYY-MM-DD format")

        try:
            trends_data = await self.storage.get_daily_trends(
                start_date=start_date,
                end_date=end_date
            )

            result = []
            for trend in trends_data:
                result.append(CostTrend(
                    date=trend.get("date", ""),
                    total_cost=trend.get("totalCost", 0.0),
                    total_requests=trend.get("totalRequests", 0),
                    active_users=trend.get("activeUsers", 0)
                ))

            logger.info(f"Retrieved {len(result)} daily trend data points")
            return result

        except Exception as e:
            logger.error(f"Error getting daily trends: {e}")
            raise

    async def get_top_sessions(
        self,
        period: Optional[str] = None,
        limit: int = 25,
        users_to_scan: int = 50,
        min_cost: Optional[float] = None,
    ) -> TopSessionsResponse:
        """
        Get the most expensive conversations for a period.

        Support's counterpart to the per-session notice the user now sees:
        spot a runaway conversation before the user calls (#833 PR-5).

        **How it is assembled, and why not a scan.** There is no index on
        session cost, and adding a GSI to sessions-metadata is a deploy
        hazard for one admin view. Instead this walks the period's top-cost
        users (``PeriodCostIndex``, already sorted) and queries each one's
        session rows — bounded, index-backed, and correct for the question
        being asked: a session can only be expensive if its owner is. The
        response says how many users were scanned and whether more had cost,
        so a truncated list never reads as "these are all of them".

        Args:
            period: Billing period (YYYY-MM). Defaults to current month.
            limit: Maximum sessions to return.
            users_to_scan: How many top-cost users to fan out over.
            min_cost: Optional floor on a session's lifetime cost.
        """
        period = period or self._get_current_period()
        period_start, _ = self._get_period_date_range(period)

        top_users = await self.storage.get_top_users_by_cost(
            period=period,
            limit=users_to_scan + 1,
        )
        truncated = len(top_users) > users_to_scan
        top_users = top_users[:users_to_scan]

        rows: List[TopSessionCost] = []
        for user_data in top_users:
            user_id = user_data.get("userId")
            if not user_id:
                continue
            user_period_cost = float(user_data.get("totalCost") or 0.0)

            try:
                sessions = await self.storage.get_user_session_costs(
                    user_id=user_id,
                    active_since=period_start,
                )
            except Exception as e:
                # One unreadable user must not empty the whole list.
                logger.warning(f"Skipping sessions for a user in top-sessions: {e}")
                continue

            for session in sessions:
                total_cost = session.get("totalCost")
                if total_cost is None:
                    # Legacy row whose aggregates have never been backfilled —
                    # it is not zero-cost, it is unknown, so say nothing.
                    continue
                total_cost = float(total_cost)
                if min_cost is not None and total_cost < min_cost:
                    continue

                partial_usd = session.get("partialMissUsd")
                rows.append(TopSessionCost(
                    session_id=session.get("sessionId", ""),
                    user_id=user_id,
                    title=session.get("title"),
                    total_cost=round(total_cost, 6),
                    last_message_at=session.get("lastMessageAt"),
                    created_at=session.get("createdAt"),
                    message_count=(
                        int(session["messageCount"])
                        if session.get("messageCount") is not None else None
                    ),
                    last_context_tokens=(
                        int(session["lastContextTokens"])
                        if session.get("lastContextTokens") is not None else None
                    ),
                    partial_miss_count=(
                        int(session["partialMissCount"])
                        if session.get("partialMissCount") is not None else None
                    ),
                    partial_miss_usd=(
                        round(float(partial_usd), 6) if partial_usd is not None else None
                    ),
                    user_period_cost=round(user_period_cost, 6),
                    share_of_user_period=(
                        round(total_cost / user_period_cost * 100, 2)
                        if user_period_cost > 0 else None
                    ),
                ))

        rows.sort(key=lambda r: r.total_cost, reverse=True)
        logger.info(
            f"Top sessions for period: {len(rows)} candidates across "
            f"{len(top_users)} users, returning {min(limit, len(rows))}"
        )

        return TopSessionsResponse(
            period=period,
            sessions=rows[:limit],
            users_scanned=len(top_users),
            truncated=truncated,
        )

    async def get_session_cost_anatomy(self, session_id: str) -> SessionCostAnatomy:
        """
        Get the per-model-call cost anatomy for one session.

        Reads every C# cost record for the session (chronological) and maps
        each to a SessionCallRow with token splits, cost, derived cacheStatus
        (including `partial_miss` — a call that read a leading segment and
        re-wrote the rest of the prefix, which costs like a miss and used to
        be reported as a hit),
        and the prompt-cache prefix fingerprints — the data needed to see
        where a session's spend went and which prefix component broke the
        cache on a miss. Rows written before this feature shipped simply lack
        cacheStatus/fingerprints and render as nulls.

        Args:
            session_id: Session identifier (any user's — admin scope).

        Returns:
            SessionCostAnatomy with per-call rows and session-level rollups.
        """
        records = await self.storage.get_session_cost_records(session_id)

        calls: List[SessionCallRow] = []
        total_cost = 0.0
        total_cache_read = 0
        total_cache_write = 0
        avoidable_misses = 0
        partial_misses = 0
        partial_miss_usd = 0.0
        wasted_usd = 0.0
        agent_switch_misses = 0
        agent_switch_usd = 0.0
        previous_removed: Optional[int] = None

        for record in records:
            token_usage = record.get("tokenUsage") or {}
            model_info = record.get("modelInfo") or {}
            fingerprints_raw = record.get("prefixFingerprints")
            ledger = _call_ledger(record, previous_removed)
            if ledger.removed is not None:
                previous_removed = ledger.removed

            # cost is a breakdown dict ({"total": ...}) on the streaming path
            # or a bare float on the legacy path.
            cost_raw = record.get("cost")
            if isinstance(cost_raw, dict):
                cost_raw = cost_raw.get("total")
            try:
                cost = float(cost_raw) if cost_raw is not None else 0.0
            except (TypeError, ValueError):
                cost = 0.0

            cache_read = int(token_usage.get("cacheReadInputTokens") or 0)
            cache_write = int(token_usage.get("cacheWriteInputTokens") or 0)
            cache_status = record.get("cacheStatus")
            row_wasted = float(record.get("wastedUsd") or 0.0)
            # #756 — derived at write time, where the predecessor row was already
            # in hand; read here as a plain projection.
            agent_switched = bool(record.get("agentSwitched"))

            total_cost += cost
            total_cache_read += cache_read
            total_cache_write += cache_write
            if cache_status == "miss_avoidable":
                avoidable_misses += 1
                # A split of the totals, never a deduction from them.
                if agent_switched:
                    agent_switch_misses += 1
                    agent_switch_usd += row_wasted
            elif cache_status == "partial_miss":
                partial_misses += 1
                partial_miss_usd += row_wasted
            wasted_usd += row_wasted

            gap_raw = record.get("cacheGapSeconds")
            prefix_gap_raw = record.get("cachePrefixGapSeconds")
            calls.append(SessionCallRow(
                timestamp=record.get("timestamp", ""),
                message_id=record.get("messageId"),
                model_id=model_info.get("modelId"),
                input_tokens=int(token_usage.get("inputTokens") or 0),
                output_tokens=int(token_usage.get("outputTokens") or 0),
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cost=cost,
                cache_status=cache_status,
                cache_gap_seconds=int(gap_raw) if gap_raw is not None else None,
                cache_prefix_gap_seconds=(
                    int(prefix_gap_raw) if prefix_gap_raw is not None else None
                ),
                wasted_usd=row_wasted,
                turn_agent_id=record.get("turnAgentId"),
                agent_switched=agent_switched,
                prefix_fingerprints=(
                    PrefixFingerprints(**fingerprints_raw)
                    if isinstance(fingerprints_raw, dict) else None
                ),
                prefix_tokens=ledger.prefix_tokens,
                window_removed_messages=ledger.removed,
                window_trimmed=ledger.trimmed,
                compaction_events=ledger.events or None,
                **_document_row_fields(ledger),
            ))

        cache_traffic = total_cache_read + total_cache_write
        cache_efficiency = (
            total_cache_read / cache_traffic if cache_traffic > 0 else None
        )

        logger.info(
            f"Session cost anatomy: {len(calls)} calls, "
            f"{avoidable_misses} avoidable misses, {partial_misses} partial misses, "
            f"wasted=${wasted_usd:.4f}"
        )

        return SessionCostAnatomy(
            session_id=session_id,
            calls=calls,
            total_cost=round(total_cost, 6),
            total_cache_read_tokens=total_cache_read,
            total_cache_write_tokens=total_cache_write,
            avoidable_miss_count=avoidable_misses,
            partial_miss_count=partial_misses,
            partial_miss_usd=round(partial_miss_usd, 6),
            wasted_usd=round(wasted_usd, 6),
            agent_switch_miss_count=agent_switch_misses,
            agent_switch_usd=round(agent_switch_usd, 6),
            cache_efficiency=cache_efficiency,
        )

    # =========================================================================
    # Content-free drill-down: user → conversations → conversation profile
    # =========================================================================

    async def _user_period_cost(self, user_id: str, period: Optional[str]) -> Optional[float]:
        """The user's recorded cost for ``period`` — the denominator for a
        conversation's share — or ``None`` when unscoped or unrecorded."""
        if not period:
            return None
        try:
            summary = await self.storage.get_user_cost_summary(user_id, period)
        except Exception as e:  # noqa: BLE001 - a missing denominator is not an error
            logger.debug("User period cost unavailable: %s", e)
            return None
        total = _as_float((summary or {}).get("totalCost")) if isinstance(summary, dict) else None
        return total if total and total > 0 else None

    @staticmethod
    def _row_facts(row: Dict[str, Any], threshold: int, share: Optional[float]) -> ProfileFacts:
        """Facts derivable from the session row alone — what the list view
        diagnoses on. The profile refines these with per-call data."""
        preferences = row.get("preferences") or {}
        compaction = row.get("compaction") or {}
        enabled = preferences.get("enabledTools")
        summary_chars = _as_int(compaction.get("summaryChars"))
        total_cost = _as_float(row.get("totalCost"))
        return ProfileFacts(
            cost_known=total_cost is not None,
            total_cost=total_cost or 0.0,
            call_count=0,
            peak_context_tokens=_as_int(row.get("lastContextTokens")),
            context_window=_as_int(row.get("contextWindow")),
            compaction_threshold=threshold,
            cache_read_tokens=_as_int(row.get("totalCacheReadTokens")) or 0,
            cache_write_tokens=_as_int(row.get("totalCacheWriteTokens")) or 0,
            wasted_usd=_as_float(row.get("wastedUsd")) or 0.0,
            partial_miss_usd=_as_float(row.get("partialMissUsd")) or 0.0,
            partial_miss_count=_as_int(row.get("partialMissCount")) or 0,
            summary_approx_tokens=(
                summary_chars // CHARS_PER_TOKEN if summary_chars is not None else None
            ),
            checkpoint=_as_int(compaction.get("checkpoint")),
            truncation_anchor=_as_int(compaction.get("truncationAnchor")),
            enabled_tools=list(enabled) if isinstance(enabled, list) else [],
            share_of_user_period=share,
            tool_call_count=_as_int(row.get("toolCallCount")),
            tool_error_count=_as_int(row.get("toolErrorCount")),
        )

    @staticmethod
    def _session_summary(
        row: Dict[str, Any],
        findings: List[Diagnosis],
        share: Optional[float],
    ) -> UserSessionSummary:
        """Map a content-free session row to the list/profile summary model."""
        preferences = row.get("preferences") or {}
        compaction = row.get("compaction") or {}
        total_cost = _as_float(row.get("totalCost"))
        read = _as_int(row.get("totalCacheReadTokens")) or 0
        write = _as_int(row.get("totalCacheWriteTokens")) or 0
        traffic = read + write
        last_context = _as_int(row.get("lastContextTokens"))
        window = _as_int(row.get("contextWindow"))
        summary_chars = _as_int(compaction.get("summaryChars"))
        enabled = preferences.get("enabledTools")
        wasted = _as_float(row.get("wastedUsd"))
        partial = _as_float(row.get("partialMissUsd"))

        return UserSessionSummary(
            session_id=row.get("sessionId", ""),
            created_at=row.get("createdAt"),
            last_message_at=row.get("lastMessageAt"),
            status=row.get("status"),
            message_count=_as_int(row.get("messageCount")),
            model_id=preferences.get("lastModel"),
            enabled_tool_count=len(enabled) if isinstance(enabled, list) else None,
            agent_bound=bool(preferences.get("assistantId")),
            last_context_tokens=last_context,
            context_window=window,
            context_share=(
                round(last_context / window, 4)
                if last_context is not None and window else None
            ),
            total_cost=round(total_cost, 6) if total_cost is not None else None,
            cost_known=total_cost is not None,
            share_of_user_period=share,
            cache_efficiency=round(read / traffic, 4) if traffic > 0 else None,
            wasted_usd=round(wasted, 6) if wasted is not None else None,
            partial_miss_usd=round(partial, 6) if partial is not None else None,
            summarized_turns=_as_int(compaction.get("totalSummarizedTurns")),
            summary_approx_tokens=(
                summary_chars // CHARS_PER_TOKEN if summary_chars is not None else None
            ),
            tool_call_count=_as_int(row.get("toolCallCount")),
            tool_error_count=_as_int(row.get("toolErrorCount")),
            compaction_count=_as_int(row.get("compactionCount")),
            compaction_applied_count=_as_int(row.get("compactionAppliedCount")),
            compaction_forced_count=_as_int(row.get("compactionForcedCount")),
            compaction_floor_unreachable_count=_as_int(
                row.get("compactionFloorUnreachableCount")
            ),
            diagnosis_count=len(findings),
            top_diagnosis_severity=top_severity(findings),
        )

    @staticmethod
    def _share(total_cost: Optional[float], user_period_cost: Optional[float]) -> Optional[float]:
        if total_cost is None or not user_period_cost:
            return None
        return round(total_cost / user_period_cost * 100, 2)

    async def get_user_sessions(
        self,
        user_id: str,
        period: Optional[str] = None,
        all_time: bool = False,
        sort: str = "cost",
        limit: int = 100,
    ) -> UserSessionsResponse:
        """One user's conversations, content-free, diagnosed on their own rows.

        Period semantics match :meth:`get_top_sessions`: ``period`` selects
        which sessions are listed (active in it) and supplies the share
        denominator; each row's ``totalCost`` is the conversation's lifetime
        cost. ``all_time`` lists everything and reports no share.

        Rows with no recorded cost are *listed*, flagged ``costKnown=False``,
        and trail under cost-sort — they are unrecorded, not free.
        """
        period = None if all_time else (period or self._get_current_period())
        active_since = self._get_period_date_range(period)[0] if period else None

        # Deleted conversations stay in the list: their cost rows and their
        # share of the period total outlive the delete, so hiding them left
        # a user's spend unaccounted for (one $3.77 row against $20.32).
        rows = await self.storage.get_user_session_diagnostics(
            user_id=user_id,
            active_since=active_since,
            include_deleted=True,
        )
        user_period_cost = await self._user_period_cost(user_id, period)
        threshold = compaction_token_threshold()

        summaries: List[UserSessionSummary] = []
        for row in rows:
            if row.get("deleted") or row.get("status") == "deleted":
                # Legacy tombstones carry `deleted` without the status flip;
                # normalise so the page has one signal to render.
                row["status"] = "deleted"
            share = self._share(_as_float(row.get("totalCost")), user_period_cost)
            findings = run_diagnoses(self._row_facts(row, threshold, share))
            summaries.append(self._session_summary(row, findings, share))

        def recent_key(s: UserSessionSummary) -> str:
            return s.last_message_at or ""

        if sort == "recent":
            summaries.sort(key=recent_key, reverse=True)
        elif sort == "context":
            summaries.sort(key=lambda s: (s.last_context_tokens or -1, recent_key(s)), reverse=True)
        elif sort == "messages":
            summaries.sort(key=lambda s: (s.message_count or -1, recent_key(s)), reverse=True)
        else:  # cost — known first by cost desc, unknown trailing by recency
            summaries.sort(
                key=lambda s: (s.cost_known, s.total_cost or 0.0, recent_key(s)),
                reverse=True,
            )

        unknown = sum(1 for s in summaries if not s.cost_known)
        deleted = [s for s in summaries if s.status == "deleted"]
        deleted_cost = sum(s.total_cost for s in deleted if s.total_cost is not None)
        logger.info(
            f"User sessions: {len(summaries)} rows ({unknown} unknown-cost, "
            f"{len(deleted)} deleted), returning {min(limit, len(summaries))}"
        )
        return UserSessionsResponse(
            user_id=user_id,
            period=period,
            user_period_cost=round(user_period_cost, 6) if user_period_cost else None,
            sessions=summaries[:limit],
            total=len(summaries),
            unknown_cost_count=unknown,
            deleted_session_count=len(deleted),
            deleted_session_cost=round(deleted_cost, 6),
        )

    async def _attachment_profile(self, session_id: str) -> AttachmentProfile:
        """Per-session upload stats. Best-effort: a fork without the uploads
        table, or a transient error, yields an empty profile, never a 500."""
        try:
            stats = await self._files().list_session_file_stats(session_id)
        except Exception as e:  # noqa: BLE001 - attachments are one signal of several
            logger.debug("Attachment stats unavailable for session: %s", e)
            return AttachmentProfile()
        by_mime: Counter = Counter()
        total_bytes = 0
        digested = digest_tokens = 0
        for item in stats:
            by_mime[item.get("mimeType") or "unknown"] += 1
            total_bytes += _as_int(item.get("sizeBytes")) or 0
            digest = item.get("digest")
            if isinstance(digest, dict) and digest.get("status") == "ready":
                digested += 1
                digest_tokens += _as_int(digest.get("tokens")) or 0
        return AttachmentProfile(
            count=len(stats),
            total_bytes=total_bytes,
            by_mime=dict(by_mime),
            digested=digested,
            digest_tokens=digest_tokens,
        )

    async def _feedback_rows(self, session_id: str) -> List[Dict[str, Any]]:
        """The session's ``F#`` rows. Best-effort: a storage fork without the
        reader, or a transient error, yields none — the profile then falls
        back to the session rollups and reports coverage honestly."""
        reader = getattr(self.storage, "get_session_feedback_rows", None)
        if reader is None:
            return []
        try:
            rows = await reader(session_id)
        except Exception as e:  # noqa: BLE001 - feedback is one signal of several
            logger.debug("Feedback rows unavailable for session: %s", e)
            return []
        return list(rows or [])

    async def get_session_profile(self, session_id: str) -> Optional[SessionProfile]:
        """The content-free diagnostic profile of one conversation, or ``None``
        when the session has no metadata row.

        Three reads — the session row (by session id, via the lookup index),
        the per-call cost rows the anatomy already uses, and the session's
        upload stats — then pure arithmetic: context trajectory, model mix,
        fingerprint churn, tool census, and the diagnosis rules over all of it.
        """
        row = await self.storage.get_session_diagnostic_row(session_id)
        if row is None:
            return None
        user_id = row.get("userId")

        records = await self.storage.get_session_cost_records(session_id)
        attachments = await self._attachment_profile(session_id)
        feedback_rows = await self._feedback_rows(session_id)
        user_period_cost = (
            await self._user_period_cost(user_id, self._get_current_period())
            if user_id else None
        )

        # ── per-call derivations ──
        trajectory: List[ContextTrajectoryPoint] = []
        model_mix: Counter = Counter()
        census: Dict[str, ToolCensusEntry] = {}
        system_changes = tool_changes = explained = 0
        system_hashes: set = set()
        tool_hashes: set = set()
        agent_switches = 0
        read_total = write_total = 0
        any_fingerprints = any_census = False
        previous_fp: Optional[Dict[str, Any]] = None
        any_prefix_tokens = any_window = any_compaction_events = False
        prefix_tokens: Optional[PrefixTokens] = None
        previous_removed: Optional[int] = None
        last_removed: Optional[int] = None
        window_trim_calls = 0
        compaction_event_counts: Counter = Counter()
        last_summary_tokens: Optional[int] = None
        any_documents = False
        full_document_calls = digest_only_calls = 0
        document_read_calls = document_read_pages = 0
        peak_document_tokens: Optional[int] = None

        for index, record in enumerate(records):
            usage = record.get("tokenUsage") or {}
            read_total += int(usage.get("cacheReadInputTokens") or 0)
            write_total += int(usage.get("cacheWriteInputTokens") or 0)
            model_id = (record.get("modelInfo") or {}).get("modelId")
            if model_id:
                model_mix[model_id] += 1
            switched = bool(record.get("agentSwitched"))
            if switched:
                agent_switches += 1

            fp = record.get("prefixFingerprints")
            if isinstance(fp, dict):
                any_fingerprints = True
                if not switched:
                    if fp.get("systemPromptHash"):
                        system_hashes.add(fp["systemPromptHash"])
                    if fp.get("toolConfigHash"):
                        tool_hashes.add(fp["toolConfigHash"])
                if isinstance(previous_fp, dict):
                    changed = False
                    if fp.get("systemPromptHash") != previous_fp.get("systemPromptHash"):
                        system_changes += 1
                        changed = True
                    if fp.get("toolConfigHash") != previous_fp.get("toolConfigHash"):
                        tool_changes += 1
                        changed = True
                    if changed and switched:
                        explained += 1
                previous_fp = fp

            tool_calls_raw = record.get("toolCalls")
            point_tool_calls: Optional[Dict[str, int]] = None
            if isinstance(tool_calls_raw, dict):
                any_census = True
                point_tool_calls = {}
                for name, entry in tool_calls_raw.items():
                    calls = _as_int(entry.get("calls") if isinstance(entry, dict) else entry) or 0
                    errors = _as_int(entry.get("errors")) or 0 if isinstance(entry, dict) else 0
                    point_tool_calls[name] = calls
                    slot = census.setdefault(name, ToolCensusEntry())
                    slot.calls += calls
                    slot.errors += errors

            ledger = _call_ledger(record, previous_removed)
            if ledger.prefix_tokens is not None:
                any_prefix_tokens = True
                prefix_tokens = ledger.prefix_tokens
            if ledger.removed is not None:
                any_window = True
                previous_removed = last_removed = ledger.removed
            if ledger.trimmed:
                window_trim_calls += 1
            if ledger.events:
                any_compaction_events = True
                for event in ledger.events:
                    compaction_event_counts[event.kind] += 1
                    if event.summary_tokens is not None:
                        last_summary_tokens = event.summary_tokens
            if ledger.documents is not None:
                any_documents = True
                if ledger.documents.get("hasDocuments"):
                    full_document_calls += 1
                elif (ledger.documents.get("documentDigests") or 0) > 0:
                    digest_only_calls += 1
                doc_tokens = ledger.documents.get("documentTokens")
                if doc_tokens is not None:
                    peak_document_tokens = max(peak_document_tokens or 0, doc_tokens)
            if ledger.document_reads is not None:
                any_documents = True
                document_read_calls += ledger.document_reads.calls
                document_read_pages += ledger.document_reads.pages
            trajectory.append(ContextTrajectoryPoint(
                call_index=index,
                timestamp=record.get("timestamp", ""),
                context_tokens=_context_tokens(record),
                cache_status=record.get("cacheStatus"),
                model_id=model_id,
                cost=_record_cost(record),
                tool_calls=point_tool_calls,
                window_trimmed=ledger.trimmed,
                compaction=[e.kind for e in ledger.events] or None,
            ))

        # Cache totals: the rows are authoritative when present, else the
        # session rollups (which cover calls written before fingerprints).
        if not records:
            read_total = _as_int(row.get("totalCacheReadTokens")) or 0
            write_total = _as_int(row.get("totalCacheWriteTokens")) or 0

        peak = max((p.context_tokens for p in trajectory), default=None)
        if peak is None:
            peak = _as_int(row.get("lastContextTokens"))

        total_cost = _as_float(row.get("totalCost"))
        share = self._share(total_cost, user_period_cost)
        threshold = compaction_token_threshold()

        facts = self._row_facts(row, threshold, share)
        facts.call_count = len(records)
        facts.peak_context_tokens = peak
        facts.cache_read_tokens = read_total
        facts.cache_write_tokens = write_total
        facts.distinct_system_prompt_hashes = len(system_hashes)
        facts.distinct_tool_config_hashes = len(tool_hashes)
        facts.agent_switch_count = agent_switches
        facts.attachment_count = attachments.count
        facts.attachment_bytes = attachments.total_bytes
        if any_census:
            facts.tool_call_count = sum(e.calls for e in census.values())
            facts.tool_error_count = sum(e.errors for e in census.values())

        # Outcome signal: thumbs joined to the calls they rate. The rows are
        # authoritative when present; the session rollups cover thumbs whose
        # rows expired (they share the C# TTL, so this is rare).
        feedback = _join_feedback(records, feedback_rows)
        if not feedback_rows:
            feedback.up = _as_int(row.get("thumbsUp")) or 0
            feedback.down = _as_int(row.get("thumbsDown")) or 0
        feedback_tracked = bool(feedback_rows) or row.get("thumbsUp") is not None

        findings = run_diagnoses(facts)
        summary = self._session_summary(row, findings, share)
        if any_census and summary.tool_call_count is None:
            summary.tool_call_count = facts.tool_call_count
            summary.tool_error_count = facts.tool_error_count

        return SessionProfile(
            session_id=session_id,
            user_id=user_id,
            session=summary,
            call_count=len(records),
            peak_context_tokens=peak,
            compaction_threshold=threshold,
            write_read_ratio=(
                round(write_total / read_total, 3) if read_total > 0 else None
            ),
            attachments=attachments,
            context_trajectory=trajectory,
            model_mix=dict(model_mix),
            fingerprint_changes=FingerprintChanges(
                system_prompt=system_changes,
                tool_config=tool_changes,
                explained_by_agent_switch=explained,
            ),
            tool_census=census,
            enabled_tool_ids=sorted(facts.enabled_tools),
            diagnoses=[SessionDiagnosis(**d.to_dict()) for d in findings],
            data_coverage=DataCoverage(
                tool_census=any_census or row.get("toolCallCount") is not None,
                compaction_count=row.get("compactionCount") is not None,
                fingerprints=any_fingerprints,
                cost=total_cost is not None,
                prefix_tokens=any_prefix_tokens,
                window_trim=any_window,
                compaction_events=(
                    any_compaction_events or row.get("compactionAppliedCount") is not None
                ),
                feedback=feedback_tracked,
                documents=any_documents or row.get("fullDocumentCalls") is not None,
            ),
            feedback=feedback,
            prefix_tokens=prefix_tokens,
            window_trim_calls=window_trim_calls,
            window_removed_messages=last_removed,
            compaction_event_counts=dict(compaction_event_counts),
            last_summary_tokens=last_summary_tokens,
            # Rows are authoritative when present; the session rollups cover
            # calls whose rows have expired (365-day TTL) or a session read
            # without its rows.
            full_document_calls=(
                full_document_calls if any_documents else (_as_int(row.get("fullDocumentCalls")) or 0)
            ),
            digest_only_calls=(
                digest_only_calls if any_documents else (_as_int(row.get("digestOnlyCalls")) or 0)
            ),
            peak_document_tokens=peak_document_tokens,
            document_read_calls=(
                document_read_calls if any_documents else (_as_int(row.get("documentReadCalls")) or 0)
            ),
            document_read_pages=(
                document_read_pages if any_documents else (_as_int(row.get("documentReadPages")) or 0)
            ),
        )

    async def get_dashboard(
        self,
        period: Optional[str] = None,
        top_users_limit: int = 100,
        include_trends: bool = True
    ) -> AdminCostDashboard:
        """
        Get complete admin cost dashboard with all metrics.

        This is the main entry point for the dashboard, combining:
        - System-wide cost summary
        - Top N users by cost
        - Model usage breakdown
        - Daily trends (optional)

        Args:
            period: The billing period (YYYY-MM format). Defaults to current month.
            top_users_limit: Number of top users to include (1-1000, default 100).
            include_trends: Whether to include daily trends for the period.

        Returns:
            AdminCostDashboard with all dashboard components.
        """
        period = period or self._get_current_period()
        logger.info(
            "Building admin cost dashboard for period"
        )

        # Get system summary
        current_period = await self.get_system_summary(
            period=period,
            period_type="monthly"
        )

        # Get top users
        top_users = await self.get_top_users(
            period=period,
            limit=top_users_limit
        )

        # Get model usage
        model_usage = await self.get_usage_by_model(period=period)

        # Get daily trends if requested
        daily_trends = None
        if include_trends:
            start_date, end_date = self._get_period_date_range(period)
            # Limit end_date to today if period is current month
            today = self._get_current_date()
            if end_date > today:
                end_date = today
            daily_trends = await self.get_daily_trends(start_date, end_date)

        # TODO: Get tier usage when implemented
        tier_usage = None

        return AdminCostDashboard(
            current_period=current_period,
            top_users=top_users,
            model_usage=model_usage,
            tier_usage=tier_usage,
            daily_trends=daily_trends
        )
