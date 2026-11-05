"""In-memory search over every user profile, for the admin User Lookup page.

The users table can only answer an exact email (``EmailIndex``) or a page of
one status or one domain (``StatusLoginIndex`` / ``EmailDomainIndex``). An
admin types part of a name or an email, possibly misspelled, and wants every
status. DynamoDB has no case-insensitive substring match, and a filtered scan
would read every user on every keystroke, so this holds the users in memory
instead and matches there.

**What it reads.** Every partition of ``StatusLoginIndex`` (suspended, inactive,
active), one 1 MB page at a time. That index already projects ``userId``,
``email``, ``name`` and ``emailDomain``, its sort key is ``lastLoginAt`` and its
partition key is the status, so no table, index or backfill change is needed.
An index row is ~250 bytes: 100k users is ~25 MB read, about 3k eventually
consistent read units, a fraction of a cent per refresh.

**How often.** At most once per ``SNAPSHOT_TTL_SECONDS`` per app-api task, and
only while admins use the page. After the first load a stale snapshot keeps
serving while one background thread re-reads, so no search waits on a refresh.

**Bounded.** ``ADMIN_USER_INDEX_MAX_ROWS`` (default 100k) caps the rows held.
Suspended and inactive users are read first because they are few; past the cap
the least recent active sign-ins are dropped and a warning is logged.

**Ranking**, best first: exact (email, name or user id), prefix (of the email,
the name, a word of the name, or the id), contains, every query word contained
in any order, then a typo match (edit distance 1-2 against the words of the
name and email). Within a rank the most recent sign-in comes first, which for
rows sharing an email is the live one (``live_profile_rank``).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple, Union

from apis.shared.users.models import UserStatus
from apis.shared.users.repository import UserRepository

logger = logging.getLogger(__name__)

SNAPSHOT_TTL_SECONDS = 60.0
DEFAULT_MAX_ROWS = 100_000
_PAGE_SIZE = 4_000  # ~250 B per index row: about one 1 MB DynamoDB page per call
_RESULT_CACHE_SIZE = 32

# Read the small partitions first so the row cap only ever drops active users.
_READ_ORDER = (UserStatus.SUSPENDED.value, UserStatus.INACTIVE.value, UserStatus.ACTIVE.value)
STATUS_ALL = "all"

# Match ranks, best first.
EXACT, PREFIX, CONTAINS, ALL_WORDS, TYPO = range(5)

_MIN_TYPO_WORD = 4  # shorter words must match exactly; a typo budget on 3 letters matches nearly anything
_WORD_SPLIT = re.compile(r"[\W\d_]+")


def fold(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace, so ``José`` matches ``jose``."""
    text = (text or "").lower()
    if text.isascii():
        return " ".join(text.split())
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


def _words(text: str) -> List[str]:
    return [w for w in _WORD_SPLIT.split(text) if w]


def _typo_budget(word: str) -> int:
    return 1 if len(word) <= 6 else 2


def _prefix_distance(word: str, token: str, budget: int) -> Optional[int]:
    """Edit distance (with transpositions) from ``word`` to ``token`` or to a prefix of it.

    Prefixes count because the admin may still be typing (``osacrfil`` for
    ``oscarfilson``). Returns None once the distance must exceed ``budget``.
    """
    la = len(word)
    target = token[: la + budget]
    lb = len(target)
    if lb < la - budget:
        return None

    prev2: List[int] = []
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        a = word[i - 1]
        cur = [i] + [0] * lb
        row_min = i
        for j in range(1, lb + 1):
            b = target[j - 1]
            value = prev[j - 1] + (a != b)
            if prev[j] + 1 < value:
                value = prev[j] + 1
            if cur[j - 1] + 1 < value:
                value = cur[j - 1] + 1
            if i > 1 and j > 1 and a == target[j - 2] and word[i - 2] == b and prev2[j - 2] + 1 < value:
                value = prev2[j - 2] + 1
            cur[j] = value
            if value < row_min:
                row_min = value
        if row_min > budget:
            return None
        prev2, prev = prev, cur

    best = min(prev[max(0, la - budget) :])
    return best if best <= budget else None


def _login_timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if not parsed.tzinfo:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


@dataclass(frozen=True, slots=True)
class IndexedUser:
    user_id: str
    email: str
    name: str
    status: str
    last_login_at: str
    email_domain: str


@dataclass
class _Snapshot:
    users: List[IndexedUser] = field(default_factory=list)  # live-first order
    # Parallel to ``users``: "email\nfolded name\nuser id". A query never holds "\n",
    # so one ``in`` test covers all three fields without matching across them.
    haystacks: List[str] = field(default_factory=list)
    # Word of a name or email local part -> its position, or positions when shared.
    words: Dict[str, Union[int, List[int]]] = field(default_factory=dict)
    domains: List[Tuple[str, int]] = field(default_factory=list)  # (domain, user count), most users first
    user_ids: frozenset = frozenset()
    generation: int = 0


@dataclass(frozen=True)
class SearchPage:
    users: List[IndexedUser]
    total: int
    next_offset: Optional[int]


def _rank(needle: str, haystack: str) -> int:
    """Rank of a user whose haystack is known to contain ``needle``."""
    email, name, user_id = haystack.split("\n")
    if needle == email or needle == name or needle == user_id:
        return EXACT
    if email.startswith(needle) or name.startswith(needle) or f" {needle}" in name or user_id.startswith(needle):
        return PREFIX
    return CONTAINS


class AdminUserIndex:
    """Process-wide, read-mostly snapshot of every user, refreshed every ``SNAPSHOT_TTL_SECONDS``."""

    def __init__(
        self,
        repository: Optional[UserRepository] = None,
        clock=time.monotonic,
        max_rows: Optional[int] = None,
        background_refresh: bool = True,
    ):
        self._repository = repository or UserRepository()
        self._clock = clock
        self._max_rows = max_rows or int(os.getenv("ADMIN_USER_INDEX_MAX_ROWS", DEFAULT_MAX_ROWS))
        self._background_refresh = background_refresh
        self._snapshot: Optional[_Snapshot] = None
        self._snapshot_at = 0.0
        self._generation = 0
        self._refresh_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._results: "OrderedDict[tuple, List[int]]" = OrderedDict()

    @property
    def enabled(self) -> bool:
        return bool(self._repository.enabled)

    # ---------- queries (blocking: call through asyncio.to_thread) ----------

    def search(
        self,
        query: str,
        *,
        status: str = STATUS_ALL,
        domain: Optional[str] = None,
        offset: int = 0,
        limit: int = 25,
    ) -> SearchPage:
        """Ranked matches for ``query``; every user, most recent sign-in first, when it is blank.

        The ranked positions are cached per query and snapshot, so "Load more"
        and the next keystroke's repeat never re-rank.
        """
        if not self.enabled:
            return SearchPage(users=[], total=0, next_offset=None)

        snapshot = self._current()
        needle = fold(query)
        wanted_domain = (domain or "").strip().lower() or None
        key = (snapshot.generation, needle, status, wanted_domain)
        with self._cache_lock:
            positions = self._results.get(key)
            if positions is not None:
                self._results.move_to_end(key)
        if positions is None:
            positions = _match(snapshot, needle, status, wanted_domain)
            with self._cache_lock:
                self._results[key] = positions
                while len(self._results) > _RESULT_CACHE_SIZE:
                    self._results.popitem(last=False)

        page = positions[offset : offset + limit]
        end = offset + len(page)
        return SearchPage(
            users=[snapshot.users[p] for p in page],
            total=len(positions),
            next_offset=end if end < len(positions) else None,
        )

    def domains(self, limit: int) -> List[str]:
        if not self.enabled:
            return []
        return [domain for domain, _ in self._current().domains[:limit]]

    def count_user_ids(self, user_ids: Iterable[str]) -> int:
        """How many of ``user_ids`` the held snapshot contains. Never reads DynamoDB."""
        snapshot = self._snapshot
        return sum(1 for u in user_ids if u in snapshot.user_ids) if snapshot else 0

    # ---------- snapshot ----------

    def _fresh(self) -> bool:
        return self._snapshot is not None and self._clock() - self._snapshot_at < SNAPSHOT_TTL_SECONDS

    def _current(self) -> _Snapshot:
        """The held snapshot, re-read when stale. Only the very first read makes callers wait."""
        if self._fresh():
            return self._snapshot  # type: ignore[return-value]

        if self._snapshot is not None and self._background_refresh:
            if self._refresh_lock.acquire(blocking=False):
                threading.Thread(target=self._refresh_and_release, name="admin-user-index", daemon=True).start()
            return self._snapshot

        with self._refresh_lock:
            if not self._fresh():
                self._refresh()
        return self._snapshot or _Snapshot()

    def _refresh_and_release(self) -> None:
        try:
            if not self._fresh():
                self._refresh()
        except Exception:
            logger.exception("Admin user index refresh failed; keeping the previous snapshot")
        finally:
            self._refresh_lock.release()

    def _read_all(self) -> List[IndexedUser]:
        users: List[IndexedUser] = []
        for status in _READ_ORDER:
            cursor = None
            while True:
                page, cursor = self._repository.query_users_by_status(status=status, limit=_PAGE_SIZE, last_evaluated_key=cursor)
                for item in page:
                    email = (item.email or "").lower()
                    users.append(
                        IndexedUser(
                            user_id=item.user_id,
                            email=email,
                            name=item.name or "",
                            status=status,
                            last_login_at=item.last_login_at or "",
                            email_domain=sys.intern((item.email_domain or email.rpartition("@")[2]).lower()),
                        )
                    )
                if len(users) >= self._max_rows:
                    if len(users) > self._max_rows or cursor or status != _READ_ORDER[-1]:
                        logger.warning(
                            "Admin user index holds only %d users (ADMIN_USER_INDEX_MAX_ROWS); "
                            "the least recent active sign-ins are not searchable",
                            self._max_rows,
                        )
                    return users[: self._max_rows]
                if not cursor:
                    break
        return users

    def _refresh(self) -> None:
        started = time.monotonic()
        users = self._read_all()

        # A failed page reads as an empty one, so an empty table may be an error:
        # keep what was held and try again next time.
        if not users and self._snapshot is not None:
            logger.warning("Admin user index refresh read no users; keeping the previous snapshot")
            self._snapshot_at = self._clock()
            return

        # Same order as live_profile_rank: most recent sign-in, then non-numeric id, then id.
        users.sort(key=lambda u: (-_login_timestamp(u.last_login_at), u.user_id.isdigit(), u.user_id))

        haystacks: List[str] = []
        words: Dict[str, Union[int, List[int]]] = {}
        for position, user in enumerate(users):
            name = fold(user.name)
            haystacks.append(f"{user.email}\n{name}\n{user.user_id.lower()}")
            local = user.email.partition("@")[0]
            for word in {*_words(name), *_words(local), _WORD_SPLIT.sub("", local)}:
                if len(word) < _MIN_TYPO_WORD - 1:
                    continue
                held = words.get(word)
                if held is None:
                    words[word] = position  # most words belong to one user: skip the list
                elif isinstance(held, int):
                    words[word] = [held, position]
                else:
                    held.append(position)

        self._generation += 1
        self._snapshot = _Snapshot(
            users=users,
            haystacks=haystacks,
            words=words,
            domains=Counter(u.email_domain for u in users if u.email_domain).most_common(),
            user_ids=frozenset(u.user_id for u in users),
            generation=self._generation,
        )
        self._snapshot_at = self._clock()
        with self._cache_lock:
            self._results.clear()
        logger.info("Admin user index loaded %d users in %.2fs", len(users), time.monotonic() - started)


# ---------- matching ----------


def _match(snapshot: _Snapshot, needle: str, status: str, domain: Optional[str]) -> List[int]:
    users = snapshot.users

    def wanted(position: int) -> bool:
        user = users[position]
        return (status == STATUS_ALL or user.status == status) and (domain is None or user.email_domain == domain)

    if not needle:
        return [p for p in range(len(users)) if wanted(p)]

    haystacks = snapshot.haystacks
    ranked: List[Tuple[int, int, int]] = []  # (rank, typo distance, position); position breaks ties live-first
    matched = set()
    for p in [p for p, h in enumerate(haystacks) if needle in h]:
        matched.add(p)
        if wanted(p):
            ranked.append((_rank(needle, haystacks[p]), 0, p))

    query_words = needle.split()  # the words the admin typed; punctuation stays part of a word
    if len(query_words) > 1:
        longest = max(query_words, key=len)
        rest = [w for w in query_words if w is not longest]
        for p in [p for p, h in enumerate(haystacks) if longest in h]:
            if p not in matched and all(w in haystacks[p] for w in rest):
                matched.add(p)
                if wanted(p):
                    ranked.append((ALL_WORDS, 0, p))

    if any(len(w) >= _MIN_TYPO_WORD for w in query_words):
        for distance, p in _typo_matches(snapshot, query_words, matched):
            if wanted(p):
                ranked.append((TYPO, distance, p))

    ranked.sort()
    return [p for _, _, p in ranked]


def _typo_matches(snapshot: _Snapshot, query_words: List[str], matched: set) -> List[Tuple[int, int]]:
    """``(total distance, position)`` for users every query word reaches, by containment or within its typo budget."""
    closest: Dict[str, Dict[int, int]] = {}
    for word in query_words:
        if len(word) < _MIN_TYPO_WORD:
            continue
        budget = _typo_budget(word)
        letters = set(word)
        shortest = len(word) - budget
        hits: Dict[int, int] = {}
        for token, held in snapshot.words.items():
            # Cheap rejections first: each edit adds at most one letter the token lacks.
            if len(token) < shortest or len(letters.difference(token)) > budget:
                continue
            distance = _prefix_distance(word, token, budget)
            if distance is None:
                continue
            for p in (held,) if isinstance(held, int) else held:
                if p not in matched and distance < hits.get(p, budget + 1):
                    hits[p] = distance
        closest[word] = hits

    haystacks = snapshot.haystacks
    results = []
    for p in set().union(*closest.values()) if closest else ():
        total = 0
        for word in query_words:
            if word in haystacks[p]:
                continue
            distance = closest.get(word, {}).get(p)
            if distance is None:
                break
            total += distance
        else:
            results.append((total, p))
    return results


_index: Optional[AdminUserIndex] = None
_index_lock = threading.Lock()


def get_admin_user_index() -> AdminUserIndex:
    """The process-wide index, so every request shares one snapshot."""
    global _index
    if _index is None:
        with _index_lock:
            if _index is None:
                _index = AdminUserIndex()
    return _index
