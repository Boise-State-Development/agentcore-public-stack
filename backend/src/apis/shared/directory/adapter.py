"""The directory port and its provider switch.

``DIRECTORY_PROVIDER`` picks the implementation. ``users_table`` (the default,
and the only one today) searches people who have signed in. An unknown value
logs a warning and falls back to the default, so a typo never removes search.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Iterable, List, Optional, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "users_table"


class DirectoryPerson(BaseModel):
    """One person. ``known`` is False for an email nobody has signed in with yet."""

    email: str
    name: str = ""
    known: bool = True
    # The account this email signs in as, for binding a project member to it.
    # Internal: excluded from every dump, so it can never reach a response.
    user_id: Optional[str] = Field(None, exclude=True)


class DirectoryAdapter(Protocol):
    async def search(self, query: str, limit: int) -> List[DirectoryPerson]:
        """People matching ``query`` (email prefix or name), best match first, at most ``limit``."""
        ...

    def find_by_emails(self, emails: Iterable[str]) -> Dict[str, DirectoryPerson]:
        """The known people among ``emails``, keyed by lowercased email. Blocking: may refresh."""
        ...

    def find_by_user_ids(self, user_ids: Iterable[str]) -> Dict[str, DirectoryPerson]:
        """The known people among ``user_ids``, keyed by user id. Blocking: may refresh."""
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


def display_names(emails: Iterable[str]) -> Dict[str, str]:
    """``email → name`` for the people the directory knows by name. Best-effort: never raises.

    A name is a courtesy beside the email that identifies someone, so a directory
    failure shows emails rather than failing the request. Blocking (it may refresh
    the snapshot): from async code, run it in a thread.
    """
    try:
        found = get_directory().find_by_emails(emails)
    except Exception:
        logger.warning("Directory lookup failed; showing emails without names", exc_info=True)
        return {}
    return {email: person.name for email, person in found.items() if person.name}


def people_by_user_id(user_ids: Iterable[str]) -> Dict[str, DirectoryPerson]:
    """``user id → person`` for the ids the directory knows. Best-effort: never raises. Blocking."""
    try:
        return get_directory().find_by_user_ids(user_ids)
    except Exception:
        logger.warning("Directory lookup by user id failed", exc_info=True)
        return {}
