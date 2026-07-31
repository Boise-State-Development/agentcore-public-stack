"""AuditService write path — the deliberate fail-open, and the deploy-order guard.

Two behaviours here are choices rather than defaults, and both are the kind that
get "fixed" by someone who reads the code without the reasoning:

  * a failed audit write must not fail the mutation it describes, and
  * an unconfigured table must be inert rather than fatal, because the table
    ships in `platform.yml` while the code that writes to it ships in
    `backend.yml` — and day-to-day backend deploys do not run CDK.
"""

from __future__ import annotations

import pytest

from apis.shared.audit.models import TARGET_APP_ROLE, AuditAction, AuditOutcome
from apis.shared.audit.service import AuditService, get_audit_service, reset_audit_service
from apis.shared.auth.models import User

ADMIN = User(
    email="admin@example.com",
    user_id="admin-1",
    name="Admin",
    roles=["Admin"],
)


class RecordingRepository:
    def __init__(self):
        self.written = []

    def put(self, record):
        self.written.append(record)


class ExplodingRepository:
    def put(self, record):
        raise RuntimeError("dynamodb is having a day")


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_audit_service()
    yield
    reset_audit_service()


def test_records_a_mutation() -> None:
    repo = RecordingRepository()
    service = AuditService(repository=repo)

    result = service.record(
        action=AuditAction.ROLE_UPDATED,
        actor=ADMIN,
        target_id="analyst",
        changes=["granted_admin_scopes"],
        before={"granted_admin_scopes": []},
        after={"granted_admin_scopes": ["admin.costs"]},
    )

    assert result is not None
    assert len(repo.written) == 1
    written = repo.written[0]
    assert written.action == AuditAction.ROLE_UPDATED
    assert written.actor_email == "admin@example.com"
    assert written.target_type == TARGET_APP_ROLE
    assert written.after == {"granted_admin_scopes": ["admin.costs"]}


def test_changes_are_sorted_for_a_stable_record() -> None:
    repo = RecordingRepository()
    AuditService(repository=repo).record(
        action=AuditAction.ROLE_UPDATED,
        actor=ADMIN,
        target_id="analyst",
        changes=["priority", "display_name"],
    )
    assert repo.written[0].changes == ["display_name", "priority"]


def test_a_failed_write_returns_none_rather_than_raising() -> None:
    """The mutation already happened; taking the request down helps nobody."""
    service = AuditService(repository=ExplodingRepository())

    result = service.record(
        action=AuditAction.ROLE_DELETED, actor=ADMIN, target_id="analyst"
    )

    assert result is None


def test_denial_records_carry_a_reason() -> None:
    repo = RecordingRepository()
    AuditService(repository=repo).record(
        action=AuditAction.ROLE_MUTATION_DENIED,
        actor=ADMIN,
        target_id="system_admin",
        outcome=AuditOutcome.DENIED,
        reason="not permitted to mutate a scope-bearing role",
    )

    written = repo.written[0]
    assert written.outcome == AuditOutcome.DENIED
    assert "scope-bearing" in written.reason


def test_unconfigured_service_is_inert(monkeypatch) -> None:
    """No table name means no sink — not a crash on every role mutation."""
    monkeypatch.delenv("DYNAMODB_AUDIT_LOG_TABLE_NAME", raising=False)
    service = AuditService()

    assert service.configured is False
    assert service.record(
        action=AuditAction.ROLE_CREATED, actor=ADMIN, target_id="analyst"
    ) is None


def test_configured_once_the_table_name_is_present(monkeypatch) -> None:
    monkeypatch.setenv("DYNAMODB_AUDIT_LOG_TABLE_NAME", "some-audit-table")
    assert AuditService().configured is True


def test_an_injected_repository_counts_as_configured(monkeypatch) -> None:
    """So tests and callers that supply a sink do not need the env var."""
    monkeypatch.delenv("DYNAMODB_AUDIT_LOG_TABLE_NAME", raising=False)
    assert AuditService(repository=RecordingRepository()).configured is True


def test_get_audit_service_is_a_singleton() -> None:
    assert get_audit_service() is get_audit_service()
