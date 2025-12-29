"""In-memory cache for AppRole data with TTL support."""

import os
import asyncio
import logging
from typing import Dict, Optional, List, Any
from datetime import datetime, timedelta
from dataclasses import dataclass

from .models import AppRole, UserEffectivePermissions

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Cache entry with TTL tracking."""

    value: Any
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() >= self.expires_at


class AppRoleCache:
    """
    In-memory cache for AppRole data with TTL support.

    Cache invalidation occurs:
    - Automatically when TTL expires
    - Manually when admin updates roles (via invalidate methods)
    - On application restart (cache is not persistent)

    Cache Layers:
    - Layer 1: User Permissions Cache (per-user, 5 min TTL)
    - Layer 2: Role Cache (per-role, 10 min TTL)
    - Layer 3: JWT Mapping Cache (per-JWT-role, 10 min TTL)
    """

    def __init__(self):
        """Initialize cache with configurable TTLs from environment."""
        # Get TTLs from environment (in minutes)
        user_ttl_minutes = int(
            os.environ.get("APP_ROLE_USER_CACHE_TTL_MINUTES", "5")
        )
        role_ttl_minutes = int(
            os.environ.get("APP_ROLE_ROLE_CACHE_TTL_MINUTES", "10")
        )
        mapping_ttl_minutes = int(
            os.environ.get("APP_ROLE_MAPPING_CACHE_TTL_MINUTES", "10")
        )

        self.DEFAULT_USER_TTL = timedelta(minutes=user_ttl_minutes)
        self.DEFAULT_ROLE_TTL = timedelta(minutes=role_ttl_minutes)
        self.DEFAULT_MAPPING_TTL = timedelta(minutes=mapping_ttl_minutes)

        self._user_cache: Dict[str, CacheEntry] = {}
        self._role_cache: Dict[str, CacheEntry] = {}
        self._jwt_mapping_cache: Dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

        logger.info(
            f"AppRoleCache initialized with TTLs: "
            f"user={user_ttl_minutes}min, role={role_ttl_minutes}min, "
            f"mapping={mapping_ttl_minutes}min"
        )

    # =========================================================================
    # User Permissions Cache
    # =========================================================================

    async def get_user_permissions(
        self, user_id: str
    ) -> Optional[UserEffectivePermissions]:
        """Get cached user permissions."""
        entry = self._user_cache.get(f"user:{user_id}")
        if entry and not entry.is_expired:
            return entry.value
        return None

    async def set_user_permissions(
        self,
        user_id: str,
        permissions: UserEffectivePermissions,
        ttl: Optional[timedelta] = None,
    ):
        """Cache user permissions."""
        ttl = ttl or self.DEFAULT_USER_TTL
        self._user_cache[f"user:{user_id}"] = CacheEntry(
            value=permissions, expires_at=datetime.utcnow() + ttl
        )

    # =========================================================================
    # Role Cache
    # =========================================================================

    async def get_role(self, role_id: str) -> Optional[AppRole]:
        """Get cached role."""
        entry = self._role_cache.get(f"role:{role_id}")
        if entry and not entry.is_expired:
            return entry.value
        return None

    async def set_role(self, role: AppRole, ttl: Optional[timedelta] = None):
        """Cache role."""
        ttl = ttl or self.DEFAULT_ROLE_TTL
        self._role_cache[f"role:{role.role_id}"] = CacheEntry(
            value=role, expires_at=datetime.utcnow() + ttl
        )

    # =========================================================================
    # JWT Mapping Cache
    # =========================================================================

    async def get_jwt_mapping(self, jwt_role: str) -> Optional[List[str]]:
        """Get cached JWT role -> AppRole IDs mapping."""
        entry = self._jwt_mapping_cache.get(f"jwt:{jwt_role}")
        if entry and not entry.is_expired:
            return entry.value
        return None

    async def set_jwt_mapping(
        self, jwt_role: str, role_ids: List[str], ttl: Optional[timedelta] = None
    ):
        """Cache JWT role mapping."""
        ttl = ttl or self.DEFAULT_MAPPING_TTL
        self._jwt_mapping_cache[f"jwt:{jwt_role}"] = CacheEntry(
            value=role_ids, expires_at=datetime.utcnow() + ttl
        )

    # =========================================================================
    # Invalidation
    # =========================================================================

    async def invalidate_user(self, user_id: str):
        """Invalidate cache for a specific user."""
        key = f"user:{user_id}"
        if key in self._user_cache:
            del self._user_cache[key]
            logger.debug(f"Invalidated user cache: {user_id}")

    async def invalidate_role(self, role_id: str):
        """Invalidate cache for a specific role and all affected users."""
        async with self._lock:
            # Remove role cache
            role_key = f"role:{role_id}"
            if role_key in self._role_cache:
                del self._role_cache[role_key]

            # Clear all user caches (they may be affected)
            # In production, could be more targeted based on JWT mappings
            self._user_cache.clear()

            logger.info(
                f"Invalidated role cache: {role_id}, cleared all user caches"
            )

    async def invalidate_jwt_mapping(self, jwt_role: str):
        """Invalidate JWT mapping cache."""
        key = f"jwt:{jwt_role}"
        if key in self._jwt_mapping_cache:
            del self._jwt_mapping_cache[key]

        # Clear affected user caches
        self._user_cache.clear()
        logger.debug(f"Invalidated JWT mapping cache: {jwt_role}")

    async def invalidate_all(self):
        """Invalidate all caches (nuclear option)."""
        async with self._lock:
            self._user_cache.clear()
            self._role_cache.clear()
            self._jwt_mapping_cache.clear()
            logger.info("Invalidated all AppRole caches")

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> Dict:
        """Get cache statistics for monitoring."""
        now = datetime.utcnow()
        return {
            "userCacheSize": len(self._user_cache),
            "userCacheExpired": sum(
                1 for e in self._user_cache.values() if e.is_expired
            ),
            "roleCacheSize": len(self._role_cache),
            "roleCacheExpired": sum(
                1 for e in self._role_cache.values() if e.is_expired
            ),
            "jwtMappingCacheSize": len(self._jwt_mapping_cache),
            "jwtMappingCacheExpired": sum(
                1 for e in self._jwt_mapping_cache.values() if e.is_expired
            ),
        }

    async def cleanup_expired(self):
        """Remove expired entries from all caches."""
        async with self._lock:
            # Clean user cache
            expired_users = [
                k for k, v in self._user_cache.items() if v.is_expired
            ]
            for k in expired_users:
                del self._user_cache[k]

            # Clean role cache
            expired_roles = [
                k for k, v in self._role_cache.items() if v.is_expired
            ]
            for k in expired_roles:
                del self._role_cache[k]

            # Clean JWT mapping cache
            expired_mappings = [
                k for k, v in self._jwt_mapping_cache.items() if v.is_expired
            ]
            for k in expired_mappings:
                del self._jwt_mapping_cache[k]

            if expired_users or expired_roles or expired_mappings:
                logger.debug(
                    f"Cleaned up expired cache entries: "
                    f"users={len(expired_users)}, "
                    f"roles={len(expired_roles)}, "
                    f"mappings={len(expired_mappings)}"
                )


# Global cache instance (singleton)
_cache_instance: Optional[AppRoleCache] = None


def get_app_role_cache() -> AppRoleCache:
    """Get or create the global AppRoleCache instance."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = AppRoleCache()
    return _cache_instance
