"""People directory: find someone to invite (docs/specs/shared-projects.md §9.1).

The only provider is the users table, which knows people who have signed in at
least once. Inviting by email works whether or not the directory knows someone,
because every membership and share is keyed by lowercased email. A future Entra
Graph provider (Phase 4.1) implements the same :class:`DirectoryAdapter`.
"""

from .adapter import DirectoryAdapter, DirectoryPerson, get_directory

__all__ = ["DirectoryAdapter", "DirectoryPerson", "get_directory"]
