"""Tests for the headless-grant record + grant-backed bearer minting.

The grant record is the durable "act as me" consent for headless runs
(``apis/shared/harness/grants.py``): create-on-enable from an attended
session, per-owner lookup via the sparse ``HeadlessGrantUserIndex`` GSI
(replacing the spike's BFF-table Scan), and total revocation (the stored
refresh token is deleted in the revoke write).
"""

from __future__ import annotations

import time

import pytest
from botocore.exceptions import ClientError

from apis.shared.harness.auth import CognitoRefreshBearerAuth, HeadlessAuthError
from apis.shared.harness.grants import (
    HEADLESS_GRANT_MAX_AGE_DAYS,
    HeadlessGrant,
    HeadlessGrantService,
)
from apis.shared.sessions_bff.refresh import CognitoRefreshError, RefreshResult

NOW = int(time.time())
MAX_AGE_SECONDS = HEADLESS_GRANT_MAX_AGE_DAYS * 24 * 60 * 60


class FakeTable:
    """Duck-typed DynamoDB Table capturing writes; query returns canned pages."""

    def __init__(self, query_items=None):
        self.query_items = list(query_items or [])
        self.put_items: list[dict] = []
        self.update_calls: list[dict] = []
        self.query_kwargs: dict = {}
        self.update_error: Exception | None = None

    def query(self, **kwargs):
        self.query_kwargs = kwargs
        return {"Items": list(self.query_items)}

    def put_item(self, Item):
        self.put_items.append(Item)
        # Newest-first, as the descending GSI query would return it.
        self.query_items.insert(0, Item)

    def update_item(self, **kwargs):
        if self.update_error is not None:
            raise self.update_error
        self.update_calls.append(kwargs)


def _service(table: FakeTable) -> HeadlessGrantService:
    service = HeadlessGrantService(table_name="fake-table")
    service._table = table
    return service


def _grant_item(
    *,
    grant_id: str = "hlg-abc",
    user_id: str = "user-1",
    status: str = "active",
    created_at: int = NOW - 100,
    ttl: int = NOW + 1000,
) -> dict:
    return {
        "PK": f"HEADLESS-GRANT#{grant_id}",
        "SK": "META",
        "grant_id": grant_id,
        "grant_user_id": user_id,
        "username": "user1",
        "cognito_refresh_token": "rt-stored",
        "status": status,
        "created_at": created_at,
        "updated_at": created_at,
        "token_issued_at": created_at,
        "ttl": ttl,
    }


# ---------------------------------------------------------------------------
# HeadlessGrantService
# ---------------------------------------------------------------------------


class TestEnable:
    @pytest.mark.asyncio
    async def test_creates_grant_with_sparse_gsi_key_and_login_anchored_ttl(self):
        table = FakeTable()
        service = _service(table)
        issued_at = NOW - 3600  # the login that produced the token

        grant = await service.enable(
            user_id="user-1",
            username="user1",
            refresh_token="rt-1",
            token_issued_at=issued_at,
        )

        assert grant.grant_id.startswith("hlg-")
        item = table.put_items[0]
        assert item["PK"] == f"HEADLESS-GRANT#{grant.grant_id}"
        assert item["SK"] == "META"
        # Sparse GSI partition key — only grant items carry it.
        assert item["grant_user_id"] == "user-1"
        assert item["cognito_refresh_token"] == "rt-1"
        assert item["status"] == "active"
        # "Must have logged in within N days": TTL anchors to the login.
        assert item["ttl"] == issued_at + MAX_AGE_SECONDS

    @pytest.mark.asyncio
    async def test_renews_existing_active_grant_in_place(self):
        table = FakeTable([_grant_item()])
        service = _service(table)

        grant = await service.enable(
            user_id="user-1", username="user1", refresh_token="rt-new"
        )

        assert grant.grant_id == "hlg-abc"  # stable id for audit continuity
        assert not table.put_items  # renew, not a second record
        (call,) = table.update_calls
        assert call["Key"] == {"PK": "HEADLESS-GRANT#hlg-abc", "SK": "META"}
        assert call["ExpressionAttributeValues"][":rt"] == "rt-new"
        assert grant.cognito_refresh_token == "rt-new"

    @pytest.mark.asyncio
    async def test_max_age_days_is_env_overridable(self, monkeypatch):
        monkeypatch.setenv("HEADLESS_GRANT_MAX_AGE_DAYS", "7")
        table = FakeTable()
        service = _service(table)

        await service.enable(
            user_id="user-1",
            username="user1",
            refresh_token="rt-1",
            token_issued_at=NOW,
        )

        assert table.put_items[0]["ttl"] == NOW + 7 * 24 * 60 * 60


class TestGetActiveGrant:
    @pytest.mark.asyncio
    async def test_returns_newest_active_grant(self):
        table = FakeTable([_grant_item()])
        service = _service(table)

        grant = await service.get_active_grant("user-1")

        assert isinstance(grant, HeadlessGrant)
        assert grant.grant_id == "hlg-abc"
        assert grant.is_active
        # The lookup is a GSI query, not a Scan.
        assert table.query_kwargs["IndexName"] == "HeadlessGrantUserIndex"
        assert table.query_kwargs["ScanIndexForward"] is False

    @pytest.mark.asyncio
    async def test_skips_revoked_and_expired_grants(self):
        table = FakeTable(
            [
                _grant_item(grant_id="hlg-revoked", status="revoked"),
                # TTL passed but DynamoDB hasn't swept yet — defense in depth.
                _grant_item(grant_id="hlg-expired", ttl=NOW - 5),
                _grant_item(grant_id="hlg-live", created_at=NOW - 999),
            ]
        )
        service = _service(table)

        grant = await service.get_active_grant("user-1")

        assert grant is not None and grant.grant_id == "hlg-live"

    @pytest.mark.asyncio
    async def test_returns_none_when_user_has_no_grants(self):
        service = _service(FakeTable())
        assert await service.get_active_grant("user-1") is None


class TestRevoke:
    @pytest.mark.asyncio
    async def test_revoke_removes_the_stored_credential(self):
        table = FakeTable([_grant_item()])
        service = _service(table)

        assert await service.revoke("user-1") is True
        (call,) = table.update_calls
        assert "REMOVE cognito_refresh_token" in call["UpdateExpression"]
        assert call["ExpressionAttributeValues"][":revoked"] == "revoked"

    @pytest.mark.asyncio
    async def test_revoke_is_false_when_nothing_active(self):
        table = FakeTable([_grant_item(status="revoked")])
        service = _service(table)

        assert await service.revoke("user-1") is False
        assert not table.update_calls


class TestRecordUse:
    @pytest.mark.asyncio
    async def test_touch_failure_never_raises(self):
        table = FakeTable()
        table.update_error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "UpdateItem",
        )
        service = _service(table)

        await service.record_use("hlg-abc")  # must not raise


# ---------------------------------------------------------------------------
# CognitoRefreshBearerAuth (grant-backed mint)
# ---------------------------------------------------------------------------


class FakeGrants:
    def __init__(self, grant: HeadlessGrant | None):
        self.grant = grant
        self.persisted: list[tuple[str, str]] = []
        self.used: list[str] = []

    async def get_active_grant(self, user_id: str):
        return self.grant

    async def persist_rotated_refresh_token(self, grant_id: str, refresh_token: str):
        self.persisted.append((grant_id, refresh_token))

    async def record_use(self, grant_id: str):
        self.used.append(grant_id)


class FakeRefreshClient:
    def __init__(self, result: RefreshResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    async def refresh(self, *, username: str, refresh_token: str) -> RefreshResult:
        self.calls.append({"username": username, "refresh_token": refresh_token})
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _live_grant() -> HeadlessGrant:
    return HeadlessGrant(
        grant_id="hlg-abc",
        user_id="user-1",
        username="user1",
        cognito_refresh_token="rt-stored",
        status="active",
        created_at=NOW - 100,
        updated_at=NOW - 100,
        token_issued_at=NOW - 100,
        ttl=NOW + 1000,
    )


class TestCognitoRefreshBearerAuth:
    @pytest.mark.asyncio
    async def test_mints_from_the_grant_and_records_use(self):
        grants = FakeGrants(_live_grant())
        refresh = FakeRefreshClient(
            RefreshResult(
                access_token="at-fresh",
                refresh_token="rt-stored",  # no rotation on this pool
                id_token=None,
                access_token_exp=NOW + 3600,
            )
        )
        auth = CognitoRefreshBearerAuth(grants=grants, refresh_client=refresh)

        token = await auth.mint_bearer_for_user("user-1")

        assert token == "at-fresh"
        assert refresh.calls == [{"username": "user1", "refresh_token": "rt-stored"}]
        assert grants.used == ["hlg-abc"]
        assert grants.persisted == []

    @pytest.mark.asyncio
    async def test_no_active_grant_raises_headless_auth_error(self):
        auth = CognitoRefreshBearerAuth(
            grants=FakeGrants(None), refresh_client=FakeRefreshClient()
        )

        with pytest.raises(HeadlessAuthError, match="No active headless grant"):
            await auth.mint_bearer_for_user("user-1")

    @pytest.mark.asyncio
    async def test_cognito_refusal_raises_headless_auth_error(self):
        auth = CognitoRefreshBearerAuth(
            grants=FakeGrants(_live_grant()),
            refresh_client=FakeRefreshClient(error=CognitoRefreshError("revoked")),
        )

        with pytest.raises(HeadlessAuthError, match="refused the refresh exchange"):
            await auth.mint_bearer_for_user("user-1")

    @pytest.mark.asyncio
    async def test_rotated_refresh_token_is_persisted_onto_the_grant(self):
        grants = FakeGrants(_live_grant())
        refresh = FakeRefreshClient(
            RefreshResult(
                access_token="at-fresh",
                refresh_token="rt-rotated",
                id_token=None,
                access_token_exp=NOW + 3600,
            )
        )
        auth = CognitoRefreshBearerAuth(grants=grants, refresh_client=refresh)

        await auth.mint_bearer_for_user("user-1")

        assert grants.persisted == [("hlg-abc", "rt-rotated")]
