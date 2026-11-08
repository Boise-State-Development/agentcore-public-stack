"""Admin user search (#1874): contains, case-insensitive, typo tolerant, every status, paged.

Also keeps the duplicate-profile behaviour of the old exact search: some emails
own a legacy numeric-id PROFILE row beside the Cognito-sub one, and an exact
email lists every one of them, the live row first.
"""

import base64
import json
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.users import search_index
from apis.app_api.admin.users.routes import get_user_admin_service, require_users_admin, router
from apis.app_api.admin.users.search_index import AdminUserIndex, _prefix_distance, fold
from apis.app_api.admin.users.service import UserAdminService
from apis.shared.auth.models import User
from apis.shared.users.models import UserListItem, UserProfile, UserStatus

# ---------- fixtures ----------


def _row(user_id: str, email: str, name: str, last_login: str = "2026-09-01T00:00:00Z") -> UserListItem:
    return UserListItem(
        user_id=user_id,
        email=email,
        name=name,
        status=UserStatus.ACTIVE,
        last_login_at=last_login,
        email_domain=email.rpartition("@")[2],
    )


class _PagedRepo:
    """Stands in for ``query_users_by_status``: one list per status, served in pages."""

    enabled = True

    def __init__(self, by_status: Dict[str, List[UserListItem]]):
        self.by_status = by_status
        self.calls = 0

    def query_users_by_status(self, status, limit, last_evaluated_key):
        self.calls += 1
        rows = self.by_status.get(status, [])
        start = last_evaluated_key or 0
        end = start + limit
        return rows[start:end], (end if end < len(rows) else None)


ORG = {
    "active": [
        _row("uuid-oscar-1", "oscarfilson@u.example.edu", "Oscar Filson", "2026-09-20T00:00:00Z"),
        _row("uuid-oscar-2", "oscarfilson@example.edu", "Oscar Filson", "2026-09-10T00:00:00Z"),
        _row("uuid-maria", "mgarcia@example.edu", "María García", "2026-09-15T00:00:00Z"),
        _row("uuid-filsonia", "jfilsonia@example.edu", "Jo Filsonia", "2026-08-01T00:00:00Z"),
        _row("uuid-pat", "pat@example.edu", "Pat Oscarson", "2026-07-01T00:00:00Z"),
    ],
    "inactive": [_row("uuid-ina", "inactive.person@example.edu", "Ina Active", "2025-01-01T00:00:00Z")],
    "suspended": [_row("uuid-sus", "suspended.person@u.example.edu", "Sus Pended", "2025-02-01T00:00:00Z")],
}


def _index(by_status=None, **kwargs) -> AdminUserIndex:
    return AdminUserIndex(_PagedRepo(by_status or ORG), background_refresh=False, **kwargs)


def _ids(page) -> List[str]:
    return [u.user_id for u in page.users]


# ---------- index: matching ----------


def test_partial_email_finds_both_accounts():
    assert _ids(_index().search("oscarfilson")) == ["uuid-oscar-1", "uuid-oscar-2"]


@pytest.mark.parametrize("query", ["filson", "FILSON", "  Filson  "])
def test_contains_match_is_case_and_whitespace_insensitive(query):
    assert {"uuid-oscar-1", "uuid-oscar-2", "uuid-filsonia"} <= set(_ids(_index().search(query)))


def test_domain_fragment_matches_email():
    assert set(_ids(_index().search("@u.example.edu"))) == {"uuid-oscar-1", "uuid-sus"}


def test_accents_are_folded():
    assert _ids(_index().search("maria garcia")) == ["uuid-maria"]


def test_user_id_matches():
    assert _ids(_index().search("uuid-mar")) == ["uuid-maria"]


def test_ranking_exact_then_prefix_then_contains():
    # "oscar": prefix of two emails and names, contained in "Pat Oscarson" only as a later word (prefix of a word).
    got = _ids(_index().search("oscar"))
    assert got[:2] == ["uuid-oscar-1", "uuid-oscar-2"]
    assert "uuid-pat" in got

    # Exact email beats a prefix of a longer one.
    assert _ids(_index().search("oscarfilson@example.edu"))[0] == "uuid-oscar-2"


def test_words_match_in_any_order():
    assert set(_ids(_index().search("filson oscar"))) == {"uuid-oscar-1", "uuid-oscar-2"}


@pytest.mark.parametrize("typo", ["osacr filson", "oscar filsn", "oscarfilsom", "osacrfil"])
def test_small_typo_finds_the_user(typo):
    assert {"uuid-oscar-1", "uuid-oscar-2"} <= set(_ids(_index().search(typo)))


def test_typo_matches_rank_below_contains_matches():
    # "filson" is contained in Jo Filsonia; "filsonn" is not contained anywhere.
    got = _ids(_index().search("filsoni"))
    assert got[0] == "uuid-filsonia"  # contains
    assert {"uuid-oscar-1", "uuid-oscar-2"} <= set(got[1:])  # typo (filsoni -> filson)


def test_short_words_need_an_exact_substring():
    assert _ids(_index().search("xyz")) == []
    assert _ids(_index().search("zzzzzzzz")) == []


def test_prefix_distance():
    assert _prefix_distance("osacr", "oscar", 1) == 1  # transposition
    assert _prefix_distance("filsn", "filson", 1) == 1
    assert _prefix_distance("oscarfil", "oscarfilson", 1) == 0  # still typing
    assert _prefix_distance("garcia", "oscar", 1) is None


def test_fold():
    assert fold("  José   ÁLVAREZ ") == "jose alvarez"


# ---------- index: filters and pagination ----------


def test_inactive_and_suspended_are_searchable_and_labelled():
    page = _index().search("person")
    assert {(u.user_id, u.status) for u in page.users} == {("uuid-ina", "inactive"), ("uuid-sus", "suspended")}


def test_status_filter_applies():
    assert _ids(_index().search("person", status="suspended")) == ["uuid-sus"]
    assert _ids(_index().search("person", status="active")) == []


def test_domain_filter_applies():
    assert _ids(_index().search("oscarfilson", domain="U.Example.edu")) == ["uuid-oscar-1"]


def test_blank_query_lists_everyone_most_recent_sign_in_first():
    page = _index().search("", limit=100)
    assert page.total == 7
    assert page.users[0].user_id == "uuid-oscar-1"
    assert page.users[-1].user_id == "uuid-ina"


def test_pages_through_every_match():
    index = _index()
    first = index.search("", limit=3)
    second = index.search("", offset=first.next_offset, limit=3)
    third = index.search("", offset=second.next_offset, limit=3)
    assert first.next_offset == 3 and second.next_offset == 6 and third.next_offset is None
    assert len(set(_ids(first) + _ids(second) + _ids(third))) == 7


def test_reads_past_the_first_dynamodb_page():
    many = [_row(f"uuid-{i:05d}", f"user{i:05d}@example.edu", f"User {i}") for i in range(9_000)]
    many.append(_row("uuid-needle", "needle@example.edu", "Needle Person", "2020-01-01T00:00:00Z"))
    repo = _PagedRepo({"active": many})
    index = AdminUserIndex(repo, background_refresh=False)

    assert _ids(index.search("needle")) == ["uuid-needle"]
    assert repo.calls > 2  # suspended + inactive + several active pages


def test_row_cap_drops_least_recent_active_first(caplog):
    caplog.set_level("WARNING", logger=search_index.__name__)
    index = _index(max_rows=6)
    ids = _ids(index.search("", limit=100))
    assert "uuid-sus" in ids and "uuid-ina" in ids  # small partitions read first
    assert "uuid-pat" not in ids  # least recent active sign-in
    assert "ADMIN_USER_INDEX_MAX_ROWS" in caplog.text


def test_domains_most_users_first():
    assert _index().domains(10) == ["example.edu", "u.example.edu"]


# ---------- index: refresh ----------


def test_snapshot_is_reused_within_ttl_and_reread_after():
    now = [0.0]
    repo = _PagedRepo(ORG)
    index = AdminUserIndex(repo, clock=lambda: now[0], background_refresh=False)

    index.search("oscar")
    reads = repo.calls
    index.search("maria")
    assert repo.calls == reads

    now[0] += search_index.SNAPSHOT_TTL_SECONDS + 1
    index.search("maria")
    assert repo.calls == reads * 2


def test_empty_refresh_keeps_previous_snapshot():
    now = [0.0]
    repo = _PagedRepo(ORG)
    index = AdminUserIndex(repo, clock=lambda: now[0], background_refresh=False)
    index.search("oscar")

    repo.by_status = {}  # a failed read looks empty
    now[0] += search_index.SNAPSHOT_TTL_SECONDS + 1
    assert _ids(index.search("oscarfilson")) == ["uuid-oscar-1", "uuid-oscar-2"]


def test_stale_snapshot_serves_while_refreshing_in_background():
    now = [0.0]
    repo = _PagedRepo(ORG)
    index = AdminUserIndex(repo, clock=lambda: now[0], background_refresh=True)
    index.search("oscar")  # first load is synchronous

    now[0] += search_index.SNAPSHOT_TTL_SECONDS + 1
    assert _ids(index.search("oscarfilson")) == ["uuid-oscar-1", "uuid-oscar-2"]


# ---------- service ----------


def _profile(user_id: str, last_login_at: str, status=UserStatus.ACTIVE) -> UserProfile:
    return UserProfile(
        user_id=user_id,
        email="prof@example.com",
        name="Prof",
        email_domain="example.com",
        created_at="2026-03-01T00:00:00Z",
        last_login_at=last_login_at,
        status=status,
    )


def _service(profiles: list = (), index: AdminUserIndex = None) -> UserAdminService:
    repo = AsyncMock()
    type(repo).enabled = PropertyMock(return_value=True)
    repo.get_users_by_email.return_value = list(profiles)
    return UserAdminService(
        user_repository=repo,
        cost_aggregator=MagicMock(),
        quota_resolver=MagicMock(),
        quota_repository=MagicMock(),
        user_index=index or _index(),
    )


@pytest.mark.asyncio
async def test_exact_email_lists_every_profile_live_first():
    service = _service(
        [
            _profile("uuid-live", "2026-09-20T00:00:00Z"),
            _profile("100000001", "2026-04-15T00:00:00Z"),
        ]
    )

    got = await service.search_users("prof@example.com")

    assert [u.user_id for u in got.users] == ["uuid-live", "100000001"]
    assert got.total_count == 2  # neither is in the index yet: both count


@pytest.mark.asyncio
async def test_exact_email_rows_are_not_listed_twice():
    profile = UserProfile(
        user_id="uuid-oscar-2",
        email="oscarfilson@example.edu",
        name="Oscar Filson",
        email_domain="example.edu",
        created_at="2026-01-01T00:00:00Z",
        last_login_at="2026-09-10T00:00:00Z",
        status=UserStatus.ACTIVE,
    )
    got = await _service([profile]).search_users("oscarfilson@example.edu")

    assert [u.user_id for u in got.users] == ["uuid-oscar-2"]
    assert got.total_count == 1


@pytest.mark.asyncio
async def test_exact_email_respects_status_filter():
    service = _service([_profile("uuid-live", "2026-09-20T00:00:00Z", status=UserStatus.SUSPENDED)])
    got = await service.search_users("prof@example.com", status="active")
    assert got.users == []


@pytest.mark.asyncio
async def test_non_email_query_skips_email_lookup():
    service = _service()
    await service.search_users("oscar")
    service._user_repo.get_users_by_email.assert_not_called()


@pytest.mark.asyncio
async def test_search_cursor_round_trips():
    service = _service()
    first = await service.search_users("", limit=4)
    second = await service.search_users("", limit=4, cursor=first.next_cursor)

    assert first.total_count == 7
    assert len(first.users) == 4 and len(second.users) == 3
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_bad_cursor_restarts_from_the_top():
    service = _service()
    got = await service.search_users("", limit=2, cursor="not-base64!!")
    assert [u.user_id for u in got.users] == ["uuid-oscar-1", "uuid-maria"]


@pytest.mark.asyncio
async def test_list_all_statuses_uses_the_index():
    service = _service()
    got = await service.list_users(status="all", limit=100)
    assert got.total_count == 7
    service._user_repo.list_users_by_status.assert_not_called()


@pytest.mark.asyncio
async def test_list_one_status_queries_dynamodb():
    service = _service()
    service._user_repo.list_users_by_status.return_value = ([ORG["active"][0]], {"PK": "x"})
    got = await service.list_users(status="active", limit=1)

    assert [u.user_id for u in got.users] == ["uuid-oscar-1"]
    assert json.loads(base64.b64decode(got.next_cursor)) == {"PK": "x"}


@pytest.mark.asyncio
async def test_list_one_status_ignores_an_index_cursor():
    service = _service()
    service._user_repo.list_users_by_status.return_value = ([], None)
    cursor = base64.b64encode(json.dumps({"offset": 25}).encode()).decode()
    await service.list_users(status="active", cursor=cursor)
    assert service._user_repo.list_users_by_status.call_args.kwargs["last_evaluated_key"] is None


@pytest.mark.asyncio
async def test_list_domains_comes_from_the_index():
    assert await _service().list_domains() == ["example.edu", "u.example.edu"]


# ---------- routes ----------


def _client(service: UserAdminService) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_users_admin] = lambda: User(email="admin@example.com", user_id="admin-001", name="Admin", roles=["Admin"])
    app.dependency_overrides[get_user_admin_service] = lambda: service
    return TestClient(app)


def test_route_lists_duplicates_live_first():
    client = _client(
        _service(
            [
                _profile("uuid-live", "2026-09-20T00:00:00Z"),
                _profile("100000001", "2026-04-15T00:00:00Z"),
            ]
        )
    )

    resp = client.get("/users/search", params={"q": "prof@example.com"})

    assert resp.status_code == 200
    assert [u["userId"] for u in resp.json()["users"]] == ["uuid-live", "100000001"]


def test_route_partial_search_with_filters_and_total():
    resp = _client(_service()).get("/users/search", params={"q": "person", "status": "suspended"})

    assert resp.status_code == 200
    body = resp.json()
    assert [(u["userId"], u["status"]) for u in body["users"]] == [("uuid-sus", "suspended")]
    assert body["totalCount"] == 1


def test_route_rejects_unknown_status():
    assert _client(_service()).get("/users/search", params={"q": "x", "status": "deleted"}).status_code == 422


def test_route_requires_a_query():
    assert _client(_service()).get("/users/search").status_code == 422


def test_route_error_does_not_leak_exception_text():
    service = _service()
    service.search_users = AsyncMock(side_effect=RuntimeError("table arn:aws:dynamodb:secret exploded"))

    resp = _client(service).get("/users/search", params={"q": "oscar"})

    assert resp.status_code == 500
    assert resp.json() == {"detail": "Failed to search users"}
