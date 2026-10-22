"""Directory over the users table: people who have signed in at least once.

``/users/search`` matched against only the 100 most recent sign-ins, so anyone
past that was unfindable. This pages the whole active partition of
``StatusLoginIndex`` instead, and matches email and name in one pass.

The spec also named an email-prefix scan of ``EmailIndex``. That index has no
sort key, so a prefix match on it is a full scan of every user of every status,
which is strictly more than the active partition this pass already reads. So
there is one pass, not two.

A typeahead calls this on every keystroke, and every list of people in a project
asks it for names, so the active list is held for ``SNAPSHOT_TTL_SECONDS`` and
each call reads it in memory. The list holds an email, a name and a user id per
person, so it stays small at any realistic org size, and ``MAX_SCANNED`` caps it
regardless. Someone who signs in for the first time becomes findable within the
TTL. Until then, inviting them by email works anyway.

The users table can hold more than one row for an email (an old employee-id row
beside the live Cognito one). The snapshot is read most recent sign-in first and
the first row per email wins, so an email resolves to the account that signed in
last.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

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


@dataclass
class _Snapshot:
    people: List[DirectoryPerson] = field(default_factory=list)  # one per email, most recent sign-in first
    by_email: Dict[str, DirectoryPerson] = field(default_factory=dict)
    by_user_id: Dict[str, DirectoryPerson] = field(default_factory=dict)  # every row, duplicates included


class UsersTableDirectory:
    def __init__(self, repository: Optional[UserRepository] = None, clock=time.monotonic):
        self._repository = repository or UserRepository()
        self._clock = clock
        self._snapshot = _Snapshot()
        self._snapshot_at: Optional[float] = None
        self._lock = threading.Lock()

    async def search(self, query: str, limit: int) -> List[DirectoryPerson]:
        needle = query.strip().lower()
        if not needle or not self._repository.enabled:
            return []

        snapshot = await asyncio.to_thread(self._current)
        ranked = []
        for position, person in enumerate(snapshot.people):
            rank = _rank(needle, person.email, person.name)
            if rank is not None:
                ranked.append((rank, position, person))
        ranked.sort(key=lambda r: (r[0], r[1]))
        return [person for _, _, person in ranked[:limit]]

    def find_by_emails(self, emails: Iterable[str]) -> Dict[str, DirectoryPerson]:
        wanted = {e.strip().lower() for e in emails if e}
        if not wanted or not self._repository.enabled:
            return {}
        by_email = self._current().by_email
        return {e: by_email[e] for e in wanted if e in by_email}

    def find_by_user_ids(self, user_ids: Iterable[str]) -> Dict[str, DirectoryPerson]:
        wanted = {u for u in user_ids if u}
        if not wanted or not self._repository.enabled:
            return {}
        by_user_id = self._current().by_user_id
        return {u: by_user_id[u] for u in wanted if u in by_user_id}

    def _fresh(self) -> bool:
        return self._snapshot_at is not None and self._clock() - self._snapshot_at < SNAPSHOT_TTL_SECONDS

    def _current(self) -> _Snapshot:
        """The held snapshot, re-read once it is older than the TTL. One reader refreshes at a time."""
        if self._fresh():
            return self._snapshot
        with self._lock:
            if not self._fresh():
                self._refresh()
            return self._snapshot

    def _refresh(self) -> None:
        snapshot = _Snapshot()
        cursor = None
        while True:
            page, cursor = self._repository.query_users_by_status(
                status=UserStatus.ACTIVE.value, limit=_PAGE_SIZE, last_evaluated_key=cursor
            )
            for user in page:
                email = user.email.lower()
                latest = snapshot.by_email.get(email)
                if latest is None:
                    latest = DirectoryPerson(email=email, name=user.name or "", user_id=user.user_id)
                    snapshot.by_email[email] = latest
                    snapshot.people.append(latest)
                snapshot.by_user_id.setdefault(user.user_id, latest.model_copy(update={"user_id": user.user_id}))
            if not cursor:
                break
            if len(snapshot.people) >= MAX_SCANNED:
                logger.warning("Directory holds only the %d most recent active users", MAX_SCANNED)
                break

        # The repository reads a failed page as an empty one, so an empty list may be
        # an error rather than an empty org: keep what was held and retry next time.
        if snapshot.people:
            self._snapshot, self._snapshot_at = snapshot, self._clock()
