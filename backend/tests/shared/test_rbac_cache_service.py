"""RBAC cache + service tests (pure logic, no AWS)."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock


class TestAppRoleCache:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from apis.shared.rbac.cache import AppRoleCache
        self.cache = AppRoleCache()

    @pytest.mark.asyncio
    async def test_user_permissions_set_get(self):
        perms = MagicMock()
        await self.cache.set_user_permissions("u1", "fp", perms)
        assert await self.cache.get_user_permissions("u1", "fp") is perms

    @pytest.mark.asyncio
    async def test_user_permissions_miss(self):
        assert await self.cache.get_user_permissions("nope", "fp") is None

    @pytest.mark.asyncio
    async def test_user_permissions_expired(self):
        perms = MagicMock()
        await self.cache.set_user_permissions("u1", "fp", perms, ttl=timedelta(seconds=-1))
        assert await self.cache.get_user_permissions("u1", "fp") is None

    @pytest.mark.asyncio
    async def test_role_set_get(self):
        role = MagicMock()
        role.role_id = "admin"
        await self.cache.set_role(role)
        assert await self.cache.get_role("admin") is role

    @pytest.mark.asyncio
    async def test_role_miss(self):
        assert await self.cache.get_role("nope") is None

    @pytest.mark.asyncio
    async def test_role_expired(self):
        role = MagicMock()
        role.role_id = "admin"
        await self.cache.set_role(role, ttl=timedelta(seconds=-1))
        assert await self.cache.get_role("admin") is None

    @pytest.mark.asyncio
    async def test_jwt_mapping_set_get(self):
        await self.cache.set_jwt_mapping("viewer", ["r1", "r2"])
        assert await self.cache.get_jwt_mapping("viewer") == ["r1", "r2"]

    @pytest.mark.asyncio
    async def test_jwt_mapping_miss(self):
        assert await self.cache.get_jwt_mapping("nope") is None

    @pytest.mark.asyncio
    async def test_jwt_mapping_expired(self):
        await self.cache.set_jwt_mapping("viewer", ["r1"], ttl=timedelta(seconds=-1))
        assert await self.cache.get_jwt_mapping("viewer") is None

    @pytest.mark.asyncio
    async def test_invalidate_user(self):
        perms = MagicMock()
        await self.cache.set_user_permissions("u1", "fp", perms)
        await self.cache.invalidate_user("u1")
        assert await self.cache.get_user_permissions("u1", "fp") is None

    @pytest.mark.asyncio
    async def test_invalidate_user_nonexistent(self):
        await self.cache.invalidate_user("nope")  # no error

    @pytest.mark.asyncio
    async def test_invalidate_role(self):
        role = MagicMock(); role.role_id = "admin"
        perms = MagicMock()
        await self.cache.set_role(role)
        await self.cache.set_user_permissions("u1", "fp", perms)
        await self.cache.invalidate_role("admin")
        assert await self.cache.get_role("admin") is None
        assert await self.cache.get_user_permissions("u1", "fp") is None  # user cache cleared

    @pytest.mark.asyncio
    async def test_invalidate_jwt_mapping(self):
        await self.cache.set_jwt_mapping("viewer", ["r1"])
        perms = MagicMock()
        await self.cache.set_user_permissions("u1", "fp", perms)
        await self.cache.invalidate_jwt_mapping("viewer")
        assert await self.cache.get_jwt_mapping("viewer") is None
        assert await self.cache.get_user_permissions("u1", "fp") is None

    @pytest.mark.asyncio
    async def test_invalidate_all(self):
        role = MagicMock(); role.role_id = "admin"
        await self.cache.set_user_permissions("u1", "fp", MagicMock())
        await self.cache.set_role(role)
        await self.cache.set_jwt_mapping("v", ["r1"])
        await self.cache.invalidate_all()
        assert await self.cache.get_user_permissions("u1", "fp") is None
        assert await self.cache.get_role("admin") is None
        assert await self.cache.get_jwt_mapping("v") is None

    def test_get_stats(self):
        stats = self.cache.get_stats()
        assert "userCacheSize" in stats
        assert stats["userCacheSize"] == 0

    @pytest.mark.asyncio
    async def test_cleanup_expired(self):
        role = MagicMock(); role.role_id = "old"
        await self.cache.set_role(role, ttl=timedelta(seconds=-1))
        await self.cache.set_jwt_mapping("old", ["r1"], ttl=timedelta(seconds=-1))
        await self.cache.set_user_permissions("old", "fp", MagicMock(), ttl=timedelta(seconds=-1))
        await self.cache.cleanup_expired()
        assert self.cache.get_stats()["roleCacheSize"] == 0
        assert self.cache.get_stats()["jwtMappingCacheSize"] == 0
        assert self.cache.get_stats()["userCacheSize"] == 0

    def test_get_stats_with_expired(self):
        from apis.shared.rbac.cache import CacheEntry
        self.cache._role_cache["role:old"] = CacheEntry(
            value=MagicMock(), expires_at=datetime.now(timezone.utc) - timedelta(seconds=10)
        )
        stats = self.cache.get_stats()
        assert stats["roleCacheExpired"] == 1


class TestCacheEntry:
    def test_is_expired_false(self):
        from apis.shared.rbac.cache import CacheEntry
        e = CacheEntry(value="x", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        assert e.is_expired is False

    def test_is_expired_true(self):
        from apis.shared.rbac.cache import CacheEntry
        e = CacheEntry(value="x", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        assert e.is_expired is True


class TestAppRoleService:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from apis.shared.rbac.service import AppRoleService
        from apis.shared.rbac.cache import AppRoleCache
        self.repo = AsyncMock()
        self.cache = AppRoleCache()
        self.svc = AppRoleService(repository=self.repo, cache=self.cache)

    def _make_user(self, user_id="u1", email="a@b.com", roles=None):
        u = MagicMock()
        u.user_id = user_id
        u.email = email
        u.roles = roles or ["viewer"]
        return u

    def _make_role(self, role_id="r1", enabled=True, tools=None, models=None, priority=10, quota_tier=None):
        from apis.shared.rbac.models import AppRole, EffectivePermissions
        return AppRole(
            role_id=role_id, display_name=role_id, description="test",
            jwt_role_mappings=["viewer"], priority=priority, enabled=enabled,
            effective_permissions=EffectivePermissions(
                tools=tools or ["*"], models=models or ["*"], quota_tier=quota_tier,
            ),
        )

    @pytest.mark.asyncio
    async def test_resolve_user_permissions_cache_hit(self):
        from apis.shared.rbac.models import UserEffectivePermissions
        perms = UserEffectivePermissions(
            user_id="u1", app_roles=["r1"], tools=["*"], models=["*"],
            resolved_at="now", quota_tier=None,
        )
        from apis.shared.rbac.cache import roles_fingerprint
        user = self._make_user()
        # Seed under the same role-set key the service will compute.
        await self.cache.set_user_permissions(
            "u1", roles_fingerprint(user.roles), perms
        )
        result = await self.svc.resolve_user_permissions(user)
        assert result is perms
        self.repo.get_roles_for_jwt_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_user_permissions_not_shared_across_role_sets(self):
        """Same user_id, different roles → no cache crossover.

        Regression guard for the API-key path, which builds a User for the
        same subject as the cookie session; a shared key let one poison the
        other's grants for the whole TTL.
        """
        from apis.shared.rbac.cache import roles_fingerprint
        from apis.shared.rbac.models import UserEffectivePermissions
        stale = UserEffectivePermissions(
            user_id="u1", app_roles=["default"], tools=[], models=[],
            resolved_at="now", quota_tier=None,
        )
        placeholder = self._make_user(roles=["user"])
        await self.cache.set_user_permissions(
            "u1", roles_fingerprint(placeholder.roles), stale
        )

        self.repo.get_roles_for_jwt_role.return_value = ["r1"]
        self.repo.get_role.return_value = self._make_role()
        real = self._make_user(roles=["Staff"])
        result = await self.svc.resolve_user_permissions(real)

        assert result is not stale
        assert "*" in result.models

    @pytest.mark.asyncio
    async def test_resolve_user_permissions_from_db(self):
        self.repo.get_roles_for_jwt_role.return_value = ["r1"]
        self.repo.get_role.return_value = self._make_role()
        result = await self.svc.resolve_user_permissions(self._make_user())
        assert "r1" in result.app_roles
        assert "*" in result.tools

    @pytest.mark.asyncio
    async def test_resolve_user_permissions_default_role(self):
        self.repo.get_roles_for_jwt_role.return_value = []
        default = self._make_role(role_id="default", tools=["basic"])
        self.repo.get_role.return_value = default
        result = await self.svc.resolve_user_permissions(self._make_user())
        assert "default" in result.app_roles

    @pytest.mark.asyncio
    async def test_resolve_no_roles_no_default(self):
        self.repo.get_roles_for_jwt_role.return_value = []
        self.repo.get_role.return_value = None
        result = await self.svc.resolve_user_permissions(self._make_user())
        assert result.app_roles == []
        assert result.tools == []

    @pytest.mark.asyncio
    async def test_merge_permissions_union(self):
        r1 = self._make_role(role_id="r1", tools=["t1"], models=["m1"], priority=5)
        r2 = self._make_role(role_id="r2", tools=["t2"], models=["m2"], priority=10, quota_tier="pro")
        perms = self.svc._merge_permissions("u1", [r1, r2])
        assert set(perms.tools) == {"t1", "t2"}
        assert set(perms.models) == {"m1", "m2"}
        assert perms.quota_tier == "pro"

    @pytest.mark.asyncio
    async def test_merge_permissions_wildcard(self):
        r1 = self._make_role(role_id="r1", tools=["*"], models=["m1"])
        perms = self.svc._merge_permissions("u1", [r1])
        assert "*" in perms.tools

    @pytest.mark.asyncio
    async def test_merge_permissions_empty(self):
        perms = self.svc._merge_permissions("u1", [])
        assert perms.tools == []
        assert perms.quota_tier is None

    @pytest.mark.asyncio
    async def test_can_access_tool_wildcard(self):
        self.repo.get_roles_for_jwt_role.return_value = ["r1"]
        self.repo.get_role.return_value = self._make_role(tools=["*"])
        assert await self.svc.can_access_tool(self._make_user(), "anything") is True

    @pytest.mark.asyncio
    async def test_can_access_tool_specific(self):
        self.repo.get_roles_for_jwt_role.return_value = ["r1"]
        self.repo.get_role.return_value = self._make_role(tools=["code_interpreter"])
        assert await self.svc.can_access_tool(self._make_user(), "code_interpreter") is True
        # Clear cache for next assertion
        await self.cache.invalidate_all()
        self.repo.get_role.return_value = self._make_role(tools=["code_interpreter"])
        self.repo.get_roles_for_jwt_role.return_value = ["r1"]
        assert await self.svc.can_access_tool(self._make_user(), "other") is False

    @pytest.mark.asyncio
    async def test_can_access_model(self):
        self.repo.get_roles_for_jwt_role.return_value = ["r1"]
        self.repo.get_role.return_value = self._make_role(models=["claude"])
        assert await self.svc.can_access_model(self._make_user(), "claude") is True

    @pytest.mark.asyncio
    async def test_get_accessible_tools(self):
        self.repo.get_roles_for_jwt_role.return_value = ["r1"]
        self.repo.get_role.return_value = self._make_role(tools=["t1", "t2"])
        tools = await self.svc.get_accessible_tools(self._make_user())
        assert set(tools) == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_get_accessible_models(self):
        self.repo.get_roles_for_jwt_role.return_value = ["r1"]
        self.repo.get_role.return_value = self._make_role(models=["m1"])
        models = await self.svc.get_accessible_models(self._make_user())
        assert "m1" in models

    @pytest.mark.asyncio
    async def test_get_user_quota_tier(self):
        self.repo.get_roles_for_jwt_role.return_value = ["r1"]
        self.repo.get_role.return_value = self._make_role(quota_tier="enterprise")
        tier = await self.svc.get_user_quota_tier(self._make_user())
        assert tier == "enterprise"
