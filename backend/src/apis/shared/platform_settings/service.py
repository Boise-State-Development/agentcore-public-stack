"""Chat-mode settings service with a short in-process TTL cache.

The inference path reads the policy on every turn; the TTL cache keeps
that to one DynamoDB read per container per minute. Admin updates bust
the local cache immediately — other containers converge within the TTL.
Reads degrade to compiled-in defaults (current server behavior) when the
table is unconfigured, the item is absent, or the read fails.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .models import ChatModeSettings, ChatModeSettingsUpdate
from .repository import PlatformSettingsRepository, get_platform_settings_repository

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60.0


class ChatModeSettingsService:
    """Cached read / write-through update of the chat-mode policy."""

    def __init__(
        self,
        repository: Optional[PlatformSettingsRepository] = None,
        cache_ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._repository = repository or get_platform_settings_repository()
        self._cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._cached: Optional[ChatModeSettings] = None
        self._cached_at: float = 0.0

    async def get_settings(self) -> ChatModeSettings:
        """Get the chat-mode policy, falling back to defaults on any failure."""
        now = self._clock()
        if self._cached is not None and (now - self._cached_at) < self._cache_ttl_seconds:
            return self._cached

        try:
            settings = await self._repository.get_chat_mode_settings()
        except Exception:
            logger.exception("Failed to read chat-mode settings — using defaults")
            settings = None

        resolved = settings or ChatModeSettings()
        self._cached = resolved
        self._cached_at = now
        return resolved

    async def update_settings(
        self,
        update: ChatModeSettingsUpdate,
        updated_by: str,
    ) -> ChatModeSettings:
        """Persist a new chat-mode policy and bust the local cache."""
        settings = ChatModeSettings(
            default_mode=update.default_mode,
            allow_mode_toggle=update.allow_mode_toggle,
            updated_at=datetime.now(timezone.utc),
            updated_by=updated_by,
        )
        await self._repository.put_chat_mode_settings(settings)
        self._cached = settings
        self._cached_at = self._clock()
        return settings


# Singleton instance
_service: Optional[ChatModeSettingsService] = None


def get_chat_mode_settings_service() -> ChatModeSettingsService:
    """Get the chat-mode settings service singleton."""
    global _service
    if _service is None:
        _service = ChatModeSettingsService()
    return _service
