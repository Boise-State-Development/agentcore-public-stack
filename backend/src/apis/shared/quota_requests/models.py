"""Quota requests and the request policy (docs/specs/quota-requests-and-extensions.md §7).

Rows live in the ``user-quotas`` table beside tiers, assignments, overrides and
extensions. No new table and no new index: requests reuse GSI1/GSI2 under
``QUOTA_REQUEST#`` prefixes that the assignment reads (exact-match
``ASSIGNMENT_TYPE#`` / ``USER#`` partitions) can never return.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.main_agent.quota.models import FundingMode, FundingSource

RequestStatus = Literal["pending", "info_requested", "approved", "denied", "withdrawn"]
RequestKind = Literal["one_time", "standing"]

OPEN_STATUSES: frozenset[str] = frozenset({"pending", "info_requested"})

JUSTIFICATION_MAX_CHARS = 2000
NOTE_MAX_CHARS = 2000
# Admin routes enforce only a sane ceiling, so a fat-fingered 25000 is caught (§8.6).
ADMIN_AMOUNT_CEILING = Decimal("10000")


def _to_decimal(v):
    if v is None or isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        return Decimal(v)
    return v


class FundingField(BaseModel):
    """One field of the deployment's funding-source schema (§4.3)."""

    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(..., min_length=1, max_length=40, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    label: str = Field(..., min_length=1, max_length=80)
    required: bool = False
    pattern: Optional[str] = Field(None, max_length=200)
    help: Optional[str] = Field(None, max_length=200)


class QuotaRequestPolicy(BaseModel):
    """The one admin-editable record that shapes requests at runtime (§4.5).

    A missing row means these defaults.
    """

    model_config = ConfigDict(populate_by_name=True)

    amount_presets: List[Decimal] = Field(
        default_factory=lambda: [Decimal("10"), Decimal("25"), Decimal("50")], alias="amountPresets"
    )
    allow_custom_amount: bool = Field(True, alias="allowCustomAmount")
    # 0 = no cap on what a user may ask for; admins can always grant more.
    max_request_amount: Decimal = Field(Decimal("100"), alias="maxRequestAmount", ge=0)
    # "end_of_period" or "days:N".
    default_through: str = Field("end_of_period", alias="defaultThrough", pattern=r"^(end_of_period|days:\d{1,3})$")
    max_duration_days: int = Field(92, alias="maxDurationDays", ge=1, le=366)
    justification_min_chars: int = Field(20, alias="justificationMinChars", ge=0, le=JUSTIFICATION_MAX_CHARS)
    funding_source_mode: Literal["hidden", "optional", "required"] = Field("optional", alias="fundingSourceMode")
    funding_fields: List[FundingField] = Field(default_factory=list, alias="fundingFields")
    default_funding_mode: FundingMode = Field("central", alias="defaultFundingMode")
    standing_enabled: bool = Field(True, alias="standingEnabled")
    standing_default_cap: Decimal = Field(Decimal("100"), alias="standingDefaultCap", ge=0)
    fiscal_year_end: str = Field("12-31", alias="fiscalYearEnd", pattern=r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
    report_group_field: Optional[str] = Field(None, alias="reportGroupField")
    notify_emails: List[str] = Field(default_factory=list, alias="notifyEmails")

    updated_by: Optional[str] = Field(None, alias="updatedBy")
    updated_at: Optional[str] = Field(None, alias="updatedAt")

    @field_validator("amount_presets", mode="before")
    @classmethod
    def _presets(cls, v):
        return [_to_decimal(x) for x in v] if isinstance(v, (list, tuple)) else v

    @field_validator("max_request_amount", "standing_default_cap", mode="before")
    @classmethod
    def _decimal(cls, v):
        return _to_decimal(v)

    @field_validator("funding_fields")
    @classmethod
    def _unique_keys(cls, v: List[FundingField]) -> List[FundingField]:
        keys = [f.key for f in v]
        if len(keys) != len(set(keys)):
            raise ValueError("funding field keys must be unique")
        return v

    @property
    def schema_keys(self) -> List[str]:
        return [f.key for f in self.funding_fields]


class QuotaRequestContext(BaseModel):
    """What the user saw when they asked. Numbers only, never conversation content (§5.5)."""

    model_config = ConfigDict(populate_by_name=True)

    tier_id: Optional[str] = Field(None, alias="tierId")
    tier_name: Optional[str] = Field(None, alias="tierName")
    period_type: str = Field("monthly", alias="periodType")
    period: str
    base_limit: Optional[Decimal] = Field(None, alias="baseLimit")
    effective_limit: Optional[Decimal] = Field(None, alias="effectiveLimit")
    current_usage: Decimal = Field(Decimal("0"), alias="currentUsage")
    percentage_used: Decimal = Field(Decimal("0"), alias="percentageUsed")
    blocked: bool = False

    @field_validator("base_limit", "effective_limit", "current_usage", "percentage_used", mode="before")
    @classmethod
    def _decimal(cls, v):
        return _to_decimal(v)


class QuotaRequestDecision(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    decided_by: str = Field(..., alias="decidedBy")
    decided_by_email: Optional[str] = Field(None, alias="decidedByEmail")
    decided_at: str = Field(..., alias="decidedAt")
    outcome: Literal["approved", "denied"]
    note: Optional[str] = None
    amount: Optional[Decimal] = None
    valid_until: Optional[str] = Field(None, alias="validUntil")
    funding_mode: Optional[FundingMode] = Field(None, alias="fundingMode")
    standing: Optional[bool] = None
    extension_id: Optional[str] = Field(None, alias="extensionId")
    # When the extension this approval created was revoked. Joined from the
    # extension when the requester lists their requests; never stored, because
    # a revoke acts on the extension and leaves the request "approved" (§7.6).
    extension_revoked_at: Optional[str] = Field(None, alias="extensionRevokedAt")

    @field_validator("amount", mode="before")
    @classmethod
    def _decimal(cls, v):
        return _to_decimal(v)


class QuotaRequest(BaseModel):
    """A user's ask for more quota (§7.1)."""

    model_config = ConfigDict(populate_by_name=True)

    request_id: str = Field(..., alias="requestId")
    user_id: str = Field(..., alias="userId")
    user_email: str = Field("", alias="userEmail")
    status: RequestStatus = "pending"
    kind: RequestKind = "one_time"
    requested_amount: Decimal = Field(..., alias="requestedAmount", gt=0)
    requested_until: str = Field(..., alias="requestedUntil", description="ISO date, inclusive")
    justification: str
    funding_source: Optional[FundingSource] = Field(None, alias="fundingSource")
    funding_key: Optional[str] = Field(None, alias="fundingKey")
    context: QuotaRequestContext
    admin_note: Optional[str] = Field(None, alias="adminNote")
    decision: Optional[QuotaRequestDecision] = None
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")

    @field_validator("requested_amount", mode="before")
    @classmethod
    def _decimal(cls, v):
        return _to_decimal(v)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


# ========== Request bodies ==========


class QuotaRequestCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: RequestKind = "one_time"
    requested_amount: Decimal = Field(..., alias="requestedAmount", gt=0)
    requested_until: date = Field(..., alias="requestedUntil")
    justification: str = Field(..., max_length=JUSTIFICATION_MAX_CHARS)
    funding_source: Optional[FundingSource] = Field(None, alias="fundingSource")


class QuotaRequestPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    funding_source: Optional[FundingSource] = Field(None, alias="fundingSource")
    withdraw: bool = False


class ApproveRequestBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # None is legal only for a standing approval (uncapped).
    amount: Optional[Decimal] = Field(None, gt=0)
    valid_until: date = Field(..., alias="validUntil")
    funding_mode: FundingMode = Field("central", alias="fundingMode")
    funding_source: Optional[FundingSource] = Field(None, alias="fundingSource")
    # Defaults to the request's own kind.
    standing: Optional[bool] = None
    note: Optional[str] = Field(None, max_length=NOTE_MAX_CHARS)


class NoteBody(BaseModel):
    note: str = Field(..., min_length=1, max_length=NOTE_MAX_CHARS)


class ExtensionCreate(BaseModel):
    """A direct extension, with or without a request (§6.4)."""

    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(..., alias="userId", min_length=1)
    standing: bool = False
    amount: Optional[Decimal] = Field(None, gt=0)
    valid_from: Optional[date] = Field(None, alias="validFrom")
    valid_until: date = Field(..., alias="validUntil")
    funding_mode: FundingMode = Field("central", alias="fundingMode")
    funding_source: Optional[FundingSource] = Field(None, alias="fundingSource")
    reason: str = Field(..., min_length=1, max_length=NOTE_MAX_CHARS)
    note: Optional[str] = Field(None, max_length=NOTE_MAX_CHARS)


class RevokeBody(BaseModel):
    reason: str = Field(..., min_length=1, max_length=NOTE_MAX_CHARS)


class UserFacingPolicy(BaseModel):
    """The subset of the policy the request dialog needs (``GET /quota/policy``)."""

    model_config = ConfigDict(populate_by_name=True)

    amount_presets: List[Decimal] = Field(..., alias="amountPresets")
    allow_custom_amount: bool = Field(..., alias="allowCustomAmount")
    max_request_amount: Decimal = Field(..., alias="maxRequestAmount")
    default_through: str = Field(..., alias="defaultThrough")
    max_duration_days: int = Field(..., alias="maxDurationDays")
    justification_min_chars: int = Field(..., alias="justificationMinChars")
    funding_source_mode: str = Field(..., alias="fundingSourceMode")
    funding_fields: List[FundingField] = Field(..., alias="fundingFields")
    standing_enabled: bool = Field(..., alias="standingEnabled")
    fiscal_year_end: str = Field(..., alias="fiscalYearEnd")

    @classmethod
    def from_policy(cls, policy: QuotaRequestPolicy) -> "UserFacingPolicy":
        return cls(
            amount_presets=policy.amount_presets,
            allow_custom_amount=policy.allow_custom_amount,
            max_request_amount=policy.max_request_amount,
            default_through=policy.default_through,
            max_duration_days=policy.max_duration_days,
            justification_min_chars=policy.justification_min_chars,
            funding_source_mode=policy.funding_source_mode,
            funding_fields=policy.funding_fields,
            # A user can ask for a standing arrangement only where funds are collected.
            standing_enabled=(
                policy.standing_enabled
                and policy.funding_source_mode != "hidden"
                and bool(policy.funding_fields)
            ),
            fiscal_year_end=policy.fiscal_year_end,
        )
