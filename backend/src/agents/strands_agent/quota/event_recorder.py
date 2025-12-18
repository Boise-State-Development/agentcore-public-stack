"""Records quota enforcement events."""

from typing import Optional
from datetime import datetime
import uuid
import logging
from apis.shared.auth.models import User
from .models import QuotaTier, QuotaEvent
from .repository import QuotaRepository

logger = logging.getLogger(__name__)


class QuotaEventRecorder:
    """Records quota enforcement events (Phase 1: blocks only)"""

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
            timestamp=datetime.utcnow().isoformat() + 'Z',
            metadata={
                "tier_name": tier.tier_name,
                "session_id": session_id,
                "assignment_id": assignment_id,
                "user_email": user.email,
                "user_roles": user.roles
            }
        )

        try:
            await self.repository.record_event(event)
            logger.info(f"Recorded block event for user {user.user_id} (tier: {tier.tier_id})")
        except Exception as e:
            logger.error(f"Failed to record block event: {e}")
