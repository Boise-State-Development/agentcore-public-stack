"""HTTP surface for direct user grants: ``/admin/user-grants``."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.user_grants.routes import router
from apis.shared.auth.models import User
from apis.shared.rbac.models import UserGrant
from apis.shared.rbac.user_grant_admin_service import (
    UnknownUserError,
    UserGrantValidationError,
)
from tests.conftest import override_admin_auth


def _admin() -> User:
    return User(email="admin@example.com", user_id="admin-1", name="Admin", roles=["system_admin"])


@pytest.fixture
def service():
    svc = AsyncMock()
    svc.get_grant = AsyncMock(return_value=None)
    svc.list_grants = AsyncMock(return_value=[])
    svc.holders = AsyncMock(return_value=[])
    svc.set_grant = AsyncMock(return_value=None)
    svc.delete_grant = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def client(service) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    override_admin_auth(app, _admin)
    with patch(
        "apis.app_api.admin.user_grants.routes.get_user_grant_admin_service",
        return_value=service,
    ):
        yield TestClient(app)


def test_get_without_a_grant_serves_the_empty_shape(client):
    resp = client.get("/admin/user-grants/user-1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["userId"] == "user-1"
    assert body["grantedTools"] == []
    assert body["grantedModels"] == []
    assert body["grantedSkills"] == []
    assert body["active"] is True


def test_get_serves_the_stored_grant(client, service):
    service.get_grant.return_value = UserGrant(
        user_id="user-1",
        granted_tools=["browse_web"],
        expires_at="2000-01-01T00:00:00+00:00",
        granted_by="admin@example.com",
    )

    body = client.get("/admin/user-grants/user-1").json()

    assert body["grantedTools"] == ["browse_web"]
    assert body["grantedBy"] == "admin@example.com"
    assert body["active"] is False


def test_put_replaces_and_returns_the_grant(client, service):
    service.set_grant.return_value = UserGrant(user_id="user-1", granted_tools=["browse_web"])

    resp = client.put("/admin/user-grants/user-1", json={"grantedTools": ["browse_web"]})

    assert resp.status_code == 200
    assert resp.json()["grantedTools"] == ["browse_web"]
    _, kwargs_or_args = service.set_grant.await_args
    user_id, update, admin = service.set_grant.await_args.args
    assert user_id == "user-1"
    assert update.granted_tools == ["browse_web"]
    assert admin.user_id == "admin-1"


def test_put_that_grants_nothing_returns_the_empty_shape(client, service):
    service.set_grant.return_value = None

    resp = client.put("/admin/user-grants/user-1", json={})

    assert resp.status_code == 200
    assert resp.json()["grantedTools"] == []


def test_put_unknown_user_is_404(client, service):
    service.set_grant.side_effect = UnknownUserError("nope")

    resp = client.put("/admin/user-grants/ghost", json={"grantedTools": ["browse_web"]})

    assert resp.status_code == 404


def test_put_validation_failure_is_400_with_the_reason(client, service):
    service.set_grant.side_effect = UserGrantValidationError("Unknown tool id(s): nope.")

    resp = client.put("/admin/user-grants/user-1", json={"grantedTools": ["nope"]})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Unknown tool id(s): nope."


def test_put_rejects_an_overlong_note(client):
    resp = client.put("/admin/user-grants/user-1", json={"note": "x" * 501})

    assert resp.status_code == 422


def test_delete_is_204(client, service):
    resp = client.delete("/admin/user-grants/user-1")

    assert resp.status_code == 204
    service.delete_grant.assert_awaited_once()


def test_delete_without_a_grant_is_404(client, service):
    service.delete_grant.return_value = False

    assert client.delete("/admin/user-grants/user-1").status_code == 404


def test_list_flags_expired_grants(client, service):
    service.list_grants.return_value = [
        UserGrant(user_id="a", granted_tools=["x"]),
        UserGrant(user_id="b", granted_tools=["x"], expires_at="2000-01-01T00:00:00+00:00"),
    ]

    body = client.get("/admin/user-grants").json()

    assert body["total"] == 2
    assert [g["active"] for g in body["grants"]] == [True, False]


def test_holders_passes_model_ids_with_dots_and_colons(client, service):
    service.holders.return_value = ["user-1"]

    resp = client.get("/admin/user-grants/for/model/us.anthropic.claude-haiku-4-5:0")

    assert resp.status_code == 200
    assert resp.json() == {
        "kind": "model",
        "resourceId": "us.anthropic.claude-haiku-4-5:0",
        "userIds": ["user-1"],
    }
    service.holders.assert_awaited_once_with("model", "us.anthropic.claude-haiku-4-5:0")


def test_holders_rejects_an_unknown_kind(client):
    assert client.get("/admin/user-grants/for/role/x").status_code == 400


def test_routes_require_full_admin():
    """Every route carries an authorization dependency (the coverage test's backstop)."""
    import inspect

    for route in router.routes:
        sources = [inspect.getsource(d.call) for d in route.dependant.dependencies if d.call]
        assert any("resolve_user_permissions" in s for s in sources), route.path
