"""People directory over the users table (shared-projects PR-1.3, §9.1)."""

import pytest

from apis.shared.directory import adapter as directory_adapter
from apis.shared.directory.users_table import SNAPSHOT_TTL_SECONDS, UsersTableDirectory
from apis.shared.users.models import UserProfile, UserStatus


async def _add(repo, n: int, email: str, name: str, status: UserStatus = UserStatus.ACTIVE) -> None:
    # Later n = more recent sign-in.
    at = f"2026-09-{1 + n // 1440:02d}T{(n // 60) % 24:02d}:{n % 60:02d}:00Z"
    await repo.upsert_user(UserProfile(
        user_id=f"u{n}", email=email, name=name, email_domain=email.split("@")[1],
        created_at=at, last_login_at=at, status=status,
    ))


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_finds_people_past_the_old_100_user_cap(user_repository):
    """/users/search only looked at the 100 most recent sign-ins; the directory pages all of them."""
    await _add(user_repository, 0, "ada@example.edu", "Ada Lovelace")  # the oldest sign-in
    for n in range(1, 151):
        await _add(user_repository, n, f"user{n}@example.edu", f"User {n}")

    people = await UsersTableDirectory(user_repository).search("lovelace", 10)
    assert [p.email for p in people] == ["ada@example.edu"]


@pytest.mark.asyncio
async def test_ranks_email_then_name_matches_and_recency_breaks_ties(user_repository):
    await _add(user_repository, 1, "sam.smith@example.edu", "Sam Smith")
    await _add(user_repository, 2, "jordan@example.edu", "Jordan Samuels")      # name word prefix
    await _add(user_repository, 3, "alex@example.edu", "Alex Balsamo")          # name contains
    await _add(user_repository, 4, "pat.osam@example.edu", "Pat O")             # email contains
    await _add(user_repository, 5, "sam.jones@example.edu", "Sam Jones")        # newer email prefix
    await _add(user_repository, 6, "sam@example.edu", "S")                      # newest email prefix

    directory = UsersTableDirectory(user_repository)
    assert [p.email for p in await directory.search("sam", 10)] == [
        "sam@example.edu", "sam.jones@example.edu", "sam.smith@example.edu",
        "jordan@example.edu", "alex@example.edu", "pat.osam@example.edu",
    ]
    # An exact email outranks newer prefix matches.
    await _add(user_repository, 7, "sam.j@example.edu", "Sam J")
    assert [p.email for p in await UsersTableDirectory(user_repository).search("SAM.Jones@example.edu", 10)] == [
        "sam.jones@example.edu",
    ]
    assert [p.email for p in await UsersTableDirectory(user_repository).search("sam.j", 10)][:2] == [
        "sam.j@example.edu", "sam.jones@example.edu",
    ]


@pytest.mark.asyncio
async def test_limit_inactive_users_and_blank_queries(user_repository):
    await _add(user_repository, 1, "kim@example.edu", "Kim A")
    await _add(user_repository, 2, "kimberly@example.edu", "Kim B")
    await _add(user_repository, 3, "kimo@example.edu", "Kim C", status=UserStatus.SUSPENDED)

    directory = UsersTableDirectory(user_repository)
    assert [p.email for p in await directory.search("kim", 1)] == ["kimberly@example.edu"]
    assert "kimo@example.edu" not in [p.email for p in await directory.search("kim", 10)]
    assert await directory.search("   ", 10) == []
    assert all(p.known for p in await directory.search("kim", 10))


@pytest.mark.asyncio
async def test_holds_the_active_list_between_keystrokes(user_repository):
    clock = _Clock()
    directory = UsersTableDirectory(user_repository, clock=clock)
    await _add(user_repository, 1, "lee@example.edu", "Lee")
    assert len(await directory.search("lee", 10)) == 1

    await _add(user_repository, 2, "leeann@example.edu", "Leeann")
    assert len(await directory.search("lee", 10)) == 1      # same snapshot
    clock.now += SNAPSHOT_TTL_SECONDS
    assert len(await directory.search("lee", 10)) == 2      # refreshed


@pytest.mark.asyncio
async def test_an_empty_read_is_not_held(user_repository):
    clock = _Clock()
    directory = UsersTableDirectory(user_repository, clock=clock)
    assert await directory.search("new", 10) == []
    await _add(user_repository, 1, "newbie@example.edu", "New")
    assert len(await directory.search("new", 10)) == 1      # no stale empty snapshot


@pytest.mark.asyncio
async def test_no_users_table_means_no_results(monkeypatch):
    from apis.shared.users.repository import UserRepository

    monkeypatch.delenv("DYNAMODB_USERS_TABLE_NAME", raising=False)
    assert await UsersTableDirectory(UserRepository(table_name="")).search("a", 5) == []


def test_unknown_provider_falls_back_to_the_users_table(monkeypatch):
    monkeypatch.setenv("DIRECTORY_PROVIDER", "graph")
    monkeypatch.setattr(directory_adapter, "_directory", None)
    assert isinstance(directory_adapter.get_directory(), UsersTableDirectory)
    monkeypatch.setattr(directory_adapter, "_directory", None)
