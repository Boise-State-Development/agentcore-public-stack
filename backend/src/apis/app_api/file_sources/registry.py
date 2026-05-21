"""File-source adapter registry.

The registry is the "what the codebase can do" boundary: it is populated
purely by adapter code shipped in a release, never by config or admin action.
An admin maps a connector to one of these registered adapters; the registry
itself is immutable at runtime.
"""

import logging
from typing import Dict, List, Optional

from apis.shared.oauth.models import OAuthProviderType

from apis.app_api.file_sources.adapter import FileSourceAdapter

logger = logging.getLogger(__name__)


class AdapterRegistry:
    """An in-memory map of adapter key -> adapter instance."""

    def __init__(self) -> None:
        self._adapters: Dict[str, FileSourceAdapter] = {}

    def register(self, adapter: FileSourceAdapter) -> None:
        """Register an adapter. Raises on a duplicate key."""
        key = adapter.metadata.key
        if key in self._adapters:
            raise ValueError(f"Duplicate file-source adapter key: {key}")
        self._adapters[key] = adapter
        logger.info("Registered file-source adapter: %s", key)

    def get(self, key: str) -> Optional[FileSourceAdapter]:
        """Return the adapter for `key`, or None if no such adapter is shipped."""
        return self._adapters.get(key)

    def all(self) -> List[FileSourceAdapter]:
        """Return every registered adapter."""
        return list(self._adapters.values())

    def adapters_for_provider_type(
        self, provider_type: OAuthProviderType
    ) -> List[FileSourceAdapter]:
        """Return adapters that may be mapped to a connector of this type."""
        return [
            a
            for a in self._adapters.values()
            if provider_type in a.metadata.compatible_provider_types
        ]


def _build_default_registry() -> AdapterRegistry:
    """Construct the registry with every adapter shipped in this release."""
    from apis.app_api.file_sources.adapters.google_drive import GoogleDriveAdapter

    reg = AdapterRegistry()
    reg.register(GoogleDriveAdapter())
    return reg


# Process-wide singleton, populated at import time.
registry = _build_default_registry()
