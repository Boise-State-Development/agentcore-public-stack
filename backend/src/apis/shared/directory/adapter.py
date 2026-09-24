"""The directory port and its provider switch.

``DIRECTORY_PROVIDER`` picks the implementation. ``users_table`` (the default,
and the only one today) searches people who have signed in. An unknown value
logs a warning and falls back to the default, so a typo never removes search.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional, Protocol

from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "users_table"


class DirectoryPerson(BaseModel):
    """One search result. ``known`` is False for an email nobody has signed in with yet."""

    email: str
    name: str = ""
    known: bool = True


class DirectoryAdapter(Protocol):
    async def search(self, query: str, limit: int) -> List[DirectoryPerson]:
        """People matching ``query`` (email prefix or name), best match first, at most ``limit``."""
        ...


_directory: Optional[DirectoryAdapter] = None


def get_directory() -> DirectoryAdapter:
    """The process-wide directory for the configured provider."""
    global _directory
    if _directory is None:
        provider = os.environ.get("DIRECTORY_PROVIDER", "").strip().lower() or DEFAULT_PROVIDER
        if provider != DEFAULT_PROVIDER:
            logger.warning("Unknown DIRECTORY_PROVIDER %r; using %s", provider, DEFAULT_PROVIDER)
        from .users_table import UsersTableDirectory

        _directory = UsersTableDirectory()
    return _directory
