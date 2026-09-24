"""Platform self-service account tools (read-only pilot).

Skill 1 — *Account & Usage* from ``.kiro/specs/platform-self-service/``. These
answer questions about **this** platform and **this** user that the agent could
never know from its weights: "how much of my quota is left?", "what's my default
model?", "who am I on this platform?".

Two invariants hold across every tool here, and they are the whole safety story:

1. **Identity is captured by closure, never taken as a model argument.** Each
   tool is built per request by a ``make_*_tool(user)`` factory that closes over
   the authenticated ``User`` resolved from the validated session. The returned
   ``@tool`` function takes **no** user/id parameter, so the model cannot make it
   act on anyone but the invoking user — there is no argument through which to
   try. This mirrors the six existing per-request tool families
   (spreadsheet/artifact/word/excel/powerpoint/workspace): the runtime does not
   populate Strands' ToolContext, so closure capture is the blessed pattern here
   (``apis/shared/tools/injected.py``).

2. **Reads only, and they never raise.** Everything here is a read of the
   caller's own state through the same services the enforcement/settings paths
   use. A backing-service failure returns a friendly error dict — a self-service
   question must never break the turn. (Confirmed *writes* — e.g.
   ``set_default_model`` — are a later phase and are confirmation-gated.)

``get_my_quota`` deliberately does NOT call ``QuotaChecker.check_quota``: that
method records warning/block **events** as a side effect for the enforcement
path, and a mere "how much is left?" question must not fire a spurious 90%
warning. It computes the same numbers read-only via the resolver + cost
aggregator instead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from strands import tool

from apis.shared.auth.models import User

logger = logging.getLogger(__name__)

# A tier at or above this monthly limit is treated as effectively unlimited,
# matching ``QuotaChecker``'s own sentinel so the two paths agree.
_UNLIMITED_LIMIT_SENTINEL = 999999


def _current_period(period_type: str) -> str:
    """The cost-aggregation period string for ``period_type``.

    Mirrors ``QuotaChecker._get_current_period`` so a read here lines up with
    what enforcement counts against.
    """
    now = datetime.now(timezone.utc)
    if period_type == "daily":
        return now.strftime("%Y-%m-%d")
    return now.strftime("%Y-%m")


async def _read_quota_snapshot(user: User) -> Dict[str, Any]:
    """Compute the caller's quota numbers WITHOUT recording any quota event.

    Returns a dict shaped for the model to read directly. Never raises: on any
    failure it returns ``{"error": ...}`` so the tool degrades to a plain
    message rather than breaking the turn.
    """
    try:
        from apis.shared.quota import get_cost_aggregator, get_quota_resolver

        resolved = await get_quota_resolver().resolve_user_quota(user)
        if not resolved or not resolved.tier:
            return {
                "configured": False,
                "message": (
                    "No usage quota is configured for your account yet. "
                    "Ask an administrator to assign you a quota tier."
                ),
            }

        tier = resolved.tier
        tier_name = getattr(tier, "tier_name", None)

        monthly_limit = tier.monthly_cost_limit
        if monthly_limit == float("inf") or monthly_limit >= _UNLIMITED_LIMIT_SENTINEL:
            return {
                "configured": True,
                "unlimited": True,
                "tier": tier_name,
                "message": "Your plan has no usage limit (unlimited quota).",
            }

        period_type = getattr(tier, "period_type", "monthly") or "monthly"
        daily_limit = getattr(tier, "daily_cost_limit", None)
        if period_type == "daily" and daily_limit is not None:
            limit = float(daily_limit)
        else:
            limit = float(monthly_limit)

        period = _current_period(period_type)
        summary = await get_cost_aggregator().get_user_cost_summary(
            user_id=user.user_id, period=period
        )
        used = float(summary.total_cost)
        remaining = max(0.0, limit - used)
        pct = (used / limit * 100) if limit > 0 else 0.0

        return {
            "configured": True,
            "unlimited": False,
            "tier": tier_name,
            "period_type": period_type,
            "current_usage": round(used, 2),
            "quota_limit": round(limit, 2),
            "remaining": round(remaining, 2),
            "percentage_used": round(pct, 1),
            "message": (
                f"You have used ${used:.2f} of your ${limit:.2f} "
                f"{period_type} limit (${remaining:.2f} remaining, "
                f"{pct:.0f}% used)."
            ),
        }
    except Exception:  # noqa: BLE001 - a read must never break the turn
        logger.warning("get_my_quota read failed", exc_info=True)
        return {
            "error": "Could not read your quota right now. Please try again in a moment."
        }


def make_get_my_quota_tool(user: User):
    """Build the ``get_my_quota`` tool bound to ``user`` by closure."""

    @tool
    async def get_my_quota() -> dict:
        """Report how much of YOUR usage quota is left on this platform.

        Use this whenever the user asks about their quota, usage, spend,
        budget, or how much they have left. Returns the current spend, the
        limit, the remaining amount, and the percentage used for the signed-in
        user. Read-only — it never changes anything.
        """
        return await _read_quota_snapshot(user)

    return get_my_quota


def make_get_my_settings_tool(user: User):
    """Build the ``get_my_settings`` tool bound to ``user`` by closure."""

    @tool
    async def get_my_settings() -> dict:
        """Report YOUR current account settings on this platform.

        Use this when the user asks about their settings — for example their
        default model. Returns the signed-in user's stored settings. Read-only;
        a ``null`` default model means "use the platform default". To CHANGE a
        setting, tell the user which setting and value you would change and ask
        them to confirm — do not assume a change was requested.
        """
        try:
            from apis.shared.user_settings.repository import (
                get_user_settings_repository,
            )

            settings = await get_user_settings_repository().get_settings(user.user_id)
            default_model = settings.get("defaultModelId")
            return {
                "default_model_id": default_model,
                "message": (
                    "Your default model is the platform default (no explicit "
                    "choice saved)."
                    if not default_model
                    else f"Your default model is set to '{default_model}'."
                ),
            }
        except Exception:  # noqa: BLE001 - a read must never break the turn
            logger.warning("get_my_settings read failed", exc_info=True)
            return {
                "error": "Could not read your settings right now. Please try again in a moment."
            }

    return get_my_settings


def make_whoami_tool(user: User):
    """Build the ``whoami`` tool bound to ``user`` by closure."""

    @tool
    async def whoami() -> dict:
        """Report who the signed-in user is on this platform.

        Use this when the user asks who they are, what their account is, what
        roles or plan they have, or to ground an answer in their identity.
        Returns the signed-in user's name, email, roles, and quota tier.
        Read-only. Identity comes from the validated session, not from anything
        the user or the model typed.
        """
        info: Dict[str, Any] = {
            "name": user.name,
            "email": user.email,
            "roles": list(user.roles or []),
        }
        # Best-effort tier label; a failure here must not drop the identity.
        try:
            from apis.shared.quota import get_quota_resolver

            resolved = await get_quota_resolver().resolve_user_quota(user)
            if resolved and resolved.tier:
                info["quota_tier"] = getattr(resolved.tier, "tier_name", None)
        except Exception:  # noqa: BLE001
            logger.debug("whoami tier lookup failed", exc_info=True)
        return info

    return whoami
