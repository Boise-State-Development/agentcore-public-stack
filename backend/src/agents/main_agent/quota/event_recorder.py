"""Records quota enforcement events."""

from typing import Optional
from datetime import datetime, timezone
import uuid
import logging
from apis.shared.auth.models import User
from .models import QuotaTier, QuotaEvent
from .repository import QuotaRepository

logger = logging.getLogger(__name__)


class QuotaEventRecorder:
    """Records quota enforcement events (all event types)"""

    def __init__(self, repository: QuotaRepository):
        self.repository = repository

    async def record_block(
        self,
        user: User,
        tier: QuotaTier,
        current_usage: float,
        limit: float,
        percentage_used: float,
        session_id: Optional[str] = None,
        assignment_id: Optional[str] = None
    ):
        """Record quota block event"""
        event = QuotaEvent(
            event_id=str(uuid.uuid4()),
            user_id=user.user_id,
            tier_id=tier.tier_id,
            event_type="block",
            current_usage=current_usage,
            quota_limit=limit,
            percentage_used=percentage_used,
            timestamp=datetime.now(timezone.utc).isoformat() + 'Z',
            metadata={
                "tier_name": tier.tier_name,
                "session_id": session_id,
                "assignment_id": assignment_id,
                "user_name": user.name,
                "user_roles": user.roles
            }
        )

        try:
            await self.repository.record_event(event)
            logger.info(f"Recorded block event for user {user.user_id} (tier: {tier.tier_id})")
        except Exception as e:
            logger.error(f"Failed to record block event: {e}")

    async def record_warning_if_needed(
        self,
        user: User,
        tier: QuotaTier,
        current_usage: float,
        limit: float,
        percentage_used: float,
        threshold: str,
        session_id: Optional[str] = None,
        assignment_id: Optional[str] = None
    ):
        """
        Record warning event if user hasn't been warned recently.
        Prevents duplicate warnings within 60 minutes.
        """
        # Check for recent warning of this type
        recent_warning = await self.repository.get_recent_event(
            user_id=user.user_id,
            event_type="warning",
            within_minutes=60
        )

        if recent_warning and recent_warning.metadata:
            # Don't record if we've already warned about this threshold
            if recent_warning.metadata.get("threshold") == threshold:
                logger.debug(f"Skipping duplicate warning for user {user.user_id} at {threshold}")
                return

        # Record new warning
        event = QuotaEvent(
            event_id=str(uuid.uuid4()),
            user_id=user.user_id,
            tier_id=tier.tier_id,
            event_type="warning",
            current_usage=current_usage,
            quota_limit=limit,
            percentage_used=percentage_used,
            timestamp=datetime.now(timezone.utc).isoformat() + 'Z',
            metadata={
                "threshold": threshold,
                "tier_name": tier.tier_name,
                "session_id": session_id,
                "assignment_id": assignment_id,
                "user_name": user.name,
                "user_roles": user.roles
            }
        )

        try:
            await self.repository.record_event(event)
            logger.info(f"Recorded warning event for user {user.user_id} at {threshold}")
        except Exception as e:
            logger.error(f"Failed to record warning event: {e}")

    async def record_session_notice_if_needed(
        self,
        user: User,
        tier: QuotaTier,
        session_id: str,
        session_cost: float,
        limit: float,
        session_percentage: float,
        assignment_id: Optional[str] = None
    ):
        """
        Record that one conversation has reached the tier's session-notice
        share of the monthly limit.

        Deduped the same way warnings are — at most one per hour — but keyed
        on the *session*, so a second runaway conversation in the same hour
        still gets its own event. The durable record is what lets support
        answer "when did this conversation get expensive?" after the fact;
        the live SSE notice is emitted every turn while over the share, like
        ``quota_warning``.
        """
        recent_notice = await self.repository.get_recent_event(
            user_id=user.user_id,
            event_type="session_notice",
            within_minutes=60
        )

        if recent_notice and recent_notice.metadata:
            if recent_notice.metadata.get("session_id") == session_id:
                logger.debug(
                    f"Skipping duplicate session notice for session {session_id}"
                )
                return

        event = QuotaEvent(
            event_id=str(uuid.uuid4()),
            user_id=user.user_id,
            tier_id=tier.tier_id,
            event_type="session_notice",
            current_usage=session_cost,
            quota_limit=limit,
            percentage_used=session_percentage,
            timestamp=datetime.now(timezone.utc).isoformat() + 'Z',
            metadata={
                "session_id": session_id,
                "session_cost": str(session_cost),
                "tier_name": tier.tier_name,
                "assignment_id": assignment_id,
                "user_name": user.name,
                "user_roles": user.roles
            }
        )

        try:
            await self.repository.record_event(event)
            logger.info(
                f"Recorded session notice for user {user.user_id} "
                f"(session share: {session_percentage:.1f}%)"
            )
        except Exception as e:
            logger.error(f"Failed to record session notice event: {e}")

    async def record_override_applied(
        self,
        user: User,
        override_id: str,
        tier: QuotaTier
    ):
        """Record when an override is applied"""
        event = QuotaEvent(
            event_id=str(uuid.uuid4()),
            user_id=user.user_id,
            tier_id=tier.tier_id,
            event_type="override_applied",
            current_usage=0.0,
            quota_limit=tier.monthly_cost_limit,
            percentage_used=0.0,
            timestamp=datetime.now(timezone.utc).isoformat() + 'Z',
            metadata={
                "override_id": override_id,
                "tier_name": tier.tier_name,
                "user_name": user.name,
                "user_roles": user.roles
            }
        )

        try:
            await self.repository.record_event(event)
            logger.info(f"Recorded override applied for user {user.user_id} (override: {override_id})")
        except Exception as e:
            logger.error(f"Failed to record override event: {e}")

    async def record_reset(
        self,
        user: User,
        tier: QuotaTier,
        reason: str
    ):
        """Record quota reset event"""
        event = QuotaEvent(
            event_id=str(uuid.uuid4()),
            user_id=user.user_id,
            tier_id=tier.tier_id,
            event_type="reset",
            current_usage=0.0,
            quota_limit=tier.monthly_cost_limit,
            percentage_used=0.0,
            timestamp=datetime.now(timezone.utc).isoformat() + 'Z',
            metadata={
                "reason": reason,
                "tier_name": tier.tier_name,
                "user_email": user.email
            }
        )

        try:
            await self.repository.record_event(event)
            logger.info(f"Recorded reset event for user {user.user_id} (reason: {reason})")
        except Exception as e:
            logger.error(f"Failed to record reset event: {e}")
