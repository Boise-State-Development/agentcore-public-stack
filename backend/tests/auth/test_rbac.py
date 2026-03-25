"""Tests for AppRole-based RBAC utilities.

Covers require_app_roles, require_admin, and fail-closed behavior.

Requirements: 4.1–4.12
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import HTTPException

from apis.shared.auth.rbac import require_app_roles, require_admin
from apis.shared.rbac.models import UserEffectivePermissions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_permissions(app_roles: list[str]) -> UserEffectivePermissions:
    """Build a UserEffectivePermissions with the given AppRoles."""
    return UserEffectivePermissions(
        user_id="user-001",
        app_roles=app_roles,
        tools=["*"] if "system_admin" in app_roles else [],
        models=["*"] if "system_admin" in app_roles else [],
        quota_tier=None,
        resolved_at="2025-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# require_app_roles
# ---------------------------------------------------------------------------


class TestRequireAppRoles:
    """Tests for require_app_roles() — AppRole-based OR-logic dependency."""

    @pytest.fixture(autouse=True)
    def _patch_service(self, monkeypatch):
        self.mock_service = MagicMock()
        self.mock_service.resolve_user_permissions = AsyncMock()
        import apis.shared.rbac.service as svc_mod
        monkeypatch.setattr(svc_mod, "_service_instance", self.mock_service)

    @pytest.mark.asyncio
    async def test_matching_app_role_grants_access(self, make_user):
        """User with a matching AppRole is granted access."""
        self.mock_service.resolve_user_permissions.return_value = _mock_permissions(["editor"])
        checker = require_app_roles("editor", "admin")
        user = make_user(roles=["some_jwt_group"])
        result = await checker(user=user)
        assert result is user

    @pytest.mark.asyncio
    async def test_no_matching_app_role_denied(self, make_user):
        """User without any matching AppRole gets 403."""
        self.mock_service.resolve_user_permissions.return_value = _mock_permissions(["default"])
        checker = require_app_roles("editor", "admin")
        user = make_user(roles=["some_jwt_group"])
        with pytest.raises(HTTPException) as exc_info:
            await checker(user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_service_failure_denies_access(self, make_user):
        """If AppRoleService raises, access is denied (fail-closed)."""
        self.mock_service.resolve_user_permissions.side_effect = RuntimeError("DB down")
        checker = require_app_roles("editor")
        user = make_user(roles=["some_jwt_group"])
        with pytest.raises(HTTPException) as exc_info:
            await checker(user=user)
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# require_admin (predefined checker)
# ---------------------------------------------------------------------------


class TestRequireAdmin:
    """Tests for the predefined require_admin checker."""

    @pytest.fixture(autouse=True)
    def _patch_service(self, monkeypatch):
        self.mock_service = MagicMock()
        self.mock_service.resolve_user_permissions = AsyncMock()
        import apis.shared.rbac.service as svc_mod
        monkeypatch.setattr(svc_mod, "_service_instance", self.mock_service)

    @pytest.mark.asyncio
    async def test_system_admin_granted(self, make_user):
        """User whose JWT maps to system_admin AppRole passes."""
        self.mock_service.resolve_user_permissions.return_value = _mock_permissions(["system_admin"])
        user = make_user(roles=["system_admin"])
        result = await require_admin(user=user)
        assert result is user

    @pytest.mark.asyncio
    async def test_non_admin_denied(self, make_user):
        """User without system_admin AppRole is denied."""
        self.mock_service.resolve_user_permissions.return_value = _mock_permissions(["default"])
        user = make_user(roles=["Viewer"])
        with pytest.raises(HTTPException) as exc_info:
            await require_admin(user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_roles_denied(self, make_user):
        """User with no resolved AppRoles is denied."""
        self.mock_service.resolve_user_permissions.return_value = _mock_permissions([])
        user = make_user(roles=[])
        with pytest.raises(HTTPException) as exc_info:
            await require_admin(user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_service_failure_denies(self, make_user):
        """If AppRoleService raises, access is denied (fail-closed)."""
        self.mock_service.resolve_user_permissions.side_effect = RuntimeError("DB down")
        user = make_user(roles=["system_admin"])
        with pytest.raises(HTTPException) as exc_info:
            await require_admin(user=user)
        assert exc_info.value.status_code == 403
