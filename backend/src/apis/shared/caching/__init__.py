"""Shared caching primitives."""

from .config_cache import (
    MANAGED_MODELS,
    OAUTH_PROVIDERS_ALL,
    OAUTH_PROVIDERS_ENABLED,
    SYSTEM_PROMPTS,
    TOOL_CATALOG,
    ConfigListCache,
    get_config_cache,
    get_or_load,
    invalidate,
    invalidate_oauth_providers,
)

__all__ = [
    "MANAGED_MODELS",
    "OAUTH_PROVIDERS_ALL",
    "OAUTH_PROVIDERS_ENABLED",
    "SYSTEM_PROMPTS",
    "TOOL_CATALOG",
    "ConfigListCache",
    "get_config_cache",
    "get_or_load",
    "invalidate",
    "invalidate_oauth_providers",
]
