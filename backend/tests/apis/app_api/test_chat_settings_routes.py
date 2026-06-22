"""Route tests for the chat-mode policy endpoints.

Covers the admin surface (``GET/PUT /admin/settings/chat``) and the
user-facing SPA read (``GET /system/chat-settings``).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth import require_admin
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.platform_settings.models import ChatModeSettings
from apis.shared.platform_settings.service import ChatModeSettingsService

from apis.app_api.admin.settings import routes as admin_settings_routes
from apis.app_api.system import routes as system_routes


def _admin() -> User:
    return User(
        user_id="admin-1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        raw_token="test-token",
    )


def _user() -> User:
    return User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        roles=["default"],
        raw_token="test-token",
    )


class _InMemoryRepo:
    """Duck-typed stand-in for PlatformSettingsRepository."""

    def __init__(self, stored: ChatModeSettings | None = None):
        self.stored = stored

    @property
    def enabled(self) -> bool:
        return True

    async def get_chat_mode_settings(self):
        return self.stored

    async def put_chat_mode_settings(self, settings) -> None:
        self.stored = settings


@pytest.fixture
def repo() -> _InMemoryRepo:
    return _InMemoryRepo()


@pytest.fixture
def client(repo: _InMemoryRepo, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # These tests exercise the policy surface as it behaves with the skills
    # feature enabled; the disabled-feature override is covered separately.
    monkeypatch.setenv("SKILLS_ENABLED", "true")
    service = ChatModeSettingsService(repository=repo, cache_ttl_seconds=0.0)
    monkeypatch.setattr(
        admin_settings_routes, "get_chat_mode_settings_service", lambda: service
    )
    monkeypatch.setattr(
        system_routes, "get_chat_mode_settings_service", lambda: service
    )

    app = FastAPI()
    app.include_router(admin_settings_routes.router, prefix="/admin")
    app.include_router(system_routes.router)
    app.dependency_overrides[require_admin] = _admin
    app.dependency_overrides[get_current_user_from_session] = _user
    return TestClient(app)


class TestAdminChatSettings:
    def test_get_returns_defaults_when_unconfigured(self, client: TestClient):
        response = client.get("/admin/settings/chat")
        assert response.status_code == 200
        body = response.json()
        assert body["defaultMode"] == "skill"
        assert body["allowModeToggle"] is True
        assert body["updatedBy"] is None

    def test_put_persists_and_stamps_audit_fields(
        self, client: TestClient, repo: _InMemoryRepo
    ):
        response = client.put(
            "/admin/settings/chat",
            json={"defaultMode": "chat", "allowModeToggle": False},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["defaultMode"] == "chat"
        assert body["allowModeToggle"] is False
        assert body["updatedBy"] == "admin@example.com"
        assert body["updatedAt"] is not None

        assert repo.stored is not None
        assert repo.stored.default_mode == "chat"

        # Subsequent GET reflects the stored policy
        follow_up = client.get("/admin/settings/chat")
        assert follow_up.json()["defaultMode"] == "chat"

    def test_put_rejects_invalid_mode(self, client: TestClient):
        response = client.put(
            "/admin/settings/chat",
            json={"defaultMode": "voice", "allowModeToggle": True},
        )
        assert response.status_code == 422


class TestSystemChatSettings:
    def test_returns_policy_flags_only(self, client: TestClient):
        client.put(
            "/admin/settings/chat",
            json={"defaultMode": "chat", "allowModeToggle": False},
        )

        response = client.get("/system/chat-settings")
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "defaultMode": "chat",
            "allowModeToggle": False,
            "skillsEnabled": True,
        }

    def test_defaults_when_unconfigured(self, client: TestClient):
        response = client.get("/system/chat-settings")
        assert response.status_code == 200
        assert response.json() == {
            "defaultMode": "skill",
            "allowModeToggle": True,
            "skillsEnabled": True,
        }


class TestSystemChatSettingsSkillsDisabled:
    """When SKILLS_ENABLED is off, the public read ignores any stored policy
    and forces tools/chat mode so the SPA hides the skills surfaces."""

    @pytest.fixture
    def client_off(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        monkeypatch.setenv("SKILLS_ENABLED", "false")
        # A stored policy that *would* enable skills — the flag must win.
        repo = _InMemoryRepo(
            ChatModeSettings(default_mode="skill", allow_mode_toggle=True)
        )
        service = ChatModeSettingsService(repository=repo, cache_ttl_seconds=0.0)
        monkeypatch.setattr(
            system_routes, "get_chat_mode_settings_service", lambda: service
        )
        app = FastAPI()
        app.include_router(system_routes.router)
        app.dependency_overrides[get_current_user_from_session] = _user
        return TestClient(app)

    def test_forces_chat_and_reports_disabled(self, client_off: TestClient):
        response = client_off.get("/system/chat-settings")
        assert response.status_code == 200
        assert response.json() == {
            "defaultMode": "chat",
            "allowModeToggle": False,
            "skillsEnabled": False,
        }
