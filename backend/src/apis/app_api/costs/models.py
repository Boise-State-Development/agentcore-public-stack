"""Response models for the user-facing cost/quota surface.

These are the *self* (caller-scoped) analogues of the admin quota views in
``apis.app_api.admin.users.models``. They are deliberately read-only: the
``GET /costs/quota-status`` endpoint resolves the caller's own tier and spend
without recording any quota-enforcement events (that side effect belongs to
the chat path's ``QuotaChecker.check_quota``, not to a status read).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class UserQuotaStatusResponse(BaseModel):
    """The authenticated user's own quota status for the current period.

    Surfaced on the Usage settings page (the denominator behind the existing
    usage/spend numbers) and in the composer cost-counter tooltip.

    Three real states the UI must distinguish:

    * ``configured=False`` — no tier is assigned to this user yet. Nothing to
      show a bar against; the UI should say so rather than render ``$X of $0``.
    * ``unlimited=True`` — an unlimited tier/override. There is no denominator,
      so the UI shows usage without a percentage.
    * normal — ``monthlyLimit`` is populated and the bar/percentage are real.
    """

    model_config = ConfigDict(populate_by_name=True, by_alias=True)

    configured: bool = Field(
        ...,
        description="Whether a quota tier is resolved for this user at all",
    )
    unlimited: bool = Field(
        False,
        description="True for an unlimited tier/override (no meaningful denominator)",
    )

    tier_name: Optional[str] = Field(None, alias="tierName")
    matched_by: Optional[str] = Field(
        None,
        alias="matchedBy",
        description="How the tier resolved (direct_user, jwt_role:Faculty, override, ...)",
    )

    # None when unconfigured or unlimited; a positive dollar amount otherwise.
    monthly_limit: Optional[float] = Field(None, alias="monthlyLimit")

    # Current-period spend for this user (matches the tier's period_type, which
    # is monthly for effectively every tier).
    current_usage: float = Field(0.0, alias="currentUsage")
    remaining: Optional[float] = Field(
        None,
        description="Dollars left before the limit; None when unconfigured/unlimited",
    )
    usage_percentage: float = Field(
        0.0,
        alias="usagePercentage",
        description="Current usage as a percentage of the limit (0 when no limit)",
    )

    period_type: str = Field(
        "monthly",
        alias="periodType",
        description="Quota period the limit applies to (monthly or daily)",
    )
    reset_info: Optional[str] = Field(
        None,
        alias="resetInfo",
        description="Human-readable note about when the quota resets",
    )
    has_active_override: bool = Field(False, alias="hasActiveOverride")
