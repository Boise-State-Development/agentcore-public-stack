"""Tests for AppRoleCache TTL and invalidation behaviour.

Uses the REAL AppRoleCache class (no mocks) with direct manipulation
of CacheEntry.expires_at to simulate time passage for TTL tests.

Requirements: 7.1–7.8
"""

from datetime import datetime, timedelta, timezone

import pytest

from apis.shared.rbac.cache import AppRoleCache
from apis.shared.rbac.models import (
    AppRole,
    EffectivePermissions,
    UserEffectivePermissions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_permissions(user_id: str = "u1") -> UserEffectivePermissions:
    """Create a minimal UserEffectivePermissions for testing."""
    return UserEffectivePermissions(
        user_id=user_id,
        app_roles=["role_a"],
        tools=["tool_1"],
        models=["model_1"],
        quota_tier="basic",
        resolved_at="2024-01-01T00:00:00Z",
    )


def _make_role(role_id: str = "editor") -> AppRole:
    """Create a minimal AppRole for testing."""
    return AppRole(
        role_id=role_id,
        display_name="Editor",
        description="Can edit",
        jwt_role_mappings=["Editor"],
        inherits_from=[],
        effective_permissions=EffectivePermissions(
            tools=["tool_1"], models=["model_1"], quota_tier="basic"
        ),
        granted_tools=["tool_1"],
        granted_models=["model_1"],
        priority=10,
        is_system_role=False,
        enabled=True,
    )


def _expire_all_entries(cache: AppRoleCache) -> None:
    """Force all cache entries to be expired by setting expires_at in the past."""
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    for entry in cache._user_cache.values():
        entry.expires_at = past
    for entry in cache._role_cache.values():
        entry.expires_at = past
    for entry in cache._jwt_mapping_cache.values():
        entry.expires_at = past


def _expire_entry(cache: AppRoleCache, layer: str, key: str) -> None:
    """Force a specific cache entry to be expired."""
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    store = getattr(cache, f"_{layer}_cache")
    if key in store:
        store[key].expires_at = past


@pytest.fixture
def cache() -> AppRoleCache:
    """Fresh AppRoleCache with default TTLs."""
    return AppRoleCache()


# ---------------------------------------------------------------------------
# 7.2 – Cache hit before TTL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_before_ttl(cache: AppRoleCache):
    """Cached user permissions are returned when TTL has not expired (Req 7.2)."""
    perms = _make_permissions("alice")
    await cache.set_user_permissions("alice", "fp", perms, ttl=timedelta(minutes=10))
    result = await cache.get_user_permissions("alice", "fp")
    assert result is not None
    assert result.user_id == "alice"
    assert result.tools == ["tool_1"]


@pytest.mark.asyncio
async def test_role_cache_hit_before_ttl(cache: AppRoleCache):
    """Cached role is returned when TTL has not expired (Req 7.2)."""
    role = _make_role("editor")
    await cache.set_role(role, ttl=timedelta(minutes=10))
    result = await cache.get_role("editor")
    assert result is not None
    assert result.role_id == "editor"


@pytest.mark.asyncio
async def test_jwt_mapping_cache_hit_before_ttl(cache: AppRoleCache):
    """Cached JWT mapping is returned when TTL has not expired (Req 7.2)."""
    await cache.set_jwt_mapping("Admin", ["admin_role"], ttl=timedelta(minutes=10))
    result = await cache.get_jwt_mapping("Admin")
    assert result == ["admin_role"]


# ---------------------------------------------------------------------------
# 7.3 – Cache miss after TTL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_miss_after_ttl(cache: AppRoleCache):
    """Expired user permissions return None (Req 7.3)."""
    perms = _make_permissions("bob")
    await cache.set_user_permissions("bob", "fp", perms, ttl=timedelta(minutes=10))
    # Simulate time passing beyond TTL
    _expire_entry(cache, "user", "user:bob:fp")
    result = await cache.get_user_permissions("bob", "fp")
    assert result is None


@pytest.mark.asyncio
async def test_role_cache_miss_after_ttl(cache: AppRoleCache):
    """Expired role returns None (Req 7.3)."""
    role = _make_role("viewer")
    await cache.set_role(role, ttl=timedelta(minutes=10))
    _expire_entry(cache, "role", "role:viewer")
    result = await cache.get_role("viewer")
    assert result is None


@pytest.mark.asyncio
async def test_jwt_mapping_cache_miss_after_ttl(cache: AppRoleCache):
    """Expired JWT mapping returns None (Req 7.3)."""
    await cache.set_jwt_mapping("Editor", ["editor_role"], ttl=timedelta(minutes=10))
    _expire_entry(cache, "jwt_mapping", "jwt:Editor")
    result = await cache.get_jwt_mapping("Editor")
    assert result is None


# ---------------------------------------------------------------------------
# 7.4 – invalidate_role clears role + user caches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalidate_role_clears_role_and_user_caches(cache: AppRoleCache):
    """invalidate_role removes the role entry and clears ALL user caches (Req 7.4)."""
    role = _make_role("editor")
    await cache.set_role(role, ttl=timedelta(minutes=10))
    await cache.set_user_permissions("alice", "fp", _make_permissions("alice"), ttl=timedelta(minutes=10))
    await cache.set_user_permissions("bob", "fp", _make_permissions("bob"), ttl=timedelta(minutes=10))
    # JWT mapping should NOT be affected
    await cache.set_jwt_mapping("Admin", ["admin_role"], ttl=timedelta(minutes=10))

    await cache.invalidate_role("editor")

    assert await cache.get_role("editor") is None
    assert await cache.get_user_permissions("alice", "fp") is None
    assert await cache.get_user_permissions("bob", "fp") is None
    # JWT mapping untouched
    assert await cache.get_jwt_mapping("Admin") == ["admin_role"]


# ---------------------------------------------------------------------------
# 7.5 – invalidate_jwt_mapping clears mapping + user caches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalidate_jwt_mapping_clears_mapping_and_user_caches(cache: AppRoleCache):
    """invalidate_jwt_mapping removes the mapping and clears ALL user caches (Req 7.5)."""
    await cache.set_jwt_mapping("Editor", ["editor_role"], ttl=timedelta(minutes=10))
    await cache.set_user_permissions("alice", "fp", _make_permissions("alice"), ttl=timedelta(minutes=10))
    # Role cache should NOT be affected
    role = _make_role("admin")
    await cache.set_role(role, ttl=timedelta(minutes=10))

    await cache.invalidate_jwt_mapping("Editor")

    assert await cache.get_jwt_mapping("Editor") is None
    assert await cache.get_user_permissions("alice", "fp") is None
    # Role cache untouched
    assert await cache.get_role("admin") is not None


# ---------------------------------------------------------------------------
# 7.6 – invalidate_all clears everything
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalidate_all_clears_everything(cache: AppRoleCache):
    """invalidate_all clears user, role, and JWT mapping caches (Req 7.6)."""
    await cache.set_user_permissions("alice", "fp", _make_permissions("alice"), ttl=timedelta(minutes=10))
    await cache.set_role(_make_role("editor"), ttl=timedelta(minutes=10))
    await cache.set_jwt_mapping("Admin", ["admin_role"], ttl=timedelta(minutes=10))

    await cache.invalidate_all()

    assert await cache.get_user_permissions("alice", "fp") is None
    assert await cache.get_role("editor") is None
    assert await cache.get_jwt_mapping("Admin") is None


# ---------------------------------------------------------------------------
# 7.7 – cleanup_expired removes only expired entries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cleanup_expired_removes_only_expired(cache: AppRoleCache):
    """cleanup_expired removes expired entries but keeps valid ones (Req 7.7)."""
    # Valid entries (long TTL)
    await cache.set_user_permissions("alice", "fp", _make_permissions("alice"), ttl=timedelta(minutes=10))
    await cache.set_role(_make_role("editor"), ttl=timedelta(minutes=10))
    await cache.set_jwt_mapping("Admin", ["admin_role"], ttl=timedelta(minutes=10))

    # Entries that will be expired
    await cache.set_user_permissions("expired_user", "fp", _make_permissions("expired_user"), ttl=timedelta(minutes=10))
    await cache.set_role(_make_role("expired_role"), ttl=timedelta(minutes=10))
    await cache.set_jwt_mapping("ExpiredMapping", ["x"], ttl=timedelta(minutes=10))

    # Force-expire only the second batch
    _expire_entry(cache, "user", "user:expired_user:fp")
    _expire_entry(cache, "role", "role:expired_role")
    _expire_entry(cache, "jwt_mapping", "jwt:ExpiredMapping")

    await cache.cleanup_expired()

    # Valid entries still present
    assert await cache.get_user_permissions("alice", "fp") is not None
    assert await cache.get_role("editor") is not None
    assert await cache.get_jwt_mapping("Admin") == ["admin_role"]

    # Expired entries actually removed from internal dicts
    stats = cache.get_stats()
    assert stats["userCacheSize"] == 1
    assert stats["roleCacheSize"] == 1
    assert stats["jwtMappingCacheSize"] == 1


# ---------------------------------------------------------------------------
# 7.8 – get_stats accuracy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_stats_accuracy(cache: AppRoleCache):
    """get_stats returns accurate total and expired counts per layer (Req 7.8)."""
    # Add 2 user entries: 1 valid, 1 will be expired
    await cache.set_user_permissions("alice", "fp", _make_permissions("alice"), ttl=timedelta(minutes=10))
    await cache.set_user_permissions("bob", "fp", _make_permissions("bob"), ttl=timedelta(minutes=10))
    _expire_entry(cache, "user", "user:bob:fp")

    # Add 2 role entries: 1 valid, 1 will be expired
    await cache.set_role(_make_role("editor"), ttl=timedelta(minutes=10))
    await cache.set_role(_make_role("viewer"), ttl=timedelta(minutes=10))
    _expire_entry(cache, "role", "role:viewer")

    # Add 2 JWT mapping entries: 1 valid, 1 will be expired
    await cache.set_jwt_mapping("Admin", ["admin"], ttl=timedelta(minutes=10))
    await cache.set_jwt_mapping("Guest", ["guest"], ttl=timedelta(minutes=10))
    _expire_entry(cache, "jwt_mapping", "jwt:Guest")

    stats = cache.get_stats()

    assert stats["userCacheSize"] == 2
    assert stats["userCacheExpired"] == 1
    assert stats["roleCacheSize"] == 2
    assert stats["roleCacheExpired"] == 1
    assert stats["jwtMappingCacheSize"] == 2
    assert stats["jwtMappingCacheExpired"] == 1


@pytest.mark.asyncio
async def test_get_stats_empty_cache(cache: AppRoleCache):
    """get_stats returns zeros for an empty cache (Req 7.8)."""
    stats = cache.get_stats()
    assert stats["userCacheSize"] == 0
    assert stats["userCacheExpired"] == 0
    assert stats["roleCacheSize"] == 0
    assert stats["roleCacheExpired"] == 0
    assert stats["jwtMappingCacheSize"] == 0
    assert stats["jwtMappingCacheExpired"] == 0
