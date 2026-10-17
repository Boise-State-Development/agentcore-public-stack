"""Route tests for GET/PUT /admin/auth-providers/login-page-settings."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from tests.conftest import override_admin_auth

ROUTES = "apis.app_api.admin.auth_providers.routes"
URL = "/admin/auth-providers/login-page-settings"


def _admin() -> User:
    return User(
        email="admin@example.com",
        user_id="user-001",
        name="Test Admin",
        roles=["Admin"],
    )


@pytest.fixture
def client():
    from apis.app_api.admin.auth_providers.routes import router

    app = FastAPI()
    admin_router = APIRouter(prefix="/admin")
    admin_router.include_router(router)
    app.include_router(admin_router)
    override_admin_auth(app, _admin)
    return TestClient(app)


def _settings_repo(item=None, enabled=True):
    repo = MagicMock()
    repo.enabled = enabled
    repo.get_login_page_settings = AsyncMock(return_value=item)

    async def _put(hide_cognito_login, updated_by):
        return {
            "hideCognitoLogin": hide_cognito_login,
            "updatedAt": "2026-10-17T00:00:00+00:00",
            "updatedBy": updated_by,
        }

    repo.put_login_page_settings = AsyncMock(side_effect=_put)
    return repo


def _provider_service(enabled_count):
    service = MagicMock()
    service.list_providers = AsyncMock(return_value=[object()] * enabled_count)
    return service


def _patched(repo, enabled_count):
    return (
        patch(f"{ROUTES}.get_system_settings_repository", return_value=repo),
        patch(
            f"{ROUTES}.get_auth_provider_service",
            return_value=_provider_service(enabled_count),
        ),
    )


class TestGetLoginPageSettings:
    def test_defaults_when_never_saved(self, client):
        repo_patch, service_patch = _patched(_settings_repo(), enabled_count=0)
        with repo_patch, service_patch:
            resp = client.get(URL)

        assert resp.status_code == 200
        assert resp.json() == {
            "hide_cognito_login": False,
            "cognito_login_visible": True,
            "enabled_provider_count": 0,
            "updated_at": None,
            "updated_by": None,
        }

    def test_saved_hide_with_no_enabled_provider_still_shows(self, client):
        repo = _settings_repo({"hideCognitoLogin": True, "updatedBy": "user-001"})
        repo_patch, service_patch = _patched(repo, enabled_count=0)
        with repo_patch, service_patch:
            body = client.get(URL).json()

        assert body["hide_cognito_login"] is True
        assert body["cognito_login_visible"] is True

    def test_defaults_when_storage_not_configured(self, client):
        repo = _settings_repo(enabled=False)
        repo_patch, service_patch = _patched(repo, enabled_count=1)
        with repo_patch, service_patch:
            body = client.get(URL).json()

        assert body["hide_cognito_login"] is False
        repo.get_login_page_settings.assert_not_called()


class TestUpdateLoginPageSettings:
    def test_hides_when_a_provider_is_enabled(self, client):
        repo = _settings_repo()
        repo_patch, service_patch = _patched(repo, enabled_count=1)
        with repo_patch, service_patch:
            resp = client.put(URL, json={"hide_cognito_login": True})

        assert resp.status_code == 200
        body = resp.json()
        assert body["hide_cognito_login"] is True
        assert body["cognito_login_visible"] is False
        assert body["updated_by"] == "user-001"
        repo.put_login_page_settings.assert_awaited_once_with(
            hide_cognito_login=True, updated_by="user-001"
        )

    def test_refuses_to_hide_without_an_enabled_provider(self, client):
        repo = _settings_repo()
        repo_patch, service_patch = _patched(repo, enabled_count=0)
        with repo_patch, service_patch:
            resp = client.put(URL, json={"hide_cognito_login": True})

        assert resp.status_code == 422
        repo.put_login_page_settings.assert_not_called()

    def test_showing_is_always_allowed(self, client):
        repo = _settings_repo({"hideCognitoLogin": True})
        repo_patch, service_patch = _patched(repo, enabled_count=0)
        with repo_patch, service_patch:
            resp = client.put(URL, json={"hide_cognito_login": False})

        assert resp.status_code == 200
        assert resp.json()["cognito_login_visible"] is True

    def test_503_when_storage_not_configured(self, client):
        repo = _settings_repo(enabled=False)
        repo_patch, service_patch = _patched(repo, enabled_count=1)
        with repo_patch, service_patch:
            resp = client.put(URL, json={"hide_cognito_login": True})

        assert resp.status_code == 503

    def test_rejects_missing_body_field(self, client):
        repo_patch, service_patch = _patched(_settings_repo(), enabled_count=1)
        with repo_patch, service_patch:
            resp = client.put(URL, json={})

        assert resp.status_code == 422
