"""AuditService — the write path for administrative audit records.

**An audit write never fails the mutation it describes.** If DynamoDB is
unavailable, the right outcome is a role change that happened and an audit gap
that is screamed about in the logs — not an admin console that stops working
because its bookkeeping is down. The pre-existing structured log line
(``extra={"event": ...}``) is still emitted at every call site, so a failed
durable write degrades to exactly the behaviour this feature replaced rather
than to nothing.

That trade-off is deliberate and worth naming, because the opposite choice is
also defensible: in an environment where the audit trail is a compliance
control, "no record, no change" is the correct posture. Flipping it means
raising from :meth:`AuditService.record`; nothing else needs to move.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from apis.shared.auth.models import User

from .models import (
    TARGET_APP_ROLE,
    AuditOutcome,
    AuditRecord,
)
from .repository import AuditRepository

logger = logging.getLogger(__name__)


class AuditService:
    """Records administrative mutations durably."""

    def __init__(self, repository: Optional[AuditRepository] = None):
        self._repository = repository
        self._warned_unconfigured = False

    @property
    def configured(self) -> bool:
        """Whether there is a table to write to.

        The audit table ships in `platform.yml` (CDK) while the code that writes
        to it ships in `backend.yml`, and day-to-day backend changes deploy
        without a CDK run. An app-api that assumed the table existed would take
        every role mutation down in the window between those two deploys, and
        in any environment where the infra deploy simply has not happened yet.

        So: no table name configured means no audit sink, not a hard failure.
        The structured log lines at each call site still land.
        """
        return bool(self._repository or os.environ.get("DYNAMODB_AUDIT_LOG_TABLE_NAME"))

    @property
    def repository(self) -> AuditRepository:
        # Constructed lazily so importing this module never requires the table
        # to be configured — `admin_service` imports it unconditionally, and the
        # unit tests for role mutation do not want a DynamoDB resource.
        if self._repository is None:
            self._repository = AuditRepository()
        return self._repository

    def record(
        self,
        *,
        action: str,
        actor: User,
        target_id: str,
        target_type: str = TARGET_APP_ROLE,
        outcome: str = AuditOutcome.ALLOWED,
        changes: Optional[List[str]] = None,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
    ) -> Optional[AuditRecord]:
        """Write one audit record. Returns None if the write failed.

        Never raises — see the module docstring.
        """
        if not self.configured:
            if not self._warned_unconfigured:
                self._warned_unconfigured = True
                logger.warning(
                    "DYNAMODB_AUDIT_LOG_TABLE_NAME is unset — admin mutations "
                    "are not being recorded durably (structured logs only)",
                    extra={"event": "audit_sink_unconfigured"},
                )
            return None

        record = AuditRecord(
            action=action,
            actor_user_id=actor.user_id,
            actor_email=actor.email,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            changes=sorted(changes or []),
            before=before or {},
            after=after or {},
            reason=reason,
        )
        try:
            self.repository.put(record)
            return record
        except Exception:
            logger.exception(
                "Audit write failed — the mutation stands but is unrecorded",
                extra={
                    "event": "audit_write_failed",
                    "action": action,
                    "target_id": target_id,
                    "actor_user_id": actor.user_id,
                },
            )
            return None


_audit_service: Optional[AuditService] = None


def get_audit_service() -> AuditService:
    """Process-wide AuditService."""
    global _audit_service
    if _audit_service is None:
        _audit_service = AuditService()
    return _audit_service


def reset_audit_service() -> None:
    """Drop the cached instance. For tests."""
    global _audit_service
    _audit_service = None


def diff_fields(
    before_obj: Any,
    after_obj: Any,
    fields: List[str],
) -> tuple[List[str], Dict[str, Any], Dict[str, Any]]:
    """Compare named attributes on two objects.

    Returns ``(changed_field_names, before_values, after_values)`` carrying only
    the fields that actually differ — the audit record stores what changed, not
    a pair of whole snapshots (see `models` for why).

    Lists are compared order-insensitively: `granted_tools` and friends are
    normalized/sorted on write, so a pure reordering is not a change an admin
    made and should not read as one in the history.
    """
    changed: List[str] = []
    before: Dict[str, Any] = {}
    after: Dict[str, Any] = {}

    for name in fields:
        old = getattr(before_obj, name, None)
        new = getattr(after_obj, name, None)

        if isinstance(old, list) and isinstance(new, list):
            if sorted(map(str, old)) == sorted(map(str, new)):
                continue
        elif old == new:
            continue

        changed.append(name)
        before[name] = old
        after[name] = new

    return changed, before, after
