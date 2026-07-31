"""Audit record model — the durable answer to "who changed this?".

`admin_service` has emitted structured log records on every role mutation since
before delegated admin scopes existed. Those are log lines: no retention
guarantee, no before/after values, and nothing an admin can read from the
console. With a single superuser that was tolerable. Once admin power can be
*delegated* (`docs/specs/granular-admin-permissions.md`), "which admin granted
this, and when?" becomes a question the platform has to be able to answer.

**Scoped to what changed.** `before`/`after` carry only the fields a mutation
actually touched, not whole role snapshots. A role definition with a wildcard
tool grant and a full model list is large, and two copies of it per update would
push items toward the 400KB DynamoDB ceiling for no investigative gain — the
question is always "what changed", never "what did the whole record look like".

**Denied attempts are recorded too.** `outcome` exists because a delegated admin
who *tries* to escalate is more interesting than one who succeeds, and the
write-through guard (`RoleMutationForbidden`) is the only place that signal
exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from apis.shared.timestamps import utc_now_iso

# Audit records expire a year out. Long enough to cover an academic year — the
# window in which "who granted this access last fall?" is a real question — and
# bounded so the table does not grow without end.
RETENTION_DAYS = 365


class AuditAction:
    """The closed set of audited actions.

    Deliberately a flat constant namespace rather than an Enum: these strings are
    persisted, queried, and rendered, so a value that drifts is a broken history.
    Adding one is a code change with a test, which is the point.

    **There is no ``tool_granted`` / ``skill_granted`` action, on purpose.** The
    spec counted eight log emission points and expected eight record types, but
    four of them — `add_tool_to_role`, `remove_tool_from_role`, and the skill
    pair — are thin wrappers that build an ``AppRoleUpdate`` and delegate to
    `update_role`. So does the write-through path the tools/models/skills admin
    pages use (`ToolService._add_tool_to_role`). Emitting a record from the
    wrapper *and* from `update_role` would write two rows for one mutation and
    make every grant look like it happened twice. `ROLE_UPDATED` already carries
    an exact ``granted_tools`` / ``granted_skills`` before/after, which is the
    same information without the double-count.
    """

    ROLE_CREATED = "app_role.created"
    ROLE_UPDATED = "app_role.updated"
    ROLE_DELETED = "app_role.deleted"
    ROLE_SYNCED = "app_role.synced"
    # A mutation the write-through guard refused — a delegated admin attempting
    # to reach a protected or scope-bearing role. Rarer than the rest and more
    # interesting than all of them.
    ROLE_MUTATION_DENIED = "app_role.mutation_denied"


ALL_ACTIONS: frozenset[str] = frozenset(
    v for k, v in vars(AuditAction).items() if not k.startswith("_") and isinstance(v, str)
)


class AuditOutcome:
    ALLOWED = "allowed"
    DENIED = "denied"


TARGET_APP_ROLE = "app_role"


@dataclass
class AuditRecord:
    """One durable record of an administrative mutation."""

    action: str
    actor_user_id: str
    actor_email: str
    target_type: str
    target_id: str

    outcome: str = AuditOutcome.ALLOWED
    # Field names the mutation touched. Redundant with `before`/`after` keys,
    # but kept so the console can render a summary without unpacking both.
    changes: List[str] = field(default_factory=list)
    before: Dict[str, Any] = field(default_factory=dict)
    after: Dict[str, Any] = field(default_factory=dict)
    # Set on a denied attempt — why the guard refused.
    reason: Optional[str] = None

    audit_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=utc_now_iso)

    # ------------------------------------------------------------------
    # Key construction
    #
    # Kept on the model rather than the repository because the console reads
    # these back and the shapes have to agree; a reader that builds keys a
    # different way silently returns nothing.
    # ------------------------------------------------------------------

    @property
    def pk(self) -> str:
        return f"AUDIT#{self.target_type}#{self.target_id}"

    @property
    def sk(self) -> str:
        # Timestamp first so a range query on the target is chronological; the
        # id suffix keeps two mutations in the same millisecond distinct.
        return f"{self.timestamp}#{self.audit_id}"

    @property
    def actor_pk(self) -> str:
        return f"ACTOR#{self.actor_user_id}"

    @property
    def recent_pk(self) -> str:
        """Month-sharded partition for the "recent activity" feed.

        A single ``AUDIT#ALL`` partition would collect every write on the
        platform forever, which is the textbook hot-partition shape. Sharding by
        month keeps writes spread and still lets the console page a useful unit —
        and a month boundary is a natural "load older" step for the UI.
        """
        return f"AUDIT#{self.timestamp[:7]}"  # YYYY-MM

    def expires_at(self) -> int:
        """Epoch seconds for the DynamoDB TTL attribute."""
        try:
            written = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except ValueError:
            written = datetime.now(timezone.utc)
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        return int((written + timedelta(days=RETENTION_DAYS)).timestamp())

    def to_item(self) -> Dict[str, Any]:
        """Serialize for DynamoDB."""
        item: Dict[str, Any] = {
            "PK": self.pk,
            "SK": self.sk,
            "GSI1PK": self.actor_pk,
            "GSI1SK": self.sk,
            "GSI2PK": self.recent_pk,
            "GSI2SK": self.sk,
            "auditId": self.audit_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actorUserId": self.actor_user_id,
            "actorEmail": self.actor_email,
            "targetType": self.target_type,
            "targetId": self.target_id,
            "outcome": self.outcome,
            "changes": self.changes,
            "expiresAt": self.expires_at(),
        }
        # Omit empty maps rather than storing {} — an absent key reads as "no
        # payload" on the console side without a special case for either.
        if self.before:
            item["before"] = self.before
        if self.after:
            item["after"] = self.after
        if self.reason:
            item["reason"] = self.reason
        return item

    @classmethod
    def from_item(cls, item: Dict[str, Any]) -> "AuditRecord":
        record = cls(
            action=item.get("action", ""),
            actor_user_id=item.get("actorUserId", ""),
            actor_email=item.get("actorEmail", ""),
            target_type=item.get("targetType", ""),
            target_id=item.get("targetId", ""),
            outcome=item.get("outcome", AuditOutcome.ALLOWED),
            changes=list(item.get("changes", []) or []),
            before=dict(item.get("before", {}) or {}),
            after=dict(item.get("after", {}) or {}),
            reason=item.get("reason"),
        )
        # Identity comes from the stored row, not a fresh default.
        record.audit_id = item.get("auditId", record.audit_id)
        record.timestamp = item.get("timestamp", record.timestamp)
        return record

    def to_response(self) -> Dict[str, Any]:
        """camelCase projection for the admin API."""
        payload: Dict[str, Any] = {
            "auditId": self.audit_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "actorUserId": self.actor_user_id,
            "actorEmail": self.actor_email,
            "targetType": self.target_type,
            "targetId": self.target_id,
            "outcome": self.outcome,
            "changes": self.changes,
            "before": self.before,
            "after": self.after,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload
