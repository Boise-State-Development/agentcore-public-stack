"""Signed-in apps and terminals: `/auth/devices` and the admin revoke (native-app-auth.md D9).

The routes read the user's device pointers, join them with the session rows,
and end a device's session the way logout does: the row deleted and the
revocation published, and the refresh token revoked unless the user's headless
grant holds it.
"""

from __future__ import annotations

import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from apis.app_api.admin.users import routes as admin_users_routes
from apis.app_api.auth.bff import devices_router
from apis.app_api.auth.bff import routes as bff_routes
from apis.shared.auth.models import User
from apis.shared.harness.grants import HeadlessGrant
from apis.shared.sessions_bff.cache import get_default_cache
from apis.shared.sessions_bff.models import (
    DEVICE_KIND_NATIVE,
    DEVICE_KIND_TERMINAL,
    DevicePointer,
    SessionRecord,
    device_id_for,
)

USER = "user-sub-001"
OTHER = "user-sub-002"


def _record(session_id: str, *, user_id: str = USER, token: str = "r.t", seen: Optional[int] = None) -> SessionRecord:
    now = int(time.time())
    return SessionRecord(
        session_id=session_id,
        user_id=user_id,
        username="alice",
        cognito_access_token="a.t",
        cognito_refresh_token=token,
        id_token=None,
        access_token_exp=now + 3600,
        csrf_secret="c",
        created_at=now - 3600,
        last_seen_at=seen if seen is not None else now,
        ttl=now + 86_400,
        client="native",
        device_model="iPhone",
        app_version="1.41.0",
    )


def _seed(repository, record: SessionRecord, *, kind: str = DEVICE_KIND_NATIVE, pointer: bool = True) -> str:
    repository._table.put_item(Item=repository._record_to_item(record))
    device_id = device_id_for(record.session_id)
    if pointer:
        item = {
            "PK": f"DEVICES#{record.user_id}",
            "SK": f"DEVICE#{device_id}",
            "device_id": device_id,
            "session_id": record.session_id,
            "user_id": record.user_id,
            "kind": kind,
            "created_at": record.created_at,
            "ttl": record.ttl,
        }
        if kind == DEVICE_KIND_NATIVE:
            item.update(device_model="iPhone", app_version="1.41.0")
        repository._table.put_item(Item=item)
    return device_id


def _row_exists(repository, session_id: str) -> bool:
    return "Item" in repository._table.get_item(Key={"PK": f"SESSION#{session_id}", "SK": "META"})


def _pointer_exists(repository, user_id: str, device_id: str) -> bool:
    return "Item" in repository._table.get_item(Key={"PK": f"DEVICES#{user_id}", "SK": f"DEVICE#{device_id}"})


class _AttachSession(BaseHTTPMiddleware):
    """Stands in for SessionRefreshMiddleware: the caller's own session."""

    def __init__(self, app, *, record: SessionRecord) -> None:
        super().__init__(app)
        self._record = record

    async def dispatch(self, request, call_next):
        request.state.bff_session = self._record
        return await call_next(request)


class FakeGrants:
    enabled = True

    def __init__(self, token: Optional[str] = None):
        self.token = token

    async def get_active_grant(self, user_id: str):
        if self.token is None:
            return None
        now = int(time.time())
        return HeadlessGrant(
            grant_id="hlg-1",
            user_id=user_id,
            username="alice",
            cognito_refresh_token=self.token,
            status="active",
            created_at=now,
            updated_at=now,
            token_issued_at=now,
            ttl=now + 86_400,
            pinned_session_id="sess-phone",
        )


@pytest.fixture
def validator():
    user = User(email="user@example.edu", user_id=USER, name="A Member", roles=["Faculty"], picture=None)
    fake = MagicMock()
    fake.validate_token.return_value = user

    async def _noop(_user):
        return None

    with patch("apis.shared.auth.dependencies._get_bff_cognito_validator", return_value=fake), patch(
        "apis.shared.auth.dependencies._enrich_user_from_store", side_effect=_noop
    ), patch("apis.shared.auth.dependencies._get_user_sync_service", return_value=None):
        yield fake


@pytest.fixture
def revoke(monkeypatch):
    cognito = MagicMock()
    cognito.enabled = True
    cognito.revoke = AsyncMock()
    monkeypatch.setattr(bff_routes, "_get_cognito_client", lambda: cognito)
    return cognito.revoke


@pytest.fixture
def grants(monkeypatch):
    fake = FakeGrants()
    monkeypatch.setattr(bff_routes, "_get_grant_service", lambda: fake)
    return fake


def _client(app: FastAPI, current: SessionRecord) -> TestClient:
    app.include_router(devices_router)
    app.add_middleware(_AttachSession, record=current)
    return TestClient(app)


# ─── GET /auth/devices ────────────────────────────────────────────────


def test_lists_own_apps_and_terminals_newest_use_first_and_marks_the_caller(app, repository, validator):
    now = int(time.time())
    phone = _record("sess-phone", seen=now - 60)
    terminal = _record("sess-term", seen=now - 600)
    phone_id = _seed(repository, phone)
    terminal_id = _seed(repository, terminal, kind=DEVICE_KIND_TERMINAL)
    _seed(repository, _record("sess-theirs", user_id=OTHER))

    response = _client(app, phone).get("/auth/devices")

    assert response.status_code == 200
    devices = response.json()["devices"]
    assert [d["deviceId"] for d in devices] == [phone_id, terminal_id]
    assert devices[0] == {
        "deviceId": phone_id,
        "kind": "native",
        "deviceModel": "iPhone",
        "appVersion": "1.41.0",
        "signedInAt": devices[0]["signedInAt"],
        "lastUsedAt": devices[0]["lastUsedAt"],
        "expiresAt": devices[0]["expiresAt"],
        "current": True,
    }
    assert devices[1]["kind"] == "terminal" and devices[1]["current"] is False
    # Never a session id.
    assert "sess-" not in response.text


def test_a_pointer_whose_session_is_gone_is_dropped(app, repository, validator):
    phone = _record("sess-phone")
    _seed(repository, phone)
    stale_id = _seed(repository, _record("sess-gone"))
    repository._table.delete_item(Key={"PK": "SESSION#sess-gone", "SK": "META"})

    devices = _client(app, phone).get("/auth/devices").json()["devices"]

    assert [d["deviceId"] for d in devices] == [device_id_for("sess-phone")]
    assert not _pointer_exists(repository, USER, stale_id)


def test_listing_needs_a_session(app, repository):
    app.include_router(devices_router)
    assert TestClient(app).get("/auth/devices").status_code == 401


# ─── DELETE /auth/devices/{deviceId} ─────────────────────────────────


def test_signing_a_device_out_revokes_its_session_and_token(app, repository, validator, revoke, grants):
    web = _record("sess-web")
    phone = _record("sess-phone", token="phone.r.t")
    device_id = _seed(repository, phone)
    get_default_cache().set(phone)

    response = _client(app, web).delete(f"/auth/devices/{device_id}")

    assert response.status_code == 200
    assert response.json() == {"revoked": 1, "scheduledRunsContinue": False}
    assert not _row_exists(repository, "sess-phone")
    assert not _pointer_exists(repository, USER, device_id)
    assert get_default_cache().get("sess-phone") is None
    revoke.assert_awaited_once_with(refresh_token="phone.r.t")
    # Published for every other app-api task.
    feed = repository._table.query(
        KeyConditionExpression="PK = :p", ExpressionAttributeValues={":p": "REVOKED"}
    )["Items"]
    assert [i["session_id"] for i in feed] == ["sess-phone"]


def test_a_token_the_headless_grant_holds_is_kept_and_said_so(app, repository, validator, revoke, grants):
    grants.token = "phone.r.t"
    phone = _record("sess-phone", token="phone.r.t")
    device_id = _seed(repository, phone)

    response = _client(app, _record("sess-web")).delete(f"/auth/devices/{device_id}")

    assert response.json() == {"revoked": 1, "scheduledRunsContinue": True}
    # The device is signed out, the grant's token is not revoked.
    assert not _row_exists(repository, "sess-phone")
    revoke.assert_not_awaited()


def test_another_users_device_is_not_found_and_untouched(app, repository, validator, revoke, grants):
    theirs = _record("sess-theirs", user_id=OTHER)
    device_id = _seed(repository, theirs)

    response = _client(app, _record("sess-web")).delete(f"/auth/devices/{device_id}")

    assert response.status_code == 404
    assert _row_exists(repository, "sess-theirs")
    revoke.assert_not_awaited()


@pytest.mark.parametrize("device_id", ["0" * 32, "not-a-device-id", "sess-phone"])
def test_an_unknown_or_malformed_id_is_not_found(app, repository, validator, revoke, grants, device_id):
    _seed(repository, _record("sess-phone"))
    response = _client(app, _record("sess-web")).delete(f"/auth/devices/{device_id}")
    assert response.status_code == 404
    assert _row_exists(repository, "sess-phone")


def test_revoking_needs_a_session_before_anything_else(app, repository, revoke):
    app.include_router(devices_router)
    assert TestClient(app).delete("/auth/devices/not-even-an-id").status_code == 401


# ─── DELETE /admin/users/{userId}/devices ────────────────────────────


def test_an_admin_signs_out_every_app_and_terminal_and_it_is_audited(app, repository, revoke, grants, monkeypatch):
    phone = _seed(repository, _record("sess-phone", token="p.t"))
    terminal = _seed(repository, _record("sess-term", token="t.t"), kind=DEVICE_KIND_TERMINAL)
    web = _record("sess-web")
    repository._table.put_item(Item=repository._record_to_item(web))  # a browser session has no pointer
    theirs = _seed(repository, _record("sess-other", user_id=OTHER))

    admin = User(email="admin@example.edu", user_id="admin-1", name="Admin", roles=["Admin"], picture=None)
    audit = MagicMock()
    monkeypatch.setattr(admin_users_routes, "get_audit_service", lambda: audit)
    app.include_router(admin_users_routes.router, prefix="/admin")
    app.dependency_overrides[admin_users_routes.require_users_admin] = lambda: admin

    response = TestClient(app).delete(f"/admin/users/{USER}/devices")

    assert response.status_code == 200
    assert response.json() == {"revoked": 2, "scheduledRunsContinue": False}
    assert not _row_exists(repository, "sess-phone") and not _row_exists(repository, "sess-term")
    assert _row_exists(repository, "sess-web")
    assert _row_exists(repository, "sess-other") and _pointer_exists(repository, OTHER, theirs)
    assert not _pointer_exists(repository, USER, phone) and not _pointer_exists(repository, USER, terminal)
    assert revoke.await_count == 2
    audit.record.assert_called_once()
    kwargs = audit.record.call_args.kwargs
    assert kwargs["action"] == "user.devices_revoked"
    assert kwargs["target_type"] == "user" and kwargs["target_id"] == USER
    assert kwargs["after"] == {"revoked": 2, "scheduledRunsContinue": False}


# ─── Pointers are written where a session is handed over ─────────────


@pytest.mark.asyncio
async def test_a_pointer_round_trips(repository):
    record = _record("sess-phone")
    pointer = DevicePointer.for_session(
        record, kind=DEVICE_KIND_NATIVE, ttl=record.ttl, device_model="iPhone", app_version="1.41.0"
    )
    await repository.put_device_pointer(pointer)
    assert await repository.list_device_pointers(USER) == [pointer]
    assert await repository.get_device_pointer(USER, pointer.device_id) == pointer
    assert await repository.list_device_pointers(OTHER) == []
    await repository.delete_device_pointer(USER, pointer.device_id)
    assert await repository.list_device_pointers(USER) == []


def test_device_ids_are_stable_and_not_the_session_id():
    assert device_id_for("sess-phone") == device_id_for("sess-phone")
    assert device_id_for("sess-phone") != device_id_for("sess-term")
    assert "sess" not in device_id_for("sess-phone") and len(device_id_for("sess-phone")) == 32
