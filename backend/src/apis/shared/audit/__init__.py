"""Durable audit trail for administrative mutations.

See `docs/specs/granular-admin-permissions.md` §8 — with a single superuser,
structured log lines were a tolerable answer to "who changed this?". Delegated
admin scopes make it a real question, so the existing emission points in
`rbac.admin_service` now also write queryable records with before/after values.
"""

from .models import (
    ALL_ACTIONS,
    RETENTION_DAYS,
    TARGET_APP_ROLE,
    AuditAction,
    AuditOutcome,
    AuditRecord,
)
from .repository import AuditRepository
from .service import (
    AuditService,
    diff_fields,
    get_audit_service,
    reset_audit_service,
)

__all__ = [
    "ALL_ACTIONS",
    "RETENTION_DAYS",
    "TARGET_APP_ROLE",
    "AuditAction",
    "AuditOutcome",
    "AuditRecord",
    "AuditRepository",
    "AuditService",
    "diff_fields",
    "get_audit_service",
    "reset_audit_service",
]
