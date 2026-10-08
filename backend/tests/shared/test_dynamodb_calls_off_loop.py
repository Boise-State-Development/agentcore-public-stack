"""The per-request DynamoDB reads on app-api's auth paths run off the event loop.

``RateLimiter``, ``ApiKeyRepository`` and ``UserRepository`` all wrap a
synchronous boto3 table in ``async`` methods. Called directly, each held the
single uvicorn process's event loop for a DynamoDB round trip: a few
milliseconds on every API-key request (rate limit, key lookup, profile) and
on every cookie-session request (profile). These tests pin that each table
call now happens on a worker thread, not the loop's.

They assert the thread, not the loop's responsiveness: a DynamoDB round trip
is too short to park a probe behind, unlike the Bedrock calls in
``tests/routes/test_api_converse_offload.py``.
"""

import threading

import pytest

from apis.shared.auth.api_keys.repository import ApiKeyRepository
from apis.shared.rate_limit import RateLimiter
from apis.shared.users.models import UserProfile, UserStatus
from apis.shared.users.repository import UserRepository


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    # boto3.resource("dynamodb") needs a region to build; nothing is called on it.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


class _RecordingTable:
    """A fake table that records the thread each call ran on."""

    def __init__(self, **responses):
        self.threads: dict[str, int] = {}
        self._responses = responses

    def __getattr__(self, name):
        def _call(**_kwargs):
            self.threads[name] = threading.get_ident()
            return self._responses.get(name, {})

        return _call


def _profile() -> UserProfile:
    return UserProfile(
        user_id="u1", email="a@example.com", name="A", email_domain="example.com",
        created_at="2026-01-01T00:00:00Z", last_login_at="2026-01-01T00:00:00Z",
        status=UserStatus.ACTIVE,
    )


def _assert_off_loop(table: _RecordingTable, *ops: str) -> None:
    loop_thread = threading.get_ident()
    assert set(table.threads) == set(ops), f"expected {ops}, saw {list(table.threads)}"
    for op, ident in table.threads.items():
        assert ident != loop_thread, f"{op} ran on the event loop thread"


@pytest.mark.asyncio
async def test_rate_limiter_counts_off_the_loop():
    limiter = RateLimiter(table_name="t")
    limiter.table = _RecordingTable(update_item={"Attributes": {"requestCount": 1}})
    assert await limiter.check_rate_limit("k") is True
    _assert_off_loop(limiter.table, "update_item")


@pytest.mark.asyncio
async def test_api_key_repository_reads_and_writes_off_the_loop():
    repo = ApiKeyRepository()
    repo.table = _RecordingTable(
        get_item={"Item": {"keyId": "k"}},
        query={"Items": [{"keyId": "k"}]},
    )
    await repo.create_key({"PK": "USER#u", "SK": "KEY#k"})
    await repo.update_last_used("u", "k")
    assert await repo.get_key("u", "k") == {"keyId": "k"}
    assert await repo.get_key_for_user("u") == {"keyId": "k"}
    assert await repo.get_key_by_hash("h") == {"keyId": "k"}
    assert await repo.delete_key("u", "k") is True
    _assert_off_loop(repo.table, "put_item", "update_item", "get_item", "query", "delete_item")


@pytest.mark.asyncio
async def test_user_repository_reads_and_writes_off_the_loop():
    repo = UserRepository(table_name="t")
    repo.table = _RecordingTable(
        get_item={},
        query={"Items": [], "Count": 0},
    )
    profile = _profile()
    assert await repo.get_user("u1") is None
    assert await repo.get_user_by_user_id("u1") is None
    assert await repo.get_users_by_email("a@example.com") == []
    await repo.create_user(profile)
    await repo.update_user(profile)
    assert await repo.list_users_by_domain("example.com") == ([], None)
    assert await repo.list_users_by_status("active") == ([], None)
    assert await repo.count_active_users() == 0
    _assert_off_loop(repo.table, "get_item", "query", "put_item")


def test_query_users_by_status_stays_synchronous_for_its_sync_callers():
    """The sync entry point is unchanged; only the async wrapper moved to a thread."""
    repo = UserRepository(table_name="t")
    repo.table = _RecordingTable(query={"Items": []})
    assert repo.query_users_by_status("active") == ([], None)
    assert repo.table.threads["query"] == threading.get_ident()
