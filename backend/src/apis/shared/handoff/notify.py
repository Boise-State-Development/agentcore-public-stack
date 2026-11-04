"""Inbox entries for a handoff's state changes (``docs/specs/agent-handoffs-and-consults.md`` §4.3, PR-0.3b).

The second of the three notification layers: the row and the toast reach a user who is in the app,
the inbox reaches one who missed them, reloaded, or closed the tab mid-turn. Written by the Runtime
right after the source row's status write, which is itself after the stream has ended, so nothing
here is on the TTFT path.

The payload names the conversations and the Agent, never what was asked or said: an inbox entry
outlives the tab and the bell is not where a user expects conversation text.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from apis.shared.handoff.metrics import emit_handoff_metric
from apis.shared.handoff.models import Handoff, HandoffStatus
from apis.shared.notifications import NotificationKind, NotificationService

logger = logging.getLogger(__name__)

#: The states that need the user, and the inbox kind each one writes.
HANDOFF_NOTIFICATION_KINDS: Dict[str, NotificationKind] = {
    "needs_input": "handoff_needs_input",
    "done": "handoff_done",
    "stopped": "handoff_stopped",
}

#: A stop the user pressed themselves tells them nothing they don't know.
_SILENT_STOP_REASONS = frozenset({"user_stopped"})

_service: Optional[NotificationService] = None


def _svc() -> NotificationService:
    global _service
    if _service is None:
        _service = NotificationService()
    return _service


def notification_payload(handoff: Handoff, *, target_session_id: str) -> Dict[str, str]:
    """What an entry carries: ids and display names only."""
    return {
        "handoffId": handoff.handoff_id,
        "sourceSessionId": handoff.session_id,
        "targetSessionId": handoff.target_session_id or target_session_id,
        "agentId": handoff.agent_id,
        "agentName": handoff.agent_name,
        "sourceTitle": handoff.source_title,
    }


def notify_handoff_state(
    handoff: Handoff,
    status: HandoffStatus,
    *,
    recipient_email: Optional[str],
    target_session_id: str,
    stopped_reason: Optional[str] = None,
    service: Optional[NotificationService] = None,
) -> bool:
    """Put one entry in the owner's inbox for a state change that needs them. Never raises.

    Call only for a transition that actually moved the row, so a turn that repeats a state the row
    can't take again (a frozen ``done``) writes nothing. Returns whether an entry was written.
    """
    kind = HANDOFF_NOTIFICATION_KINDS.get(status)
    if kind is None or (status == "stopped" and stopped_reason in _SILENT_STOP_REASONS):
        return False
    if not recipient_email:
        # A token with no email claim: the inbox is keyed by email, so there is nowhere to write.
        logger.info("Handoff %s notification skipped: no email for the user", kind)
        return False
    try:
        written = (service or _svc()).notify_own(
            recipient_email=recipient_email,
            kind=kind,
            payload=notification_payload(handoff, target_session_id=target_session_id),
        )
    except Exception:  # noqa: BLE001 - a courtesy; the row already says what happened
        logger.warning("Handoff %s notification failed", kind, exc_info=True)
        return False
    if written is None:
        return False
    emit_handoff_metric("HandoffNotified", handoff.kind, session_id=handoff.session_id, detail="inbox")
    return True


__all__ = ["HANDOFF_NOTIFICATION_KINDS", "notification_payload", "notify_handoff_state"]
