"""People directory: find someone to invite, and put names to emails (docs/specs/shared-projects.md §9.1).

The only provider is the users table, which knows people who have signed in at
least once. Inviting by email works whether or not the directory knows someone,
because every membership and share is keyed by lowercased email. A future Entra
Graph provider (Phase 4.1) implements the same :class:`DirectoryAdapter`.
"""

from .adapter import DirectoryAdapter, DirectoryPerson, display_names, get_directory, people_by_user_id

__all__ = ["DirectoryAdapter", "DirectoryPerson", "display_names", "get_directory", "people_by_user_id"]
