"""Shared fixtures for RBAC test suite.

Provides:
- make_app_role() factory for creating AppRole objects
- Mock AppRoleRepository
- Mock AppRoleCache
"""

from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.shared.rbac.cache import AppRoleCache
from apis.shared.rbac.models import AppRole, EffectivePermissions


# ---------------------------------------------------------------------------
# AppRole factory
# ---------------------------------------------------------------------------

@pytest.fixture
def make_app_role():
    """Factory fixture that creates AppRole objects with sensible defaults."""

    def _make_app_role(
        role_id: str = "test_role",
        display_name: str = "Test Role",
        description: str = "A test role",
        jwt_role_mappings: Optional[List[str]] = None,
        inherits_from: Optional[List[str]] = None,
        granted_tools: Optional[List[str]] = None,
        granted_models: Optional[List[str]] = None,
        granted_skills: Optional[List[str]] = None,
        tools: Optional[List[str]] = None,
        models: Optional[List[str]] = None,
        skills: Optional[List[str]] = None,
        quota_tier: Optional[str] = None,
        priority: int = 0,
        is_system_role: bool = False,
        enabled: bool = True,
        **kwargs: Any,
    ) -> AppRole:
        effective_tools = tools if tools is not None else (granted_tools or [])
        effective_models = models if models is not None else (granted_models or [])
        effective_skills = skills if skills is not None else (granted_skills or [])

        return AppRole(
            role_id=role_id,
            display_name=display_name,
            description=description,
            jwt_role_mappings=jwt_role_mappings if jwt_role_mappings is not None else [],
            inherits_from=inherits_from if inherits_from is not None else [],
            effective_permissions=EffectivePermissions(
                tools=effective_tools,
                models=effective_models,
                skills=effective_skills,
                quota_tier=quota_tier,
            ),
            granted_tools=granted_tools if granted_tools is not None else [],
            granted_models=granted_models if granted_models is not None else [],
            granted_skills=granted_skills if granted_skills is not None else [],
            priority=priority,
            is_system_role=is_system_role,
            enabled=enabled,
            **kwargs,
        )

    return _make_app_role


# ---------------------------------------------------------------------------
# Mock AppRoleRepository
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_app_role_repo():
    """Mock AppRoleRepository with all async methods stubbed."""
    repo = AsyncMock()
    repo.get_role = AsyncMock(return_value=None)
    repo.list_roles = AsyncMock(return_value=[])
    repo.create_role = AsyncMock()
    repo.update_role = AsyncMock()
    repo.delete_role = AsyncMock(return_value=True)
    repo.get_roles_for_jwt_role = AsyncMock(return_value=[])
    repo.role_exists = AsyncMock(return_value=False)
    return repo


# ---------------------------------------------------------------------------
# Mock AppRoleCache
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_app_role_cache():
    """Mock AppRoleCache with all async methods stubbed.

    Returns cache misses by default so tests can configure hits as needed.
    """
    cache = AsyncMock(spec=AppRoleCache)
    cache.get_user_permissions = AsyncMock(return_value=None)
    cache.set_user_permissions = AsyncMock()
    cache.get_role = AsyncMock(return_value=None)
    cache.set_role = AsyncMock()
    cache.get_jwt_mapping = AsyncMock(return_value=None)
    cache.set_jwt_mapping = AsyncMock()
    cache.invalidate_user = AsyncMock()
    cache.invalidate_role = AsyncMock()
    cache.invalidate_jwt_mapping = AsyncMock()
    cache.invalidate_all = AsyncMock()
    cache.cleanup_expired = AsyncMock()
    cache.get_stats = MagicMock(return_value={
        "userCacheSize": 0,
        "userCacheExpired": 0,
        "roleCacheSize": 0,
        "roleCacheExpired": 0,
        "jwtMappingCacheSize": 0,
        "jwtMappingCacheExpired": 0,
    })
    return cache
