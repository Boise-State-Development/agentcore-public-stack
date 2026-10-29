"""Device-grant repository tests against moto-backed DynamoDB.

The properties worth pinning down are the concurrency ones. A device grant is
a bearer credential handed over exactly once, so "two polls both got the
session id" and "a browser approved the wrong CLI's grant" are the failures
that matter — not CRUD round-tripping.

Uses the shared ``aws`` fixture (``tests/shared/conftest.py``) for mock_aws.
"""

from __future__ import annotations

import asyncio
import time

import boto3
import pytest
from botocore.exceptions import ClientError

from apis.shared.auth.device_grants.models import (
    GRANT_TTL_SECONDS,
    DeviceGrant,
    GrantStatus,
    generate_device_code,
    generate_user_code,
    hash_device_code,
)
from apis.shared.auth.device_grants.repository import DeviceGrantRepository

TABLE_NAME = "test-device-grants"
AWS_REGION = "us-east-1"


@pytest.fixture
def table(aws):
    """The PK/SK table shape the BFF sessions table already provides."""
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb.Table(TABLE_NAME)


@pytest.fixture
def repository(table) -> DeviceGrantRepository:
    return DeviceGrantRepository(table_name=TABLE_NAME)


def make_grant(**overrides: object) -> DeviceGrant:
    now = int(time.time())
    fields: dict[str, object] = {
        "device_code_hash": hash_device_code(generate_device_code()),
        "user_code": generate_user_code(),
        "status": GrantStatus.PENDING,
        "created_at": now,
        "expires_at": now + GRANT_TTL_SECONDS,
    }
    fields.update(overrides)
    return DeviceGrant(**fields)  # type: ignore[arg-type]


# =====================================================================
# Create and the two lookup paths
# =====================================================================


class TestCreateAndLookup:
    @pytest.mark.asyncio
    async def test_round_trips_every_field(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)

        fetched = await repository.get_by_device_code_hash(grant.device_code_hash)
        assert fetched is not None
        assert fetched.device_code_hash == grant.device_code_hash
        assert fetched.status is GrantStatus.PENDING
        assert fetched.created_at == grant.created_at
        assert fetched.expires_at == grant.expires_at
        assert fetched.session_id is None
        assert fetched.user_id is None
        assert fetched.poll_count == 0

    @pytest.mark.asyncio
    async def test_unknown_device_code_is_none(self, repository) -> None:
        assert await repository.get_by_device_code_hash("nope") is None

    @pytest.mark.asyncio
    async def test_unknown_user_code_is_none(self, repository) -> None:
        assert await repository.get_by_user_code("CDFG-HJKM") is None

    @pytest.mark.asyncio
    async def test_user_code_resolves_to_the_same_grant(self, repository) -> None:
        """The browser leg's lookup must land on the CLI's grant."""
        grant = make_grant()
        await repository.create(grant)

        fetched = await repository.get_by_user_code(grant.user_code)
        assert fetched is not None
        assert fetched.device_code_hash == grant.device_code_hash

    @pytest.mark.asyncio
    async def test_user_code_lookup_tolerates_human_typing(self, repository) -> None:
        """Lower case, missing hyphen and stray whitespace all resolve."""
        grant = make_grant(user_code="CDFG-HJKM")
        await repository.create(grant)

        for typed in ("cdfg-hjkm", "CDFGHJKM", "  cdfg hjkm  ", "CdFg-HjKm"):
            fetched = await repository.get_by_user_code(typed)
            assert fetched is not None, typed
            assert fetched.device_code_hash == grant.device_code_hash

    @pytest.mark.asyncio
    async def test_duplicate_user_code_is_refused(self, repository) -> None:
        """A collision must fail loudly, not retarget the first CLI's grant.

        The user-code alphabet is small enough that this is reachable, and
        silently overwriting the pointer would send a browser approval to the
        wrong terminal.
        """
        first = make_grant(user_code="CDFG-HJKM")
        await repository.create(first)

        second = make_grant(user_code="CDFG-HJKM")
        with pytest.raises(ClientError):
            await repository.create(second)

        # The original pointer still resolves to the original grant.
        fetched = await repository.get_by_user_code("CDFG-HJKM")
        assert fetched is not None
        assert fetched.device_code_hash == first.device_code_hash

    @pytest.mark.asyncio
    async def test_failed_create_leaves_no_partial_state(self, repository, table) -> None:
        """The pair is transactional: a refused create writes neither item."""
        first = make_grant(user_code="CDFG-HJKM")
        await repository.create(first)

        second = make_grant(user_code="CDFG-HJKM")
        with pytest.raises(ClientError):
            await repository.create(second)

        # The second grant's own item must not exist either.
        assert await repository.get_by_device_code_hash(second.device_code_hash) is None

    @pytest.mark.asyncio
    async def test_ttl_is_written_for_dynamo_reaping(self, repository, table) -> None:
        grant = make_grant()
        await repository.create(grant)

        item = table.get_item(Key={"PK": f"DEVICE-GRANT#{grant.device_code_hash}", "SK": "META"})["Item"]
        assert int(item["ttl"]) == grant.expires_at

        pointer = table.get_item(Key={"PK": f"DEVICE-USERCODE#{grant.user_code.replace('-', '')}", "SK": "META"})["Item"]
        assert int(pointer["ttl"]) == grant.expires_at

    @pytest.mark.asyncio
    async def test_grant_items_are_invisible_to_session_keys(self, repository, table) -> None:
        """Sharing the BFF sessions table is only safe if the prefixes differ."""
        grant = make_grant()
        await repository.create(grant)

        scanned = table.scan()["Items"]
        assert scanned, "expected the grant and pointer items"
        assert all(not i["PK"].startswith("SESSION#") for i in scanned)


# =====================================================================
# Expiry is the caller's business
# =====================================================================


class TestExpiryVisibility:
    @pytest.mark.asyncio
    async def test_expired_grant_is_still_readable(self, repository) -> None:
        """Deliberate divergence from SessionRepository.get.

        RFC 8628 needs ``expired_token`` distinguished from an unknown grant,
        which is impossible if the repository hides the row.
        """
        grant = make_grant(expires_at=int(time.time()) - 1)
        await repository.create(grant)

        fetched = await repository.get_by_device_code_hash(grant.device_code_hash)
        assert fetched is not None
        assert fetched.is_expired()
        assert not fetched.is_claimable()
        assert not fetched.is_approvable()


# =====================================================================
# approve
# =====================================================================


class TestApprove:
    @pytest.mark.asyncio
    async def test_attaches_session_and_user(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)

        assert await repository.approve(grant.device_code_hash, session_id="sess-001", user_id="user-sub-1")

        fetched = await repository.get_by_device_code_hash(grant.device_code_hash)
        assert fetched is not None
        assert fetched.status is GrantStatus.APPROVED
        assert fetched.session_id == "sess-001"
        assert fetched.user_id == "user-sub-1"
        assert fetched.is_claimable()

    @pytest.mark.asyncio
    async def test_missing_grant_is_false(self, repository) -> None:
        assert not await repository.approve("nope", session_id="s", user_id="u")

    @pytest.mark.asyncio
    async def test_second_approval_is_refused(self, repository) -> None:
        """Only a pending grant may be approved, so a session cannot be swapped."""
        grant = make_grant()
        await repository.create(grant)
        await repository.approve(grant.device_code_hash, session_id="sess-001", user_id="user-1")

        assert not await repository.approve(grant.device_code_hash, session_id="sess-002", user_id="attacker")

        fetched = await repository.get_by_device_code_hash(grant.device_code_hash)
        assert fetched is not None
        assert fetched.session_id == "sess-001"

    @pytest.mark.asyncio
    async def test_expired_grant_cannot_be_approved(self, repository) -> None:
        grant = make_grant(expires_at=int(time.time()) - 1)
        await repository.create(grant)

        assert not await repository.approve(grant.device_code_hash, session_id="sess-001", user_id="user-1")

    @pytest.mark.asyncio
    async def test_denied_grant_cannot_be_approved(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)
        await repository.deny(grant.device_code_hash)

        assert not await repository.approve(grant.device_code_hash, session_id="sess-001", user_id="user-1")


# =====================================================================
# claim — the single-use gate
# =====================================================================


class TestClaim:
    @pytest.mark.asyncio
    async def test_returns_session_id_once(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)
        await repository.approve(grant.device_code_hash, session_id="sess-001", user_id="user-1")

        assert await repository.claim(grant.device_code_hash) == "sess-001"

        fetched = await repository.get_by_device_code_hash(grant.device_code_hash)
        assert fetched is not None
        assert fetched.status is GrantStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_second_claim_returns_none(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)
        await repository.approve(grant.device_code_hash, session_id="sess-001", user_id="user-1")
        await repository.claim(grant.device_code_hash)

        assert await repository.claim(grant.device_code_hash) is None

    @pytest.mark.asyncio
    async def test_concurrent_claims_yield_exactly_one_winner(self, repository) -> None:
        """The property the whole conditional-update design exists for.

        Two polls landing together must not both walk away with the session
        value, or two CLIs share one session and tumble each other's refresh.
        """
        grant = make_grant()
        await repository.create(grant)
        await repository.approve(grant.device_code_hash, session_id="sess-001", user_id="user-1")

        results = await asyncio.gather(*(repository.claim(grant.device_code_hash) for _ in range(8)))

        assert results.count("sess-001") == 1
        assert results.count(None) == 7

    @pytest.mark.asyncio
    async def test_pending_grant_is_not_claimable(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)
        assert await repository.claim(grant.device_code_hash) is None

    @pytest.mark.asyncio
    async def test_missing_grant_is_not_claimable(self, repository) -> None:
        assert await repository.claim("nope") is None

    @pytest.mark.asyncio
    async def test_expired_grant_is_not_claimable(self, repository) -> None:
        """Approved just before the deadline, polled just after."""
        now = int(time.time())
        grant = make_grant(expires_at=now + 5)
        await repository.create(grant)
        await repository.approve(
            grant.device_code_hash,
            session_id="sess-001",
            user_id="user-1",
            now=now,
        )

        assert await repository.claim(grant.device_code_hash, now=now + 10) is None

    @pytest.mark.asyncio
    async def test_denied_grant_is_not_claimable(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)
        await repository.deny(grant.device_code_hash)
        assert await repository.claim(grant.device_code_hash) is None


# =====================================================================
# deny
# =====================================================================


class TestDeny:
    @pytest.mark.asyncio
    async def test_marks_denied(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)

        assert await repository.deny(grant.device_code_hash)

        fetched = await repository.get_by_device_code_hash(grant.device_code_hash)
        assert fetched is not None
        assert fetched.status is GrantStatus.DENIED

    @pytest.mark.asyncio
    async def test_cannot_deny_an_approved_grant(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)
        await repository.approve(grant.device_code_hash, session_id="sess-001", user_id="user-1")
        assert not await repository.deny(grant.device_code_hash)

    @pytest.mark.asyncio
    async def test_missing_grant_is_false(self, repository) -> None:
        assert not await repository.deny("nope")


# =====================================================================
# record_poll
# =====================================================================


class TestRecordPoll:
    @pytest.mark.asyncio
    async def test_stamps_time_and_counts(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)
        now = int(time.time())

        await repository.record_poll(grant.device_code_hash, now=now)

        fetched = await repository.get_by_device_code_hash(grant.device_code_hash)
        assert fetched is not None
        assert fetched.last_polled_at == now
        assert fetched.poll_count == 1

    @pytest.mark.asyncio
    async def test_counts_accumulate_across_concurrent_polls(self, repository) -> None:
        """``ADD`` rather than read-modify-write, so no count is lost."""
        grant = make_grant()
        await repository.create(grant)

        await asyncio.gather(*(repository.record_poll(grant.device_code_hash) for _ in range(6)))

        fetched = await repository.get_by_device_code_hash(grant.device_code_hash)
        assert fetched is not None
        assert fetched.poll_count == 6

    @pytest.mark.asyncio
    async def test_feeds_the_slow_down_decision(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)
        now = int(time.time())
        await repository.record_poll(grant.device_code_hash, now=now)

        fetched = await repository.get_by_device_code_hash(grant.device_code_hash)
        assert fetched is not None
        assert fetched.should_slow_down(now + 1)
        assert not fetched.should_slow_down(now + 60)

    @pytest.mark.asyncio
    async def test_missing_grant_does_not_raise(self, repository) -> None:
        """Best-effort: a failed stamp must never fail the poll."""
        await repository.record_poll("nope")


# =====================================================================
# delete
# =====================================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_removes_grant_and_pointer(self, repository) -> None:
        grant = make_grant()
        await repository.create(grant)

        await repository.delete(grant)

        assert await repository.get_by_device_code_hash(grant.device_code_hash) is None
        assert await repository.get_by_user_code(grant.user_code) is None


# =====================================================================
# Disabled repository
# =====================================================================


class TestDisabled:
    @pytest.mark.asyncio
    async def test_is_inert_without_a_table(self) -> None:
        """No table configured must be a no-op, not an AWS call."""
        repo = DeviceGrantRepository(table_name="")
        assert repo.enabled is False

        grant = make_grant()
        await repo.create(grant)
        assert await repo.get_by_device_code_hash(grant.device_code_hash) is None
        assert await repo.get_by_user_code(grant.user_code) is None
        assert not await repo.approve(grant.device_code_hash, session_id="s", user_id="u")
        assert await repo.claim(grant.device_code_hash) is None
        assert not await repo.deny(grant.device_code_hash)
        await repo.record_poll(grant.device_code_hash)
        await repo.delete(grant)

    def test_prefers_the_dedicated_table_when_configured(self, monkeypatch) -> None:
        """Moving these items to their own table stays a config change."""
        monkeypatch.setenv("BFF_SESSIONS_TABLE_NAME", "shared-sessions")
        monkeypatch.setenv("DEVICE_GRANTS_TABLE_NAME", "dedicated-grants")
        assert DeviceGrantRepository()._table_name == "dedicated-grants"

    def test_falls_back_to_the_bff_sessions_table(self, monkeypatch) -> None:
        monkeypatch.delenv("DEVICE_GRANTS_TABLE_NAME", raising=False)
        monkeypatch.setenv("BFF_SESSIONS_TABLE_NAME", "shared-sessions")
        assert DeviceGrantRepository()._table_name == "shared-sessions"
