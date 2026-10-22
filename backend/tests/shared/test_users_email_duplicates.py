"""UserRepository email lookups when one email owns several PROFILE rows.

The pre-Cognito login keyed users by a numeric employee ID; the current one
keys by the Cognito ``sub`` uuid, and nothing retired the old rows. EmailIndex
has no sort key, so the order a query returns them in is not something the
lookup may depend on. These tests feed rows through a stub table in a chosen
order (moto's order can't be pinned), plus one moto round-trip.
"""

import logging
from typing import List, Optional
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from apis.shared.users.models import UserProfile, UserStatus
from apis.shared.users.repository import UserRepository

EMAIL = "prof@boisestate.edu"
LEGACY_ID = "100000001"
LIVE_ID = "a1b2c3d4-0000-4000-8000-000000000001"
RECREATED_ID = "0b7e4c2a-9d3f-4e61-8a10-6f5d2c9b8e44"


def _item(user_id: str, last_login_at: Optional[str], email: str = EMAIL) -> dict:
    item = {
        "PK": f"USER#{user_id}",
        "SK": "PROFILE",
        "userId": user_id,
        "email": email,
        "name": "Prof",
        "roles": [],
        "emailDomain": "boisestate.edu",
        "createdAt": "2026-03-01T00:00:00Z",
        "status": "active",
    }
    if last_login_at is not None:
        item["lastLoginAt"] = last_login_at
    return item


def _repo_over_pages(*pages: List[dict]) -> UserRepository:
    """A repository whose EmailIndex query returns ``pages`` in order."""
    repo = UserRepository(table_name="")
    repo._enabled = True
    responses = []
    for i, page in enumerate(pages):
        response: dict = {"Items": page}
        if i < len(pages) - 1:
            response["LastEvaluatedKey"] = {"PK": f"page-{i}"}
        responses.append(response)
    repo.table = MagicMock()
    repo.table.query.side_effect = responses
    return repo


class TestGetUserByEmailRanking:
    @pytest.mark.asyncio
    async def test_single_row(self, caplog):
        repo = _repo_over_pages([_item(LIVE_ID, "2026-09-20T12:00:00Z")])

        with caplog.at_level(logging.WARNING, logger="apis.shared.users.repository"):
            got = await repo.get_user_by_email(EMAIL)

        assert got is not None and got.user_id == LIVE_ID
        assert "profiles for one email" not in caplog.text

    @pytest.mark.asyncio
    async def test_empty_result(self):
        assert await _repo_over_pages([]).get_user_by_email(EMAIL) is None
        assert await _repo_over_pages([]).get_users_by_email(EMAIL) == []

    @pytest.mark.asyncio
    async def test_query_error_reads_as_not_found(self):
        repo = _repo_over_pages()
        repo.table.query.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "Query"
        )

        assert await repo.get_user_by_email(EMAIL) is None

    @pytest.mark.asyncio
    async def test_uuid_beats_numeric_listed_first(self, caplog):
        repo = _repo_over_pages([
            _item(LEGACY_ID, "2026-04-15T09:00:00Z"),
            _item(LIVE_ID, "2026-09-20T12:00:00Z"),
        ])

        with caplog.at_level(logging.WARNING, logger="apis.shared.users.repository"):
            got = await repo.get_user_by_email(EMAIL)

        assert got.user_id == LIVE_ID
        assert "2 profiles for one email" in caplog.text
        assert LEGACY_ID in caplog.text

    @pytest.mark.asyncio
    async def test_uuid_beats_numeric_on_equal_login(self):
        same = "2026-04-15T09:00:00Z"
        repo = _repo_over_pages([_item(LEGACY_ID, same), _item(LIVE_ID, same)])

        got = await repo.get_user_by_email(EMAIL)

        assert got.user_id == LIVE_ID

    @pytest.mark.asyncio
    async def test_two_uuids_most_recent_login_wins(self):
        repo = _repo_over_pages([
            _item(RECREATED_ID, "2026-06-01T08:00:00Z"),
            _item(LIVE_ID, "2026-09-20T12:00:00Z"),
        ])

        got = await repo.get_user_by_email(EMAIL)

        assert got.user_id == LIVE_ID

    @pytest.mark.asyncio
    async def test_compares_instants_not_strings(self):
        # As strings "…:00.5Z" < "…:00Z" ('.' sorts before 'Z'); as instants
        # the fractional one is half a second later and must win.
        repo = _repo_over_pages([
            _item(RECREATED_ID, "2026-09-20T12:00:00Z"),
            _item(LIVE_ID, "2026-09-20T12:00:00.500000Z"),
        ])

        got = await repo.get_user_by_email(EMAIL)

        assert got.user_id == LIVE_ID

    @pytest.mark.asyncio
    async def test_legacy_double_suffix_timestamp_is_ranked(self):
        # Pre-fix rows carry "+00:00Z"; _heal_iso repairs it before ranking.
        repo = _repo_over_pages([
            _item(LIVE_ID, "2026-09-20T12:00:00+00:00Z"),
            _item(LEGACY_ID, "2026-04-15T09:00:00Z"),
        ])

        got = await repo.get_user_by_email(EMAIL)

        assert got.user_id == LIVE_ID

    @pytest.mark.asyncio
    async def test_reads_every_page(self):
        repo = _repo_over_pages(
            [_item(LEGACY_ID, "2026-04-15T09:00:00Z")],
            [_item(LIVE_ID, "2026-09-20T12:00:00Z")],
        )

        got = await repo.get_user_by_email(EMAIL)

        assert got.user_id == LIVE_ID
        assert repo.table.query.call_count == 2
        first, second = repo.table.query.call_args_list
        assert "Limit" not in first.kwargs
        assert second.kwargs["ExclusiveStartKey"] == {"PK": "page-0"}

    @pytest.mark.asyncio
    async def test_get_users_by_email_orders_live_first(self):
        repo = _repo_over_pages([
            _item(LEGACY_ID, "2026-04-15T09:00:00Z"),
            _item(RECREATED_ID, "2026-06-01T08:00:00Z"),
            _item(LIVE_ID, "2026-09-20T12:00:00Z"),
        ])

        got = await repo.get_users_by_email(EMAIL.upper())

        assert [p.user_id for p in got] == [LIVE_ID, RECREATED_ID, LEGACY_ID]
        assert repo.table.query.call_args.kwargs["ExpressionAttributeValues"] == {":email": EMAIL}


def _profile(user_id: str, last_login_at: str) -> UserProfile:
    return UserProfile(
        user_id=user_id, email=EMAIL, name="Prof", email_domain="boisestate.edu",
        created_at="2026-03-01T00:00:00Z", last_login_at=last_login_at,
        status=UserStatus.ACTIVE,
    )


class TestAgainstMoto:
    @pytest.mark.asyncio
    async def test_live_profile_wins_in_a_real_index(self, user_repository):
        await user_repository.create_user(_profile(LEGACY_ID, "2026-04-15T09:00:00Z"))
        await user_repository.create_user(_profile(LIVE_ID, "2026-09-20T12:00:00Z"))

        got = await user_repository.get_user_by_email(EMAIL)
        every = await user_repository.get_users_by_email(EMAIL)

        assert got.user_id == LIVE_ID
        assert [p.user_id for p in every] == [LIVE_ID, LEGACY_ID]

    @pytest.mark.asyncio
    async def test_upsert_warns_when_new_id_collides_on_email(self, user_repository, caplog):
        await user_repository.create_user(_profile(LEGACY_ID, "2026-04-15T09:00:00Z"))

        with caplog.at_level(logging.WARNING, logger="apis.shared.users.repository"):
            _, is_new = await user_repository.upsert_user(_profile(LIVE_ID, "2026-09-20T12:00:00Z"))

        assert is_new is True
        assert f"New user {LIVE_ID} shares its email" in caplog.text
        assert LEGACY_ID in caplog.text

    @pytest.mark.asyncio
    async def test_upsert_of_returning_user_does_not_warn(self, user_repository, caplog):
        await user_repository.create_user(_profile(LIVE_ID, "2026-09-01T00:00:00Z"))

        with caplog.at_level(logging.WARNING, logger="apis.shared.users.repository"):
            _, is_new = await user_repository.upsert_user(_profile(LIVE_ID, "2026-09-20T12:00:00Z"))

        assert is_new is False
        assert "shares its email" not in caplog.text
