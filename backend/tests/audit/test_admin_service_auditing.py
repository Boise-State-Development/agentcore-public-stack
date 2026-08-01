"""Role mutations land in the audit trail with the right before/after.

The regression this exists to catch: `update_role` mutates the fetched role
**in place**, so a diff taken after the update loop compares the object to
itself and reports nothing changed. That failure is silent — records are still
written, they just claim no changes — which is worse than no audit log at all,
because it looks like one.
"""

from __future__ import annotations

import pytest

from apis.shared.audit.models import AuditAction, AuditOutcome
from apis.shared.audit.service import AuditService
from apis.shared.auth.models import User
from apis.shared.rbac.admin_service import AppRoleAdminService
from apis.shared.rbac.models import AppRoleCreate, AppRoleUpdate
from apis.shared.rbac.role_constraints import RoleMutationForbidden


class RecordingRepository:
    def __init__(self):
        self.written = []

    def put(self, record):
        self.written.append(record)


@pytest.fixture
def audit_sink():
    return RecordingRepository()


@pytest.fixture
def admin():
    return User(
        email="admin@example.com",
        user_id="admin-1",
        name="Admin User",
        roles=["Admin"],
    )


@pytest.fixture
def service(mock_app_role_repo, mock_app_role_cache, audit_sink):
    return AppRoleAdminService(
        repository=mock_app_role_repo,
        cache=mock_app_role_cache,
        audit=AuditService(repository=audit_sink),
    )


def only(records, action):
    matches = [r for r in records if r.action == action]
    assert len(matches) == 1, f"expected exactly one {action}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_records_the_full_grant_set(
    service, mock_app_role_repo, audit_sink, admin, make_app_role
):
    mock_app_role_repo.role_exists.return_value = False
    mock_app_role_repo.create_role.return_value = make_app_role(role_id="analyst")

    await service.create_role(
        AppRoleCreate(
            roleId="analyst",
            displayName="Analyst",
            grantedAdminScopes=["admin.costs"],
        ),
        admin,
    )

    record = only(audit_sink.written, AuditAction.ROLE_CREATED)
    assert record.target_id == "analyst"
    assert record.actor_user_id == "admin-1"
    # No prior state to diff against — the grants are the record.
    assert record.after["granted_admin_scopes"] == ["admin.costs"]
    assert record.before == {}


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_records_a_real_before_and_after(
    service, mock_app_role_repo, audit_sink, admin, make_app_role
):
    existing = make_app_role(role_id="analyst", granted_admin_scopes=[])
    mock_app_role_repo.get_role.return_value = existing
    mock_app_role_repo.update_role.return_value = existing

    await service.update_role(
        "analyst", AppRoleUpdate(grantedAdminScopes=["admin.costs"]), admin
    )

    record = only(audit_sink.written, AuditAction.ROLE_UPDATED)
    assert record.changes == ["granted_admin_scopes"]
    assert record.before == {"granted_admin_scopes": []}
    assert record.after == {"granted_admin_scopes": ["admin.costs"]}


@pytest.mark.asyncio
async def test_update_records_only_the_field_that_changed(
    service, mock_app_role_repo, audit_sink, admin, make_app_role
):
    """The role form posts every field on every save.

    A record built from the submitted payload would claim the admin edited ten
    fields when they renamed the role.
    """
    existing = make_app_role(
        role_id="analyst",
        display_name="Analyst",
        granted_tools=["calculator"],
        priority=5,
    )
    mock_app_role_repo.get_role.return_value = existing
    mock_app_role_repo.update_role.return_value = existing

    await service.update_role(
        "analyst",
        AppRoleUpdate(
            displayName="Data Analyst",
            grantedTools=["calculator"],
            priority=5,
        ),
        admin,
    )

    record = only(audit_sink.written, AuditAction.ROLE_UPDATED)
    assert record.changes == ["display_name"]


@pytest.mark.asyncio
async def test_a_no_op_update_writes_no_record(
    service, mock_app_role_repo, audit_sink, admin, make_app_role
):
    existing = make_app_role(role_id="analyst", display_name="Analyst")
    mock_app_role_repo.get_role.return_value = existing
    mock_app_role_repo.update_role.return_value = existing

    await service.update_role("analyst", AppRoleUpdate(displayName="Analyst"), admin)

    assert audit_sink.written == []


@pytest.mark.asyncio
async def test_granting_a_tool_records_one_row_not_two(
    service, mock_app_role_repo, audit_sink, admin, make_app_role
):
    """`add_tool_to_role` delegates to `update_role`.

    If both emitted, every grant made from the tools admin page would appear
    twice in the trail.
    """
    existing = make_app_role(role_id="analyst", granted_tools=[])
    mock_app_role_repo.get_role.return_value = existing
    mock_app_role_repo.update_role.return_value = existing

    await service.add_tool_to_role("analyst", "calculator", admin)

    assert len(audit_sink.written) == 1
    assert audit_sink.written[0].action == AuditAction.ROLE_UPDATED
    assert audit_sink.written[0].changes == ["granted_tools"]


# ---------------------------------------------------------------------------
# Delete and sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_records_what_the_role_conferred(
    service, mock_app_role_repo, audit_sink, admin, make_app_role
):
    """Once the role is gone this record is the only remaining evidence."""
    mock_app_role_repo.get_role.return_value = make_app_role(
        role_id="analyst", granted_admin_scopes=["admin.costs"]
    )
    mock_app_role_repo.delete_role.return_value = True

    await service.delete_role("analyst", admin)

    record = only(audit_sink.written, AuditAction.ROLE_DELETED)
    assert record.before["granted_admin_scopes"] == ["admin.costs"]


@pytest.mark.asyncio
async def test_a_failed_delete_records_nothing(
    service, mock_app_role_repo, audit_sink, admin, make_app_role
):
    mock_app_role_repo.get_role.return_value = make_app_role(role_id="analyst")
    mock_app_role_repo.delete_role.return_value = False

    await service.delete_role("analyst", admin)

    assert audit_sink.written == []


@pytest.mark.asyncio
async def test_sync_is_recorded(
    service, mock_app_role_repo, audit_sink, admin, make_app_role
):
    existing = make_app_role(role_id="analyst")
    mock_app_role_repo.get_role.return_value = existing
    mock_app_role_repo.update_role.return_value = existing

    await service.sync_effective_permissions("analyst", admin)

    only(audit_sink.written, AuditAction.ROLE_SYNCED)


# ---------------------------------------------------------------------------
# Denied escalation — the record worth having even though nothing changed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_mutation_is_recorded_as_denied(
    mock_app_role_repo, mock_app_role_cache, audit_sink, admin, make_app_role
):
    service = AppRoleAdminService(
        repository=mock_app_role_repo,
        cache=mock_app_role_cache,
        audit=AuditService(repository=audit_sink),
    )
    mock_app_role_repo.get_role.return_value = make_app_role(role_id="privileged")

    async def refuse(role, actor):
        raise RoleMutationForbidden("cannot mutate a scope-bearing role")

    service._assert_actor_may_mutate = refuse

    with pytest.raises(RoleMutationForbidden):
        await service.update_role(
            "privileged", AppRoleUpdate(displayName="nice try"), admin
        )

    record = only(audit_sink.written, AuditAction.ROLE_MUTATION_DENIED)
    assert record.outcome == AuditOutcome.DENIED
    assert record.actor_user_id == "admin-1"
    assert "scope-bearing" in record.reason


@pytest.mark.asyncio
async def test_an_audit_outage_does_not_fail_the_mutation(
    mock_app_role_repo, mock_app_role_cache, admin, make_app_role
):
    class ExplodingRepository:
        def put(self, record):
            raise RuntimeError("dynamodb is having a day")

    service = AppRoleAdminService(
        repository=mock_app_role_repo,
        cache=mock_app_role_cache,
        audit=AuditService(repository=ExplodingRepository()),
    )
    existing = make_app_role(role_id="analyst", display_name="Analyst")
    mock_app_role_repo.get_role.return_value = existing
    mock_app_role_repo.update_role.return_value = existing

    result = await service.update_role(
        "analyst", AppRoleUpdate(displayName="Renamed"), admin
    )

    assert result is not None
