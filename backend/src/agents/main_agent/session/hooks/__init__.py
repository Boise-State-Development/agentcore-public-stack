"""Hooks for Main Agent"""

from agents.main_agent.session.hooks.stop import StopHook
from agents.main_agent.session.hooks.conversation_caching import (
    ConversationCachingHook,
    is_caching_supported,
    CACHING_SUPPORTED_PATTERNS,
)

__all__ = [
    "StopHook",
    "ConversationCachingHook",
    "is_caching_supported",
    "CACHING_SUPPORTED_PATTERNS",
]





