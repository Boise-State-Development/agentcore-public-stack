"""Export-target adapter registry.

The registry is the "what the codebase can do" boundary: it is populated
purely by adapter code shipped in a release, never by config or admin action.
An admin maps a connector to one of these registered adapters; the registry
itself is immutable at runtime.

Mirror of `file_sources.registry`. The first concrete adapter
(`GoogleDriveExportAdapter`) lands in the next PR — until then this registry
is intentionally empty: the contract, validation, and admin surface ship
first so the data model is in place.
"""

import logging
from typing import Dict, List, Optional

from apis.shared.oauth.models import OAuthProviderType

from apis.app_api.export_targets.adapter import ExportTargetAdapter

logger = logging.getLogger(__name__)


class ExportTargetRegistry:
    """An in-memory map of adapter key -> adapter instance."""

    def __init__(self) -> None:
        self._adapters: Dict[str, ExportTargetAdapter] = {}

    def register(self, adapter: ExportTargetAdapter) -> None:
        """Register an adapter. Raises on a duplicate key."""
        key = adapter.metadata.key
        if key in self._adapters:
            raise ValueError(f"Duplicate export-target adapter key: {key}")
        self._adapters[key] = adapter
        logger.info("Registered export-target adapter: %s", key)

    def get(self, key: str) -> Optional[ExportTargetAdapter]:
        """Return the adapter for `key`, or None if no such adapter is shipped."""
        return self._adapters.get(key)

    def all(self) -> List[ExportTargetAdapter]:
        """Return every registered adapter."""
        return list(self._adapters.values())

    def adapters_for_provider_type(
        self, provider_type: OAuthProviderType
    ) -> List[ExportTargetAdapter]:
        """Return adapters that may be mapped to a connector of this type."""
        return [
            a
            for a in self._adapters.values()
            if provider_type in a.metadata.compatible_provider_types
        ]


def _build_default_registry() -> ExportTargetRegistry:
    """Construct the registry with every export-target adapter in this release."""
    from apis.app_api.export_targets.adapters.google_drive import (
        GoogleDriveExportAdapter,
    )

    reg = ExportTargetRegistry()
    reg.register(GoogleDriveExportAdapter())
    return reg


# Process-wide singleton, populated at import time.
registry = _build_default_registry()
