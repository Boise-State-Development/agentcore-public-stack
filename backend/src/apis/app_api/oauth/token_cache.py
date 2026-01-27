"""In-memory cache for decrypted OAuth tokens.

Uses TTLCache to avoid repeated KMS decrypt calls for frequently accessed tokens.
Cache entries expire after 5 minutes by default.
"""

import logging
from typing import Optional

from cachetools import TTLCache

logger = logging.getLogger(__name__)

# Default cache TTL in seconds (5 minutes)
DEFAULT_CACHE_TTL = 300

# Default cache size (number of tokens to cache)
DEFAULT_CACHE_SIZE = 1000


class TokenCache:
    """
    TTL-based cache for decrypted OAuth access tokens.

    Reduces KMS API calls by caching decrypted tokens in memory.
    Tokens are automatically evicted after the TTL expires.

    Thread-safe through cachetools' internal locking.
    """

    def __init__(
        self,
        maxsize: int = DEFAULT_CACHE_SIZE,
        ttl: int = DEFAULT_CACHE_TTL,
    ):
        """
        Initialize the token cache.

        Args:
            maxsize: Maximum number of tokens to cache
            ttl: Time-to-live in seconds for cache entries
        """
        self._cache: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)
        self._ttl = ttl
        self._maxsize = maxsize

        logger.info(f"Initialized token cache: maxsize={maxsize}, ttl={ttl}s")

    def _make_key(self, user_id: str, provider_id: str) -> str:
        """Create a cache key from user and provider IDs."""
        return f"{user_id}:{provider_id}"

    def get(self, user_id: str, provider_id: str) -> Optional[str]:
        """
        Get a cached access token.

        Args:
            user_id: User identifier
            provider_id: OAuth provider identifier

        Returns:
            Decrypted access token if cached, None otherwise
        """
        key = self._make_key(user_id, provider_id)
        token = self._cache.get(key)
        if token:
            logger.debug(f"Token cache hit: user={user_id}, provider={provider_id}")
        else:
            logger.debug(f"Token cache miss: user={user_id}, provider={provider_id}")
        return token

    def set(self, user_id: str, provider_id: str, access_token: str) -> None:
        """
        Cache a decrypted access token.

        Args:
            user_id: User identifier
            provider_id: OAuth provider identifier
            access_token: Decrypted access token to cache
        """
        key = self._make_key(user_id, provider_id)
        self._cache[key] = access_token
        logger.debug(f"Cached token: user={user_id}, provider={provider_id}")

    def delete(self, user_id: str, provider_id: str) -> bool:
        """
        Remove a token from cache.

        Args:
            user_id: User identifier
            provider_id: OAuth provider identifier

        Returns:
            True if token was in cache, False otherwise
        """
        key = self._make_key(user_id, provider_id)
        if key in self._cache:
            del self._cache[key]
            logger.debug(f"Evicted token from cache: user={user_id}, provider={provider_id}")
            return True
        return False

    def delete_for_user(self, user_id: str) -> int:
        """
        Remove all cached tokens for a user.

        Args:
            user_id: User identifier

        Returns:
            Number of tokens removed
        """
        prefix = f"{user_id}:"
        keys_to_delete = [k for k in self._cache.keys() if k.startswith(prefix)]
        for key in keys_to_delete:
            del self._cache[key]
        if keys_to_delete:
            logger.debug(f"Evicted {len(keys_to_delete)} tokens for user: {user_id}")
        return len(keys_to_delete)

    def delete_for_provider(self, provider_id: str) -> int:
        """
        Remove all cached tokens for a provider.

        Useful when provider configuration changes (e.g., scopes updated).

        Args:
            provider_id: OAuth provider identifier

        Returns:
            Number of tokens removed
        """
        suffix = f":{provider_id}"
        keys_to_delete = [k for k in self._cache.keys() if k.endswith(suffix)]
        for key in keys_to_delete:
            del self._cache[key]
        if keys_to_delete:
            logger.debug(f"Evicted {len(keys_to_delete)} tokens for provider: {provider_id}")
        return len(keys_to_delete)

    def clear(self) -> None:
        """Clear all cached tokens."""
        size = len(self._cache)
        self._cache.clear()
        logger.info(f"Cleared token cache ({size} entries)")

    def get_stats(self) -> dict:
        """Get cache statistics."""
        return {
            "size": len(self._cache),
            "maxsize": self._maxsize,
            "ttl_seconds": self._ttl,
        }


# Singleton instance
_token_cache: Optional[TokenCache] = None


def get_token_cache() -> TokenCache:
    """Get the token cache singleton."""
    global _token_cache
    if _token_cache is None:
        _token_cache = TokenCache()
    return _token_cache
