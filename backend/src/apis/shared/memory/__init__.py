"""Memory Spaces — user-owned, shareable markdown "second brains" (F5).

A shared-layer primitive (consumed by app-api and the agent runtime, never
importing either) that stores named, per-owner, optionally-shared markdown
wikis: an always-loaded ``MEMORY.md`` index plus typed entries fetched on
demand. See ``docs/specs/user-markdown-memory.md``.

PR-1 (this package) is the data layer only — models, S3 byte store, DynamoDB
repository, and the permission-gated service. No routes, agent tools, or
system-prompt wiring (those land in later PRs). Gated per environment by the
``MEMORY_SPACES_ENABLED`` flag (default off; see ``apis.shared.feature_flags``).
"""

from .models import (
    EntryType,
    MemoryEntryRef,
    MemoryIndex,
    MemorySpace,
    Role,
    ShareRole,
    SpaceMember,
)
from .repository import MemorySpaceRepository
from .service import (
    MemorySpaceError,
    MemorySpaceExport,
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemorySpaceService,
)
from .store import (
    MemorySpaceStore,
    MemorySpaceStoreError,
    compute_content_hash,
    content_key,
    get_memory_space_store,
)
from .templates import TEMPLATES, SpaceTemplate, get_template, is_valid_template

__all__ = [
    "EntryType",
    "MemoryEntryRef",
    "MemoryIndex",
    "MemorySpace",
    "Role",
    "ShareRole",
    "SpaceMember",
    "MemorySpaceRepository",
    "MemorySpaceService",
    "MemorySpaceExport",
    "MemorySpaceError",
    "MemorySpaceNotFoundError",
    "MemorySpacePermissionError",
    "MemorySpaceStore",
    "MemorySpaceStoreError",
    "compute_content_hash",
    "content_key",
    "get_memory_space_store",
    "TEMPLATES",
    "SpaceTemplate",
    "get_template",
    "is_valid_template",
]
