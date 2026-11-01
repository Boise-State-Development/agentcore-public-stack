"""Quota requests and extensions: validation, decisions, notifications, audit.

The one place the request lifecycle (spec §7.6) and the extension guards
(§8.6) live, so the user routes and the admin routes cannot disagree about
them. Nothing here is on the turn path: a decision takes effect when the
resolver next reads extensions, and on a blocked turn that is the next send
(fresh-on-block, §8.3).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from agents.main_agent.quota.extensions import (
    active_extensions,
    compute_funding_key,
    effective_limit,
    end_of_day_iso,
    is_unlimited,
    period_base_limit,
)
from agents.main_agent.quota.models import FundingSource, QuotaExtension, ResolvedQuota
from agents.main_agent.quota.repository import QuotaRepository
from apis.shared.audit.models import (
    TARGET_QUOTA_EXTENSION,
    TARGET_QUOTA_REQUEST,
    AuditAction,
    AuditOutcome,
)
from apis.shared.audit.service import get_audit_service
from apis.shared.auth.models import User
from apis.shared.notifications import NotificationService
from apis.shared.timestamps import from_iso, to_iso, utc_now_iso

from .models import (
    ADMIN_AMOUNT_CEILING,
    OPEN_STATUSES,
    ApproveRequestBody,
    ExtensionCreate,
    QuotaRequest,
    QuotaRequestContext,
    QuotaRequestCreate,
    QuotaRequestDecision,
    QuotaRequestPatch,
    QuotaRequestPolicy,
)
from .repository import QuotaRequestRepository, get_quota_request_repository

logger = logging.getLogger(__name__)


class QuotaRequestError(Exception):
    """Base: carries the HTTP status the routes map it to."""

    status_code = 400

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.message = message
        self.extra = extra


class InvalidQuotaRequest(QuotaRequestError):
    status_code = 400


class QuotaRequestNotFound(QuotaRequestError):
    status_code = 404


class OpenRequestExists(QuotaRequestError):
    status_code = 409


class QuotaRequestStateError(QuotaRequestError):
    status_code = 409


class SelfDecisionForbidden(QuotaRequestError):
    status_code = 403


# ── pure helpers ────────────────────────────────────────────────────────────


def validate_funding_source(
    source: Optional[FundingSource], policy: QuotaRequestPolicy
) -> Optional[FundingSource]:
    """Check a funding source against the deployment's schema (§8.6).

    Unknown keys are refused so the report's CSV columns stay stable;
    required fields must be present and non-blank; a field's ``pattern`` must
    match the whole value. Values come back trimmed, blanks dropped.
    """
    if source is None:
        return None
    schema = {f.key: f for f in policy.funding_fields}
    fields = {k.strip(): str(v).strip() for k, v in (source.fields or {}).items()}
    unknown = sorted(k for k in fields if k not in schema)
    if unknown:
        raise InvalidQuotaRequest(f"Unknown funding field(s): {', '.join(unknown)}")
    for key, spec in schema.items():
        value = fields.get(key, "")
        if not value:
            if spec.required:
                raise InvalidQuotaRequest(f"{spec.label} is required")
            continue
        if spec.pattern:
            try:
                matched = re.fullmatch(spec.pattern, value) is not None
            except re.error:
                matched = True  # an admin's bad regex must not lock users out
            if not matched:
                raise InvalidQuotaRequest(f"{spec.label} is not in the expected format")
    cleaned = {k: v for k, v in fields.items() if v}
    label = (source.label or "").strip() or None
    authorizer = (source.authorizer_email or "").strip().lower() or None
    if authorizer and "@" not in authorizer:
        raise InvalidQuotaRequest("The authorizer must be an email address")
    if not cleaned and not label:
        raise InvalidQuotaRequest("A funding source needs at least a label or one field")
    return FundingSource(label=label, fields=cleaned, authorizer_email=authorizer)


def _current_period(period_type: str, now: datetime) -> str:
    return now.strftime("%Y-%m-%d") if period_type == "daily" else now.strftime("%Y-%m")


def _money(value: Optional[float]) -> Optional[str]:
    return None if value is None else f"{float(value):.2f}"


class QuotaRequestService:
    def __init__(
        self,
        *,
        requests: Optional[QuotaRequestRepository] = None,
        quotas: Optional[QuotaRepository] = None,
        notifications: Optional[NotificationService] = None,
    ):
        self._requests = requests
        self._quotas = quotas
        self._notifications = notifications

    # Lazily built so importing this module never needs AWS configured.
    @property
    def requests(self) -> QuotaRequestRepository:
        if self._requests is None:
            self._requests = get_quota_request_repository()
        return self._requests

    @property
    def quotas(self) -> QuotaRepository:
        if self._quotas is None:
            from apis.shared.quota import get_quota_repository

            self._quotas = get_quota_repository()
        return self._quotas

    @property
    def notifications(self) -> NotificationService:
        if self._notifications is None:
            self._notifications = NotificationService()
        return self._notifications

    # ── quota reads ─────────────────────────────────────────────────────

    async def _resolve(self, user: User, *, fresh: bool = False) -> Optional[ResolvedQuota]:
        from apis.shared.quota import get_quota_resolver

        return await get_quota_resolver().resolve_user_quota(user, bypass_cache=fresh)

    async def _period_usage(self, user_id: str, period: str) -> float:
        from apis.shared.quota import get_cost_aggregator

        try:
            summary = await get_cost_aggregator().get_user_cost_summary(user_id=user_id, period=period)
            return float(summary.total_cost)
        except Exception:  # noqa: BLE001 - a context read must not fail the action
            logger.warning("Cost summary read failed for quota request context", exc_info=True)
            return 0.0

    async def snapshot(self, user: User, *, fresh: bool = False) -> QuotaRequestContext:
        """Numbers-only picture of the user's quota now (§7.1 ``context``)."""
        now = datetime.now(timezone.utc)
        resolved = await self._resolve(user, fresh=fresh)
        if not resolved:
            return QuotaRequestContext(period=_current_period("monthly", now))
        tier = resolved.tier
        period = _current_period(tier.period_type, now)
        usage = await self._period_usage(user.user_id, period)
        if is_unlimited(tier):
            return QuotaRequestContext(
                tier_id=tier.tier_id, tier_name=tier.tier_name, period_type=tier.period_type,
                period=period, current_usage=usage,
            )
        base = period_base_limit(tier)
        limit = effective_limit(base, active_extensions(resolved.extensions, utc_now_iso()))
        pct = (usage / limit * 100) if limit else 0.0
        return QuotaRequestContext(
            tier_id=tier.tier_id,
            tier_name=tier.tier_name,
            period_type=tier.period_type,
            period=period,
            base_limit=base,
            effective_limit=limit,
            current_usage=round(usage, 6),
            percentage_used=round(pct, 1),
            blocked=bool(limit is not None and usage >= limit and tier.action_on_limit == "block"),
        )

    async def _user_for(self, user_id: str, fallback_email: str = "") -> User:
        """A ``User`` good enough to resolve someone else's tier (email + roles)."""
        from apis.shared.users.repository import UserRepository

        profile = None
        try:
            profile = await UserRepository().get_user_by_user_id(user_id)
        except Exception:  # noqa: BLE001
            logger.warning("User directory read failed for quota extension", exc_info=True)
        if profile is not None:
            return User(email=profile.email, user_id=user_id, name=profile.name, roles=list(profile.roles))
        return User(email=fallback_email, user_id=user_id, name="", roles=[])

    def _invalidate(self, user_id: str) -> None:
        """Drop this process's cached resolution for the user (other tasks expire in 5 min)."""
        from apis.shared.quota import get_quota_resolver

        get_quota_resolver().invalidate_cache(user_id)

    # ── user side ───────────────────────────────────────────────────────

    async def get_policy(self) -> QuotaRequestPolicy:
        return await self.requests.get_policy()

    async def list_my_requests(self, user: User) -> List[QuotaRequest]:
        """The caller's requests, each approval marked if its extension was since revoked.

        The extension read happens only for a user with an approval to mark.
        """
        requests = await self.requests.list_user_requests(user.user_id)
        if not any(r.decision and r.decision.extension_id for r in requests):
            return requests
        revoked = {
            e.extension_id: e.revoked_at
            for e in await self.quotas.list_user_extensions(user.user_id)
            if e.revoked_at
        }
        for request in requests:
            if request.decision and request.decision.extension_id in revoked:
                request.decision.extension_revoked_at = revoked[request.decision.extension_id]
        return requests

    async def open_request_for(self, user_id: str) -> Optional[QuotaRequest]:
        for request in await self.requests.list_user_requests(user_id):
            if request.is_open:
                return request
        return None

    def _validate_funding_for_user(
        self, source: Optional[FundingSource], policy: QuotaRequestPolicy, *, standing: bool
    ) -> Optional[FundingSource]:
        if policy.funding_source_mode == "hidden":
            if source is not None:
                raise InvalidQuotaRequest("This deployment does not collect funding sources on requests")
            if standing:
                raise InvalidQuotaRequest("Ongoing requests are not available on this deployment")
            return None
        if standing and not (policy.standing_enabled and policy.funding_fields):
            raise InvalidQuotaRequest("Ongoing requests are not available on this deployment")
        cleaned = validate_funding_source(source, policy)
        if cleaned is None and (standing or policy.funding_source_mode == "required"):
            raise InvalidQuotaRequest("A funding source is required")
        return cleaned

    async def submit(self, user: User, body: QuotaRequestCreate) -> QuotaRequest:
        policy = await self.get_policy()
        open_request = await self.open_request_for(user.user_id)
        if open_request is not None:
            raise OpenRequestExists(
                "You already have an open quota request", openRequestId=open_request.request_id
            )

        amount = body.requested_amount
        if policy.max_request_amount > 0 and amount > policy.max_request_amount:
            raise InvalidQuotaRequest(f"Requests are limited to ${policy.max_request_amount} per period")
        if not policy.allow_custom_amount and amount not in policy.amount_presets:
            raise InvalidQuotaRequest("Choose one of the offered amounts")

        today = datetime.now(timezone.utc).date()
        if body.requested_until < today:
            raise InvalidQuotaRequest("The end date is in the past")
        standing = body.kind == "standing"
        # A standing request runs to the fiscal year end, past maxDurationDays.
        if not standing and body.requested_until > today + timedelta(days=policy.max_duration_days):
            raise InvalidQuotaRequest(f"Requests can cover at most {policy.max_duration_days} days")

        justification = body.justification.strip()
        if len(justification) < policy.justification_min_chars:
            raise InvalidQuotaRequest(
                f"Please give at least {policy.justification_min_chars} characters of justification"
            )

        funding = self._validate_funding_for_user(body.funding_source, policy, standing=standing)
        now = utc_now_iso()
        request = QuotaRequest(
            request_id=uuid.uuid4().hex,
            user_id=user.user_id,
            user_email=(user.email or "").lower(),
            status="pending",
            kind=body.kind,
            requested_amount=amount,
            requested_until=body.requested_until.isoformat(),
            justification=justification,
            funding_source=funding,
            funding_key=compute_funding_key(funding.fields, policy.schema_keys) if funding else None,
            context=await self.snapshot(user),
            created_at=now,
            updated_at=now,
        )
        await self.requests.create_request(request)
        logger.info("Quota request submitted", extra={"event": "quota_request_submitted", "request_id": request.request_id})

        if policy.notify_emails:
            self.notifications.notify_many(
                policy.notify_emails,
                kind="quota_request_submitted",
                actor=user,
                payload={
                    "requestId": request.request_id,
                    "userEmail": request.user_email,
                    "amount": _money(amount),
                    "through": request.requested_until,
                },
            )
        return request

    async def _own_request(self, user: User, request_id: str) -> QuotaRequest:
        request = await self.requests.get_request(request_id)
        # Another user's request is indistinguishable from a missing one.
        if request is None or request.user_id != user.user_id:
            raise QuotaRequestNotFound("Quota request not found")
        return request

    async def patch(self, user: User, request_id: str, body: QuotaRequestPatch) -> QuotaRequest:
        request = await self._own_request(user, request_id)
        expected = request.status
        if body.withdraw:
            if not request.is_open:
                raise QuotaRequestStateError("Only an open request can be withdrawn")
            request.status = "withdrawn"
        elif body.funding_source is not None:
            if request.status != "info_requested":
                raise QuotaRequestStateError("A funding source can be added only when your administrator asks for one")
            policy = await self.get_policy()
            funding = validate_funding_source(body.funding_source, policy)
            request.funding_source = funding
            request.funding_key = compute_funding_key(funding.fields, policy.schema_keys) if funding else None
            request.status = "pending"
        else:
            raise InvalidQuotaRequest("Nothing to change")
        request.updated_at = utc_now_iso()
        return await self._save(request, expected)

    async def _save(self, request: QuotaRequest, expected_status: str) -> QuotaRequest:
        from .repository import RequestStateConflict

        try:
            return await self.requests.save_request(request, expected_status=expected_status)
        except RequestStateConflict as e:
            raise QuotaRequestStateError("This request changed while you were looking at it; reload it") from e

    # ── admin side ──────────────────────────────────────────────────────

    def _refuse_self(self, admin: User, subject_user_id: str, target_type: str, target_id: str, action: str) -> None:
        """No self-approval (§8.6): audit the attempt and refuse with 403."""
        if subject_user_id != admin.user_id:
            return
        get_audit_service().record(
            action=AuditAction.QUOTA_REQUEST_MUTATION_DENIED,
            actor=admin,
            target_type=target_type,
            target_id=target_id,
            outcome=AuditOutcome.DENIED,
            reason=f"self-{action}",
        )
        raise SelfDecisionForbidden("You cannot decide your own quota request or extend your own quota")

    async def get_request(self, request_id: str) -> QuotaRequest:
        request = await self.requests.get_request(request_id)
        if request is None:
            raise QuotaRequestNotFound("Quota request not found")
        return request

    async def list_queue(
        self, status: str, *, limit: int, cursor: Optional[str]
    ) -> Tuple[List[QuotaRequest], Optional[str]]:
        return await self.requests.list_by_status(status, limit=limit, cursor=cursor)

    async def summary(self) -> Dict[str, int]:
        return {
            "pending": await self.requests.count_by_status("pending"),
            "infoRequested": await self.requests.count_by_status("info_requested"),
        }

    async def request_detail(self, request: QuotaRequest) -> Dict[str, Any]:
        """The request plus live context: tier, usage now and the last three periods, history."""
        subject = await self._user_for(request.user_id, request.user_email)
        live = await self.snapshot(subject)
        resolved = await self._resolve(subject)
        now = datetime.now(timezone.utc)
        history_periods = []
        year, month = now.year, now.month
        for _ in range(3):
            month -= 1
            if month == 0:
                year, month = year - 1, 12
            period = f"{year:04d}-{month:02d}"
            history_periods.append({"period": period, "spend": await self._period_usage(request.user_id, period)})
        prior = [r for r in await self.requests.list_user_requests(request.user_id) if r.request_id != request.request_id]
        extensions = await self.quotas.list_user_extensions(request.user_id)
        return {
            "request": request.model_dump(by_alias=True, mode="json"),
            "user": {
                "userId": subject.user_id,
                "email": subject.email,
                "name": subject.name,
                "roles": subject.roles,
                "matchedBy": resolved.matched_by if resolved else None,
            },
            "live": live.model_dump(by_alias=True, mode="json"),
            "previousPeriods": history_periods,
            "priorRequests": [r.model_dump(by_alias=True, mode="json") for r in prior],
            "extensions": [e.model_dump(by_alias=True, mode="json") for e in extensions],
        }

    def _validate_admin_grant(
        self,
        *,
        amount: Optional[Decimal],
        standing: bool,
        funding_mode: str,
        funding_source: Optional[FundingSource],
        valid_until: date,
        policy: QuotaRequestPolicy,
    ) -> Optional[FundingSource]:
        if amount is None and not standing:
            raise InvalidQuotaRequest("An amount is required unless this is an uncapped standing extension")
        if amount is not None and amount > ADMIN_AMOUNT_CEILING:
            raise InvalidQuotaRequest(f"Amounts are limited to ${ADMIN_AMOUNT_CEILING} per period")
        if valid_until < datetime.now(timezone.utc).date():
            raise InvalidQuotaRequest("The end date is in the past")
        if standing:
            if not policy.standing_enabled:
                raise InvalidQuotaRequest("Standing extensions are turned off in the quota request settings")
            if not policy.funding_fields:
                raise InvalidQuotaRequest("Standing extensions need funding fields defined in the quota request settings")
            if funding_mode != "chargeback":
                raise InvalidQuotaRequest("A standing extension is always charged to a funding source")
        cleaned = validate_funding_source(funding_source, policy)
        if funding_mode == "chargeback" and cleaned is None:
            raise InvalidQuotaRequest("Charging to a funding source needs the funding source")
        # A central extension carries no fund; anything sent with it is dropped.
        return cleaned if funding_mode == "chargeback" else None

    async def _create_extension(
        self,
        admin: User,
        *,
        subject: User,
        amount: Optional[Decimal],
        standing: bool,
        funding_mode: str,
        funding: Optional[FundingSource],
        valid_from: Optional[date],
        valid_until: date,
        reason: str,
        request_id: Optional[str],
        policy: QuotaRequestPolicy,
    ) -> QuotaExtension:
        now = utc_now_iso()
        start = now
        if valid_from is not None and valid_from > datetime.now(timezone.utc).date():
            start = to_iso(datetime(valid_from.year, valid_from.month, valid_from.day, tzinfo=timezone.utc))
        resolved = await self._resolve(subject)
        base = None
        if resolved and not is_unlimited(resolved.tier):
            base = period_base_limit(resolved.tier)
        extension = QuotaExtension(
            extension_id=uuid.uuid4().hex,
            user_id=subject.user_id,
            user_email=(subject.email or "").lower(),
            standing=standing,
            amount=amount,
            valid_from=start,
            valid_until=end_of_day_iso(valid_until),
            funding_mode=funding_mode,
            funding_source=funding,
            funding_key=compute_funding_key(funding.fields, policy.schema_keys) if funding else None,
            request_id=request_id,
            reason=reason,
            base_limit_snapshot=base,
            created_by=admin.user_id,
            created_by_email=(admin.email or "").lower() or None,
            created_at=now,
        )
        await self.quotas.create_extension(extension)
        self._invalidate(subject.user_id)
        get_audit_service().record(
            action=AuditAction.QUOTA_EXTENSION_CREATED,
            actor=admin,
            target_type=TARGET_QUOTA_EXTENSION,
            target_id=extension.extension_id,
            changes=["amount", "fundingMode", "standing", "validUntil"],
            after={
                "userId": extension.user_id,
                "amount": _money(extension.amount),
                "standing": extension.standing,
                "fundingMode": extension.funding_mode,
                "fundingKey": extension.funding_key,
                "validFrom": extension.valid_from,
                "validUntil": extension.valid_until,
                "requestId": request_id,
            },
        )
        logger.info("Quota extension created", extra={"event": "quota_extension_created", "extension_id": extension.extension_id})
        return extension

    async def approve(self, admin: User, request_id: str, body: ApproveRequestBody) -> Tuple[QuotaRequest, QuotaExtension]:
        request = await self.get_request(request_id)
        self._refuse_self(admin, request.user_id, TARGET_QUOTA_REQUEST, request_id, "approve")
        if not request.is_open:
            raise QuotaRequestStateError(f"This request is already {request.status}")
        policy = await self.get_policy()
        standing = body.standing if body.standing is not None else request.kind == "standing"
        funding = self._validate_admin_grant(
            amount=body.amount, standing=standing, funding_mode=body.funding_mode,
            funding_source=body.funding_source, valid_until=body.valid_until, policy=policy,
        )
        subject = await self._user_for(request.user_id, request.user_email)
        reason = (body.note or "").strip() or f"Approved quota request {request_id}"
        extension = await self._create_extension(
            admin, subject=subject, amount=body.amount, standing=standing,
            funding_mode=body.funding_mode, funding=funding, valid_from=None,
            valid_until=body.valid_until, reason=reason, request_id=request_id, policy=policy,
        )

        expected = request.status
        request.status = "approved"
        request.updated_at = utc_now_iso()
        request.decision = QuotaRequestDecision(
            decided_by=admin.user_id,
            decided_by_email=(admin.email or "").lower() or None,
            decided_at=request.updated_at,
            outcome="approved",
            note=(body.note or "").strip() or None,
            amount=body.amount,
            valid_until=extension.valid_until,
            funding_mode=body.funding_mode,
            standing=standing,
            extension_id=extension.extension_id,
        )
        try:
            await self._save(request, expected)
        except QuotaRequestStateError:
            # Another admin decided first; undo the grant this approval made.
            await self.quotas.revoke_extension(
                extension.extension_id, revoked_by=admin.user_id, reason="Superseded by a concurrent decision"
            )
            self._invalidate(request.user_id)
            raise

        get_audit_service().record(
            action=AuditAction.QUOTA_REQUEST_APPROVED,
            actor=admin,
            target_type=TARGET_QUOTA_REQUEST,
            target_id=request_id,
            changes=["status"],
            before={"status": expected},
            after={"status": "approved", "extensionId": extension.extension_id, "amount": _money(body.amount)},
        )
        self.notifications.notify(
            recipient_email=request.user_email,
            kind="quota_request_decided",
            actor=admin,
            payload={
                "requestId": request_id,
                "outcome": "approved",
                "requestedAmount": _money(request.requested_amount),
                "amount": _money(body.amount),
                "through": body.valid_until.isoformat(),
                "standing": standing,
                "note": request.decision.note,
            },
        )
        return request, extension

    async def request_info(self, admin: User, request_id: str, note: str) -> QuotaRequest:
        request = await self.get_request(request_id)
        self._refuse_self(admin, request.user_id, TARGET_QUOTA_REQUEST, request_id, "request_info")
        if request.status != "pending":
            raise QuotaRequestStateError(f"Only a pending request can be sent back; this one is {request.status}")
        request.status = "info_requested"
        request.admin_note = note.strip()
        request.updated_at = utc_now_iso()
        await self._save(request, "pending")
        get_audit_service().record(
            action=AuditAction.QUOTA_REQUEST_INFO_REQUESTED,
            actor=admin,
            target_type=TARGET_QUOTA_REQUEST,
            target_id=request_id,
            changes=["status"],
            before={"status": "pending"},
            after={"status": "info_requested"},
        )
        self.notifications.notify(
            recipient_email=request.user_email,
            kind="quota_request_info_requested",
            actor=admin,
            payload={"requestId": request_id, "note": request.admin_note},
        )
        return request

    async def deny(self, admin: User, request_id: str, note: str) -> QuotaRequest:
        request = await self.get_request(request_id)
        self._refuse_self(admin, request.user_id, TARGET_QUOTA_REQUEST, request_id, "deny")
        if not request.is_open:
            raise QuotaRequestStateError(f"This request is already {request.status}")
        expected = request.status
        request.status = "denied"
        request.updated_at = utc_now_iso()
        request.decision = QuotaRequestDecision(
            decided_by=admin.user_id,
            decided_by_email=(admin.email or "").lower() or None,
            decided_at=request.updated_at,
            outcome="denied",
            note=note.strip(),
        )
        await self._save(request, expected)
        get_audit_service().record(
            action=AuditAction.QUOTA_REQUEST_DENIED,
            actor=admin,
            target_type=TARGET_QUOTA_REQUEST,
            target_id=request_id,
            changes=["status"],
            before={"status": expected},
            after={"status": "denied"},
        )
        self.notifications.notify(
            recipient_email=request.user_email,
            kind="quota_request_decided",
            actor=admin,
            payload={"requestId": request_id, "outcome": "denied", "note": request.decision.note},
        )
        return request

    async def create_direct_extension(self, admin: User, body: ExtensionCreate) -> QuotaExtension:
        self._refuse_self(admin, body.user_id, TARGET_QUOTA_EXTENSION, body.user_id, "extend")
        policy = await self.get_policy()
        funding = self._validate_admin_grant(
            amount=body.amount, standing=body.standing, funding_mode=body.funding_mode,
            funding_source=body.funding_source, valid_until=body.valid_until, policy=policy,
        )
        subject = await self._user_for(body.user_id)
        if not subject.email:
            raise QuotaRequestNotFound("No such user in the directory")
        extension = await self._create_extension(
            admin, subject=subject, amount=body.amount, standing=body.standing,
            funding_mode=body.funding_mode, funding=funding, valid_from=body.valid_from,
            valid_until=body.valid_until, reason=body.reason.strip(), request_id=None, policy=policy,
        )
        self.notifications.notify(
            recipient_email=subject.email,
            kind="quota_extension_granted",
            actor=admin,
            payload={
                "extensionId": extension.extension_id,
                "standing": extension.standing,
                "amount": _money(extension.amount),
                "fundingLabel": (funding.label if funding else None),
                "through": body.valid_until.isoformat(),
                "note": (body.note or "").strip() or None,
            },
        )
        return extension

    async def list_extensions(self, *, user_id: Optional[str], active_only: bool) -> List[QuotaExtension]:
        if user_id:
            extensions = await self.quotas.list_user_extensions(user_id)
        else:
            extensions = await self.quotas.list_extensions()
        if active_only:
            now = utc_now_iso()
            extensions = [e for e in extensions if e.is_active(now)]
        return sorted(extensions, key=lambda e: e.created_at, reverse=True)

    async def revoke(self, admin: User, extension_id: str, reason: str) -> QuotaExtension:
        current = await self.quotas.get_extension(extension_id)
        if current is None:
            raise QuotaRequestNotFound("Quota extension not found")
        revoked = await self.quotas.revoke_extension(extension_id, revoked_by=admin.user_id, reason=reason.strip())
        if revoked is None:
            raise QuotaRequestStateError("This extension is already revoked")
        self._invalidate(revoked.user_id)
        get_audit_service().record(
            action=AuditAction.QUOTA_EXTENSION_REVOKED,
            actor=admin,
            target_type=TARGET_QUOTA_EXTENSION,
            target_id=extension_id,
            changes=["validUntil"],
            before={"validUntil": current.valid_until},
            after={"validUntil": revoked.valid_until, "revokedAt": revoked.revoked_at},
            reason=reason.strip(),
        )
        self.notifications.notify(
            recipient_email=revoked.user_email,
            kind="quota_extension_revoked",
            actor=admin,
            payload={"extensionId": extension_id, "reason": reason.strip()},
        )
        return revoked

    async def update_policy(self, admin: User, policy: QuotaRequestPolicy) -> QuotaRequestPolicy:
        if policy.report_group_field and policy.report_group_field not in policy.schema_keys:
            raise InvalidQuotaRequest("The report grouping field must be one of the funding fields")
        for field in policy.funding_fields:
            if field.pattern:
                try:
                    re.compile(field.pattern)
                except re.error as e:
                    raise InvalidQuotaRequest(f"{field.label}: invalid pattern ({e})") from e
        policy.notify_emails = sorted({e.strip().lower() for e in policy.notify_emails if e.strip()})
        policy.updated_by = admin.user_id
        policy.updated_at = utc_now_iso()
        return await self.requests.put_policy(policy)


_service: Optional[QuotaRequestService] = None


def get_quota_request_service() -> QuotaRequestService:
    global _service
    if _service is None:
        _service = QuotaRequestService()
    return _service
