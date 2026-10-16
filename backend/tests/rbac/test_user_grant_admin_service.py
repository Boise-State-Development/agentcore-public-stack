"""Writes to a direct user grant: validation, cache invalidation, audit."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from apis.shared.audit import AuditService
from apis.shared.audit.models import TARGET_USER_GRANT, AuditAction
from apis.shared.auth.models import User
from apis.shared.rbac.models import UserGrant, UserGrantUpdate
from apis.shared.rbac.user_grant_admin_service import (
    MAX_IDS_PER_LIST,
    UnknownUserError,
    UserGrantAdminService,
    UserGrantValidationError,
)


class RecordingRepository:
    def __init__(self):
        self.written = []

    def put(self, record):
        self.written.append(record)


KNOWN_TOOLS = {"browse_web", "calculator"}
KNOWN_MODELS = {"m.pilot"}
KNOWN_SKILLS = {"s.pilot"}


@pytest.fixture
def audit_sink():
    return RecordingRepository()


@pytest.fixture
def admin():
    return User(email="admin@example.com", user_id="admin-1", name="Admin", roles=["Admin"])


@pytest.fixture
def service(mock_app_role_repo, mock_app_role_cache, audit_sink):
    async def tool_exists(tool_id):
        return tool_id in KNOWN_TOOLS

    async def model_exists(model_id):
        return model_id in KNOWN_MODELS

    async def skill_exists(skill_id):
        return skill_id in KNOWN_SKILLS

    async def user_exists(user_id):
        return user_id != "ghost"

    return UserGrantAdminService(
        repository=mock_app_role_repo,
        cache=mock_app_role_cache,
        audit=AuditService(repository=audit_sink),
        tool_exists=tool_exists,
        model_exists=model_exists,
        skill_exists=skill_exists,
        user_exists=user_exists,
    )


def _future(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def only(records, action):
    matches = [r for r in records if r.action == action]
    assert len(matches) == 1, f"expected exactly one {action}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# set_grant — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_grant_stores_sorted_deduped_ids(service, mock_app_role_repo, admin):
    update = UserGrantUpdate(
        granted_tools=["calculator", "browse_web", "calculator"],
        granted_models=["m.pilot"],
        granted_skills=["s.pilot"],
        note="  pilot  ",
    )

    stored = await service.set_grant("user-1", update, admin)

    assert stored.granted_tools == ["browse_web", "calculator"]
    assert stored.granted_models == ["m.pilot"]
    assert stored.granted_skills == ["s.pilot"]
    assert stored.note == "pilot"
    assert stored.granted_by == "admin@example.com"
    mock_app_role_repo.put_user_grant.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_grant_invalidates_this_users_cache_and_bumps_the_watermark(
    service, mock_app_role_cache, admin
):
    from apis.shared.rbac.version import get_roles_version

    before = get_roles_version()
    await service.set_grant("user-1", UserGrantUpdate(granted_tools=["browse_web"]), admin)

    mock_app_role_cache.invalidate_user.assert_awaited_once_with("user-1")
    assert get_roles_version() > before


@pytest.mark.asyncio
async def test_set_grant_records_an_audit_row_with_before_and_after(
    service, mock_app_role_repo, audit_sink, admin
):
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["calculator"]
    )

    await service.set_grant(
        "user-1", UserGrantUpdate(granted_tools=["browse_web", "calculator"]), admin
    )

    record = only(audit_sink.written, AuditAction.USER_GRANT_UPDATED)
    assert record.target_type == TARGET_USER_GRANT
    assert record.target_id == "user-1"
    assert record.actor_email == "admin@example.com"
    assert record.changes == ["granted_tools"]
    assert record.before == {"granted_tools": ["calculator"]}
    assert record.after == {"granted_tools": ["browse_web", "calculator"]}


@pytest.mark.asyncio
async def test_unchanged_save_records_nothing(service, mock_app_role_repo, audit_sink, admin):
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["calculator"], note=""
    )

    await service.set_grant("user-1", UserGrantUpdate(granted_tools=["calculator"]), admin)

    assert audit_sink.written == []


@pytest.mark.asyncio
async def test_set_grant_normalises_expiry_to_utc(service, admin):
    stored = await service.set_grant(
        "user-1",
        UserGrantUpdate(granted_tools=["browse_web"], expires_at="2999-01-01T12:00:00Z"),
        admin,
    )
    assert stored.expires_at == "2999-01-01T12:00:00+00:00"


@pytest.mark.asyncio
async def test_empty_update_removes_an_existing_grant(
    service, mock_app_role_repo, audit_sink, admin
):
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["calculator"]
    )

    result = await service.set_grant("user-1", UserGrantUpdate(), admin)

    assert result is None
    mock_app_role_repo.delete_user_grant.assert_awaited_once_with("user-1")
    mock_app_role_repo.put_user_grant.assert_not_awaited()
    only(audit_sink.written, AuditAction.USER_GRANT_DELETED)


@pytest.mark.asyncio
async def test_empty_update_with_no_grant_writes_nothing(
    service, mock_app_role_repo, audit_sink, admin
):
    mock_app_role_repo.get_user_grant.return_value = None

    result = await service.set_grant("user-1", UserGrantUpdate(), admin)

    assert result is None
    mock_app_role_repo.delete_user_grant.assert_not_awaited()
    mock_app_role_repo.put_user_grant.assert_not_awaited()
    assert audit_sink.written == []


# ---------------------------------------------------------------------------
# set_grant — refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_user_is_refused_before_anything_is_written(
    service, mock_app_role_repo, admin
):
    with pytest.raises(UnknownUserError):
        await service.set_grant("ghost", UserGrantUpdate(granted_tools=["browse_web"]), admin)
    mock_app_role_repo.put_user_grant.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update",
    [
        UserGrantUpdate(granted_tools=["*"]),
        UserGrantUpdate(granted_models=["*"]),
        UserGrantUpdate(granted_skills=["*"]),
    ],
)
async def test_wildcard_is_refused(service, mock_app_role_repo, admin, update):
    with pytest.raises(UserGrantValidationError, match="'\\*'"):
        await service.set_grant("user-1", update, admin)
    mock_app_role_repo.put_user_grant.assert_not_awaited()


@pytest.mark.asyncio
async def test_scoped_tool_ref_is_refused(service, admin):
    with pytest.raises(UserGrantValidationError, match="whole server"):
        await service.set_grant(
            "user-1", UserGrantUpdate(granted_tools=["browse_web::fetch"]), admin
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update,kind",
    [
        (UserGrantUpdate(granted_tools=["no_such_tool"]), "tool"),
        (UserGrantUpdate(granted_models=["no.such.model"]), "model"),
        (UserGrantUpdate(granted_skills=["no_such_skill"]), "skill"),
    ],
)
async def test_unknown_catalog_id_is_refused(service, admin, update, kind):
    with pytest.raises(UserGrantValidationError, match=f"Unknown {kind} id"):
        await service.set_grant("user-1", update, admin)


@pytest.mark.asyncio
async def test_blank_id_is_refused(service, admin):
    with pytest.raises(UserGrantValidationError, match="non-empty"):
        await service.set_grant("user-1", UserGrantUpdate(granted_tools=["  "]), admin)


@pytest.mark.asyncio
async def test_oversized_list_is_refused(service, admin):
    ids = [f"t{i}" for i in range(MAX_IDS_PER_LIST + 1)]
    with pytest.raises(UserGrantValidationError, match="At most"):
        await service.set_grant("user-1", UserGrantUpdate(granted_tools=ids), admin)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["yesterday", "2000-01-01T00:00:00+00:00"])
async def test_bad_or_past_expiry_is_refused(service, admin, value):
    with pytest.raises(UserGrantValidationError, match="expiresAt"):
        await service.set_grant(
            "user-1", UserGrantUpdate(granted_tools=["browse_web"], expires_at=value), admin
        )


# ---------------------------------------------------------------------------
# delete_grant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_grant_invalidates_and_audits(
    service, mock_app_role_repo, mock_app_role_cache, audit_sink, admin
):
    mock_app_role_repo.get_user_grant.return_value = UserGrant(
        user_id="user-1", granted_tools=["calculator"], granted_models=["m.pilot"]
    )

    assert await service.delete_grant("user-1", admin) is True

    mock_app_role_cache.invalidate_user.assert_awaited_once_with("user-1")
    record = only(audit_sink.written, AuditAction.USER_GRANT_DELETED)
    assert record.before["granted_tools"] == ["calculator"]
    assert record.before["granted_models"] == ["m.pilot"]
    assert record.after == {}


@pytest.mark.asyncio
async def test_delete_grant_when_none_exists_is_a_no_op(
    service, mock_app_role_repo, mock_app_role_cache, audit_sink, admin
):
    mock_app_role_repo.get_user_grant.return_value = None

    assert await service.delete_grant("user-1", admin) is False

    mock_app_role_repo.delete_user_grant.assert_not_awaited()
    mock_app_role_cache.invalidate_user.assert_not_awaited()
    assert audit_sink.written == []


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holders_dispatches_by_kind(service, mock_app_role_repo):
    mock_app_role_repo.get_user_ids_for_tool = AsyncMock(return_value=["a"])
    mock_app_role_repo.get_user_ids_for_model = AsyncMock(return_value=["b"])
    mock_app_role_repo.get_user_ids_for_skill = AsyncMock(return_value=["c"])

    assert await service.holders("tool", "x") == ["a"]
    assert await service.holders("model", "x") == ["b"]
    assert await service.holders("skill", "x") == ["c"]
    with pytest.raises(UserGrantValidationError):
        await service.holders("role", "x")


@pytest.mark.asyncio
async def test_list_grants_is_sorted_by_user(service, mock_app_role_repo):
    mock_app_role_repo.list_user_grants.return_value = [
        UserGrant(user_id="zed"),
        UserGrant(user_id="amy"),
    ]
    assert [g.user_id for g in await service.list_grants()] == ["amy", "zed"]


# ---------------------------------------------------------------------------
# default lookups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_check_is_skipped_when_no_users_table_is_configured():
    from apis.shared.rbac import user_grant_admin_service as mod

    with patch.dict("os.environ", {"DYNAMODB_USERS_TABLE_NAME": ""}):
        assert await mod._user_exists("anyone") is True
