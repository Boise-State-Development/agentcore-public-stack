"""Fixtures for the audit suite.

Deliberately a small local factory rather than a copy of `tests/rbac/conftest.py`
or an import across test packages: these tests care about *what got recorded*,
so they need only enough of an AppRole to mutate. A verbatim copy of the RBAC
factory would drift the moment either side gained a field.
"""

from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.shared.rbac.cache import AppRoleCache
from apis.shared.rbac.models import AppRole, EffectivePermissions


@pytest.fixture
def make_app_role():
    def _make(
        role_id: str = "test_role",
        display_name: str = "Test Role",
        description: str = "",
        jwt_role_mappings: Optional[List[str]] = None,
        inherits_from: Optional[List[str]] = None,
        granted_tools: Optional[List[str]] = None,
        granted_models: Optional[List[str]] = None,
        granted_skills: Optional[List[str]] = None,
        granted_admin_scopes: Optional[List[str]] = None,
        priority: int = 0,
        is_system_role: bool = False,
        enabled: bool = True,
        **kwargs: Any,
    ) -> AppRole:
        return AppRole(
            role_id=role_id,
            display_name=display_name,
            description=description,
            jwt_role_mappings=jwt_role_mappings or [],
            inherits_from=inherits_from or [],
            effective_permissions=EffectivePermissions(
                tools=granted_tools or [],
                models=granted_models or [],
                skills=granted_skills or [],
                admin_scopes=granted_admin_scopes or [],
            ),
            granted_tools=granted_tools or [],
            granted_models=granted_models or [],
            granted_skills=granted_skills or [],
            granted_admin_scopes=granted_admin_scopes or [],
            priority=priority,
            is_system_role=is_system_role,
            enabled=enabled,
            **kwargs,
        )

    return _make


@pytest.fixture
def mock_app_role_repo():
    repo = AsyncMock()
    repo.get_role = AsyncMock(return_value=None)
    repo.list_roles = AsyncMock(return_value=[])
    repo.create_role = AsyncMock()
    repo.update_role = AsyncMock()
    repo.delete_role = AsyncMock(return_value=True)
    repo.get_roles_for_jwt_role = AsyncMock(return_value=[])
    repo.role_exists = AsyncMock(return_value=False)
    return repo


@pytest.fixture
def mock_app_role_cache():
    cache = AsyncMock(spec=AppRoleCache)
    cache.get_role = AsyncMock(return_value=None)
    cache.set_role = AsyncMock()
    cache.invalidate_role = AsyncMock()
    cache.invalidate_jwt_mapping = AsyncMock()
    cache.invalidate_user = AsyncMock()
    cache.invalidate_all = AsyncMock()
    cache.get_stats = MagicMock(return_value={})
    return cache
