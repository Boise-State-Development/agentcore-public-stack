"""Directory over the users table: people who have signed in at least once.

``/users/search`` matched against only the 100 most recent sign-ins, so anyone
past that was unfindable. This pages the whole active partition of
``StatusLoginIndex`` instead, and matches email and name in one pass.

The spec also named an email-prefix scan of ``EmailIndex``. That index has no
sort key, so a prefix match on it is a full scan of every user of every status,
which is strictly more than the active partition this pass already reads. So
there is one pass, not two.

A typeahead calls this on every keystroke, so the active list is held for
``SNAPSHOT_TTL_SECONDS`` and each keystroke filters in memory. The list holds an
email and a name per person, so it stays small at any realistic org size, and
``MAX_SCANNED`` caps it regardless. Someone who signs in for the first time
becomes findable within the TTL. Until then, inviting them by email works anyway.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

from apis.shared.users.models import UserStatus
from apis.shared.users.repository import UserRepository

from .adapter import DirectoryPerson

logger = logging.getLogger(__name__)

SNAPSHOT_TTL_SECONDS = 60.0
MAX_SCANNED = 20_000
_PAGE_SIZE = 1_000

# Match quality, best first. Within a rank, the most recent sign-in wins.
_EXACT_EMAIL, _EMAIL_PREFIX, _NAME_WORD_PREFIX, _NAME_CONTAINS, _EMAIL_CONTAINS = range(5)


def _rank(query: str, email: str, name: str) -> Optional[int]:
    if email == query:
        return _EXACT_EMAIL
    if email.startswith(query):
        return _EMAIL_PREFIX
    lowered = name.lower()
    if any(word.startswith(query) for word in lowered.split()):
        return _NAME_WORD_PREFIX
    if query in lowered:
        return _NAME_CONTAINS
    if query in email:
        return _EMAIL_CONTAINS
    return None


class UsersTableDirectory:
    def __init__(self, repository: Optional[UserRepository] = None, clock=time.monotonic):
        self._repository = repository or UserRepository()
        self._clock = clock
        self._snapshot: List[Tuple[str, str]] = []
        self._snapshot_at: Optional[float] = None

    async def search(self, query: str, limit: int) -> List[DirectoryPerson]:
        needle = query.strip().lower()
        if not needle or not self._repository.enabled:
            return []

        ranked = []
        for position, (email, name) in enumerate(await self._active_people()):
            rank = _rank(needle, email, name)
            if rank is not None:
                ranked.append((rank, position, email, name))
        ranked.sort()
        return [DirectoryPerson(email=email, name=name) for _, _, email, name in ranked[:limit]]

    async def _active_people(self) -> List[Tuple[str, str]]:
        """``(email, name)`` for every active user, most recent sign-in first."""
        now = self._clock()
        if self._snapshot_at is not None and now - self._snapshot_at < SNAPSHOT_TTL_SECONDS:
            return self._snapshot

        people: List[Tuple[str, str]] = []
        seen = set()
        cursor = None
        while True:
            page, cursor = await self._repository.list_users_by_status(
                status=UserStatus.ACTIVE.value, limit=_PAGE_SIZE, last_evaluated_key=cursor
            )
            for user in page:
                email = user.email.lower()
                if email not in seen:
                    seen.add(email)
                    people.append((email, user.name or ""))
            if not cursor:
                break
            if len(people) >= MAX_SCANNED:
                logger.warning("Directory holds only the %d most recent active users", MAX_SCANNED)
                break

        # The repository reads a failed page as an empty one, so an empty list may be
        # an error rather than an empty org: retry it next time instead of holding it.
        if people:
            self._snapshot, self._snapshot_at = people, now
        return people
