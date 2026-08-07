"""Device-grant service tests.

Real repository against moto, a real cookie codec with the cipher injected
(so no Secrets Manager call), and a real session repository. The interesting
assertions are about *ordering* — throttle before status, seal before claim,
claim decides the winner — so stubbing the repository would test nothing.
"""

from __future__ import annotations

import asyncio
import time

import boto3
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from apis.shared.auth.device_grants.models import (
    GRANT_TTL_SECONDS,
    MIN_POLL_GAP_SECONDS,
    POLL_INTERVAL_SECONDS,
    DeviceTokenResponse,
    GrantStatus,
    hash_device_code,
    normalise_user_code,
)
from apis.shared.auth.device_grants.repository import DeviceGrantRepository
from apis.shared.auth.device_grants.service import (
    ApprovalOutcome,
    DeviceGrantService,
    derive_verification_uri,
)
from apis.shared.sessions_bff.cookie import CookieCodec
from apis.shared.sessions_bff.models import SessionRecord
from apis.shared.sessions_bff.repository import SessionRepository

TABLE_NAME = "test-device-grant-service"
AWS_REGION = "us-east-1"
VERIFY_URI = "https://example.test/api/auth/cli/verify"


@pytest.fixture
def table(aws):
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
def codec() -> CookieCodec:
    """A codec with a pre-injected cipher — no Secrets Manager round trip."""
    c = CookieCodec(
        kms_key_arn="arn:aws:kms:us-east-1:000000000000:key/test",
        data_key_secret_arn="arn:aws:secretsmanager:us-east-1:000000000000:secret:test",
    )
    c._cipher = AESGCM(b"\x01" * 32)
    return c


@pytest.fixture
def grants(table) -> DeviceGrantRepository:
    return DeviceGrantRepository(table_name=TABLE_NAME)


@pytest.fixture
def sessions(table) -> SessionRepository:
    return SessionRepository(table_name=TABLE_NAME)


@pytest.fixture
def service(grants, sessions, codec) -> DeviceGrantService:
    return DeviceGrantService(
        repository=grants,
        session_repository=sessions,
        codec=codec,
        verification_uri=VERIFY_URI,
    )


@pytest.fixture
def seeded_session(sessions):
    """Factory that persists a real BFF session row and returns it."""

    async def _make(session_id: str = "sess-cli-001", **overrides) -> SessionRecord:
        now = int(time.time())
        fields = {
            "session_id": session_id,
            "user_id": "user-sub-1",
            "username": "alice",
            "cognito_access_token": "access.token",
            "cognito_refresh_token": "refresh.token",
            "id_token": "id.token",
            "access_token_exp": now + 3600,
            "csrf_secret": "csrf-secret",
            "created_at": now,
            "last_seen_at": now,
            "ttl": now + 28800,
        }
        fields.update(overrides)
        record = SessionRecord(**fields)  # type: ignore[arg-type]
        await sessions.put(record)
        return record

    return _make


async def _authorize_and_approve(service, seeded_session, session_id="sess-cli-001"):
    """Drive the flow to the point where a poll should hand over the session."""
    record = await seeded_session(session_id)
    auth = await service.authorize()
    outcome = await service.approve(
        user_code=auth.user_code,
        session_id=record.session_id,
        user_id=record.user_id,
    )
    assert outcome is ApprovalOutcome.APPROVED
    return auth, record


# =====================================================================
# derive_verification_uri
# =====================================================================


class TestDeriveVerificationUri:
    def test_uses_the_callback_sibling(self, monkeypatch) -> None:
        monkeypatch.delenv("BFF_CLI_VERIFICATION_URL", raising=False)
        assert derive_verification_uri("https://dev.boisestate.ai/api/auth/callback") == "https://dev.boisestate.ai/api/auth/cli/verify"

    def test_handles_a_root_level_callback(self, monkeypatch) -> None:
        monkeypatch.delenv("BFF_CLI_VERIFICATION_URL", raising=False)
        assert derive_verification_uri("https://host/auth/callback") == "https://host/auth/cli/verify"

    def test_drops_query_and_fragment(self, monkeypatch) -> None:
        monkeypatch.delenv("BFF_CLI_VERIFICATION_URL", raising=False)
        assert derive_verification_uri("https://host/api/auth/callback?x=1#frag") == "https://host/api/auth/cli/verify"

    def test_env_override_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("BFF_CLI_VERIFICATION_URL", "https://other/verify/")
        assert derive_verification_uri("https://host/api/auth/callback") == "https://other/verify"

    def test_none_without_a_callback_url(self, monkeypatch) -> None:
        monkeypatch.delenv("BFF_CLI_VERIFICATION_URL", raising=False)
        assert derive_verification_uri(None) is None


# =====================================================================
# authorize
# =====================================================================


class TestAuthorize:
    @pytest.mark.asyncio
    async def test_returns_a_complete_rfc8628_payload(self, service) -> None:
        auth = await service.authorize()

        assert auth.verification_uri == VERIFY_URI
        assert auth.expires_in == GRANT_TTL_SECONDS
        assert auth.interval == POLL_INTERVAL_SECONDS
        assert len(auth.device_code) >= 40
        assert "-" in auth.user_code

    @pytest.mark.asyncio
    async def test_complete_uri_prefills_the_user_code(self, service) -> None:
        auth = await service.authorize()
        assert auth.verification_uri_complete == f"{VERIFY_URI}?user_code={auth.user_code}"

    @pytest.mark.asyncio
    async def test_persists_a_pending_grant_the_cli_can_poll(self, service, grants) -> None:
        auth = await service.authorize()

        stored = await grants.get_by_device_code_hash(hash_device_code(auth.device_code))
        assert stored is not None
        assert stored.status is GrantStatus.PENDING
        assert stored.session_id is None

    @pytest.mark.asyncio
    async def test_device_code_is_never_stored_in_the_clear(self, service, table) -> None:
        """The whole point of hashing — a table dump must not be claimable."""
        auth = await service.authorize()

        blob = str(table.scan()["Items"])
        assert auth.device_code not in blob
        assert hash_device_code(auth.device_code) in blob

    @pytest.mark.asyncio
    async def test_user_code_lookup_finds_the_new_grant(self, service) -> None:
        auth = await service.authorize()
        found = await service.lookup_pending(user_code=auth.user_code)
        assert found is not None
        assert found.device_code_hash == hash_device_code(auth.device_code)

    @pytest.mark.asyncio
    async def test_concurrent_authorizes_all_succeed(self, service) -> None:
        """Distinct codes, so the create transaction never contends."""
        results = await asyncio.gather(*(service.authorize() for _ in range(10)))
        assert len({r.device_code for r in results}) == 10
        assert len({r.user_code for r in results}) == 10


# =====================================================================
# approve / deny
# =====================================================================


class TestApprove:
    @pytest.mark.asyncio
    async def test_attaches_the_session(self, service, grants, seeded_session) -> None:
        auth, record = await _authorize_and_approve(service, seeded_session)

        stored = await grants.get_by_device_code_hash(hash_device_code(auth.device_code))
        assert stored is not None
        assert stored.status is GrantStatus.APPROVED
        assert stored.session_id == record.session_id
        assert stored.user_id == record.user_id

    @pytest.mark.asyncio
    async def test_unknown_code_is_not_found(self, service) -> None:
        assert await service.approve(user_code="CDFG-HJKM", session_id="s", user_id="u") is ApprovalOutcome.NOT_FOUND

    @pytest.mark.asyncio
    async def test_tolerates_human_typing(self, service, seeded_session) -> None:
        record = await seeded_session()
        auth = await service.authorize()

        typed = auth.user_code.lower().replace("-", " ")
        outcome = await service.approve(user_code=typed, session_id=record.session_id, user_id=record.user_id)
        assert outcome is ApprovalOutcome.APPROVED

    @pytest.mark.asyncio
    async def test_expired_grant_reports_expired(self, service, seeded_session) -> None:
        record = await seeded_session()
        now = int(time.time())
        auth = await service.authorize(now=now)

        outcome = await service.approve(
            user_code=auth.user_code,
            session_id=record.session_id,
            user_id=record.user_id,
            now=now + GRANT_TTL_SECONDS + 1,
        )
        assert outcome is ApprovalOutcome.EXPIRED

    @pytest.mark.asyncio
    async def test_second_approval_cannot_swap_the_session(self, service, grants, seeded_session) -> None:
        """A second browser must not be able to retarget an approved grant."""
        auth, record = await _authorize_and_approve(service, seeded_session)

        outcome = await service.approve(
            user_code=auth.user_code,
            session_id="sess-attacker",
            user_id="attacker",
        )
        assert outcome is ApprovalOutcome.ALREADY_RESOLVED

        stored = await grants.get_by_device_code_hash(hash_device_code(auth.device_code))
        assert stored is not None
        assert stored.session_id == record.session_id

    @pytest.mark.asyncio
    async def test_concurrent_approvals_yield_one_winner(self, service, seeded_session) -> None:
        await seeded_session()
        auth = await service.authorize()

        outcomes = await asyncio.gather(
            *(
                service.approve(
                    user_code=auth.user_code,
                    session_id=f"sess-{i}",
                    user_id=f"user-{i}",
                )
                for i in range(6)
            )
        )
        assert outcomes.count(ApprovalOutcome.APPROVED) == 1

    @pytest.mark.asyncio
    async def test_lookup_pending_hides_a_resolved_grant(self, service, seeded_session) -> None:
        auth, _ = await _authorize_and_approve(service, seeded_session)
        assert await service.lookup_pending(user_code=auth.user_code) is None


class TestDeny:
    @pytest.mark.asyncio
    async def test_marks_denied(self, service, grants) -> None:
        auth = await service.authorize()

        assert await service.deny(user_code=auth.user_code) is ApprovalOutcome.DENIED

        stored = await grants.get_by_device_code_hash(hash_device_code(auth.device_code))
        assert stored is not None
        assert stored.status is GrantStatus.DENIED

    @pytest.mark.asyncio
    async def test_cannot_deny_an_approved_grant(self, service, seeded_session) -> None:
        auth, _ = await _authorize_and_approve(service, seeded_session)
        assert await service.deny(user_code=auth.user_code) is ApprovalOutcome.ALREADY_RESOLVED

    @pytest.mark.asyncio
    async def test_unknown_code_is_not_found(self, service) -> None:
        assert await service.deny(user_code="CDFG-HJKM") is ApprovalOutcome.NOT_FOUND


# =====================================================================
# poll — the ordering that matters
# =====================================================================


class TestPollPendingStates:
    @pytest.mark.asyncio
    async def test_unknown_device_code_is_invalid_grant(self, service) -> None:
        result = await service.poll(device_code="not-a-real-code")
        assert result.error == "invalid_grant"

    @pytest.mark.asyncio
    async def test_pending_grant_reports_authorization_pending(self, service) -> None:
        auth = await service.authorize()
        result = await service.poll(device_code=auth.device_code)
        assert result.error == "authorization_pending"

    @pytest.mark.asyncio
    async def test_denied_grant_reports_access_denied(self, service) -> None:
        auth = await service.authorize()
        await service.deny(user_code=auth.user_code)

        result = await service.poll(device_code=auth.device_code)
        assert result.error == "access_denied"

    @pytest.mark.asyncio
    async def test_expired_grant_reports_expired_token(self, service) -> None:
        now = int(time.time())
        auth = await service.authorize(now=now)

        result = await service.poll(device_code=auth.device_code, now=now + GRANT_TTL_SECONDS + 1)
        assert result.error == "expired_token"

    @pytest.mark.asyncio
    async def test_every_pending_response_carries_a_description(self, service) -> None:
        auth = await service.authorize()
        result = await service.poll(device_code=auth.device_code)
        assert result.error_description


class TestPollThrottle:
    @pytest.mark.asyncio
    async def test_a_fast_second_poll_is_slowed_down(self, service) -> None:
        now = int(time.time())
        auth = await service.authorize(now=now)

        first = await service.poll(device_code=auth.device_code, now=now)
        assert first.error == "authorization_pending"

        second = await service.poll(device_code=auth.device_code, now=now + 1)
        assert second.error == "slow_down"

    @pytest.mark.asyncio
    async def test_a_compliant_poll_is_answered_normally(self, service) -> None:
        now = int(time.time())
        auth = await service.authorize(now=now)

        await service.poll(device_code=auth.device_code, now=now)
        later = await service.poll(device_code=auth.device_code, now=now + POLL_INTERVAL_SECONDS)
        assert later.error == "authorization_pending"

    @pytest.mark.asyncio
    async def test_throttle_outranks_the_real_outcome(self, service, seeded_session) -> None:
        """slow_down is answered instead of the token, not in addition to it.

        This is the property that stops a tight loop from being a guessing
        amplifier, so an approved grant must still be withheld.

        The first poll happens *before* approval so it establishes a
        ``last_polled_at`` to throttle against without consuming the grant —
        a first poll against an already-approved grant would legitimately
        claim it, since there is no prior timestamp.
        """
        now = int(time.time())
        record = await seeded_session()
        auth = await service.authorize(now=now)

        opening = await service.poll(device_code=auth.device_code, now=now)
        assert opening.error == "authorization_pending"

        await service.approve(
            user_code=auth.user_code,
            session_id=record.session_id,
            user_id=record.user_id,
            now=now,
        )

        throttled = await service.poll(device_code=auth.device_code, now=now + 1)
        assert throttled.error == "slow_down"

        # Still claimable once the client behaves.
        ok = await service.poll(device_code=auth.device_code, now=now + 1 + MIN_POLL_GAP_SECONDS)
        assert isinstance(ok, DeviceTokenResponse)

    @pytest.mark.asyncio
    async def test_ignoring_the_interval_never_advances(self, service) -> None:
        """A client hammering every second stays throttled indefinitely."""
        now = int(time.time())
        auth = await service.authorize(now=now)
        await service.poll(device_code=auth.device_code, now=now)

        for offset in range(1, 6):
            result = await service.poll(device_code=auth.device_code, now=now + offset)
            assert result.error == "slow_down", offset


class TestPollClaim:
    @pytest.mark.asyncio
    async def test_returns_a_sealed_session_the_codec_can_unseal(self, service, codec, seeded_session) -> None:
        auth, record = await _authorize_and_approve(service, seeded_session)

        result = await service.poll(device_code=auth.device_code)
        assert isinstance(result, DeviceTokenResponse)
        assert result.user_id == record.user_id
        assert result.username == record.username
        assert result.expires_in > 0

        # The value must be exactly what SessionRefreshMiddleware will unseal.
        assert codec.unseal(result.session).session_id == record.session_id

    @pytest.mark.asyncio
    async def test_the_session_id_is_not_sent_in_the_clear(self, service, seeded_session) -> None:
        auth, record = await _authorize_and_approve(service, seeded_session)
        result = await service.poll(device_code=auth.device_code)
        assert isinstance(result, DeviceTokenResponse)
        assert record.session_id not in result.session

    @pytest.mark.asyncio
    async def test_grant_is_marked_claimed(self, service, grants, seeded_session) -> None:
        auth, _ = await _authorize_and_approve(service, seeded_session)
        await service.poll(device_code=auth.device_code)

        stored = await grants.get_by_device_code_hash(hash_device_code(auth.device_code))
        assert stored is not None
        assert stored.status is GrantStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_second_poll_after_claim_is_invalid(self, service, seeded_session) -> None:
        """Single use: even the legitimate client cannot re-collect."""
        now = int(time.time())
        auth, _ = await _authorize_and_approve(service, seeded_session)

        first = await service.poll(device_code=auth.device_code, now=now)
        assert isinstance(first, DeviceTokenResponse)

        second = await service.poll(device_code=auth.device_code, now=now + POLL_INTERVAL_SECONDS)
        assert second.error == "invalid_grant"

    @pytest.mark.asyncio
    async def test_concurrent_polls_hand_over_exactly_one_session(self, service, seeded_session) -> None:
        """The headline property. Two CLIs sharing one session would tumble
        each other's Cognito refresh, which the per-session lock exists to
        prevent — so it must never happen in the first place.

        The losers do not all report ``invalid_grant``: a peer's poll stamp
        may land first, in which case the throttle answers ``slow_down``
        before the claim is even attempted. Either way no session value is
        handed over, which is the invariant under test.
        """
        auth, _ = await _authorize_and_approve(service, seeded_session)

        results = await asyncio.gather(*(service.poll(device_code=auth.device_code) for _ in range(8)))

        tokens = [r for r in results if isinstance(r, DeviceTokenResponse)]
        losers = [r for r in results if not isinstance(r, DeviceTokenResponse)]

        assert len(tokens) == 1
        assert len(losers) == 7
        assert {r.error for r in losers} <= {"invalid_grant", "slow_down"}

    @pytest.mark.asyncio
    async def test_revoked_session_reports_expired_and_does_not_claim(self, service, grants, sessions, seeded_session) -> None:
        """If the session vanished, the grant must not be burned silently.

        The check happens before the claim so a transient problem leaves the
        grant usable rather than forcing the user to restart.
        """
        auth, record = await _authorize_and_approve(service, seeded_session)
        await sessions.delete(record.session_id)

        result = await service.poll(device_code=auth.device_code)
        assert result.error == "expired_token"

        stored = await grants.get_by_device_code_hash(hash_device_code(auth.device_code))
        assert stored is not None
        assert stored.status is GrantStatus.APPROVED  # not consumed

    @pytest.mark.asyncio
    async def test_expires_in_tracks_the_session_row(self, service, seeded_session) -> None:
        now = int(time.time())
        record = await seeded_session(ttl=now + 900)
        auth = await service.authorize(now=now)
        await service.approve(
            user_code=auth.user_code,
            session_id=record.session_id,
            user_id=record.user_id,
            now=now,
        )

        result = await service.poll(device_code=auth.device_code, now=now)
        assert isinstance(result, DeviceTokenResponse)
        assert result.expires_in == 900


# =====================================================================
# Wiring
# =====================================================================


class TestEnabled:
    def test_requires_every_dependency(self, grants, sessions, codec) -> None:
        assert DeviceGrantService(
            repository=grants,
            session_repository=sessions,
            codec=codec,
            verification_uri=VERIFY_URI,
        ).enabled

    def test_disabled_without_a_verification_uri(self, grants, sessions, codec, monkeypatch) -> None:
        monkeypatch.delenv("BFF_CLI_VERIFICATION_URL", raising=False)
        monkeypatch.delenv("BFF_AUTH_CALLBACK_URL", raising=False)
        assert not DeviceGrantService(repository=grants, session_repository=sessions, codec=codec).enabled

    def test_disabled_without_a_grants_table(self, sessions, codec) -> None:
        assert not DeviceGrantService(
            repository=DeviceGrantRepository(table_name=""),
            session_repository=sessions,
            codec=codec,
            verification_uri=VERIFY_URI,
        ).enabled

    def test_disabled_without_a_sessions_table(self, grants, codec) -> None:
        assert not DeviceGrantService(
            repository=grants,
            session_repository=SessionRepository(table_name=""),
            codec=codec,
            verification_uri=VERIFY_URI,
        ).enabled


class TestGrantAndSessionItemsCoexist:
    @pytest.mark.asyncio
    async def test_sharing_one_table_keeps_the_keyspaces_separate(self, service, table, seeded_session) -> None:
        """Device grants live in the BFF sessions table; prove no collision."""
        auth, record = await _authorize_and_approve(service, seeded_session)

        keys = {i["PK"] for i in table.scan()["Items"]}
        assert f"SESSION#{record.session_id}" in keys
        assert f"DEVICE-GRANT#{hash_device_code(auth.device_code)}" in keys
        assert f"DEVICE-USERCODE#{normalise_user_code(auth.user_code)}" in keys
