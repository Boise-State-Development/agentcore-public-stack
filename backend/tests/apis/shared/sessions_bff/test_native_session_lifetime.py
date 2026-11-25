"""A native session never slides on idle (docs/specs/native-app-auth.md D4).

D4 names three places that must honour `client = "native"`, and missing any
one silently returns the phone to the browser's 8-hour idle window:

1. `_maybe_slide` — records `last_seen_at`, writes the unchanged cap.
2. the refresh path's `new_ttl` — the cap, not `now + session_ttl`.
3. the refresh path's record rebuild — must carry `client`, or the *cached*
   record loses it at the first token refresh and every later slide is a
   browser slide.

Plus the repository round-trip and the handoff rows in the DynamoDB state
store, both against moto.
"""

from __future__ import annotations

import dataclasses
import time
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.shared.auth.state_store import DynamoDBStateStore, NativeHandoff, OIDCStateData
from apis.shared.sessions_bff import lock as lock_module
from apis.shared.sessions_bff.cache import SessionCache
from apis.shared.sessions_bff.models import NATIVE_CLIENT, CookiePayload
from apis.shared.sessions_bff.refresh import RefreshResult

from .test_session_refresh_header import _bff, _build_app
from .test_session_refresh_middleware import _enabled_config, _make_codec, _make_record

ABSOLUTE = 30 * 24 * 3600


@pytest.fixture(autouse=True)
def _reset_session_locks() -> None:
    lock_module._reset_for_tests()


def _native_record(**overrides):
    now = int(time.time())
    created = now - 3 * 24 * 3600  # signed in three days ago
    record = _make_record(**{k: v for k, v in overrides.items() if k == "access_token_exp"})
    return dataclasses.replace(
        record,
        client=NATIVE_CLIENT,
        device_model="iPhone17,1",
        app_version="1.0",
        created_at=created,
        last_seen_at=now - 600,  # past the throttle
        ttl=created + ABSOLUTE,
    )


def _wait_for(mock: AsyncMock, client: TestClient, sealed: str) -> None:
    """The slide write is fire-and-forget; poll inside the client's portal."""
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and mock.await_count == 0:
        time.sleep(0.01)
    if mock.await_count == 0:
        client.get("/echo", headers=_bff(sealed))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and mock.await_count == 0:
            time.sleep(0.01)


class TestNoSlide:
    def test_slide_records_last_seen_but_keeps_the_cap(self) -> None:
        record = _native_record()
        repo = AsyncMock()
        repo.get.return_value = record
        codec = _make_codec()
        app = _build_app(
            config=_enabled_config(), repository=repo, codec=codec, refresh_client=MagicMock()
        )
        sealed = codec.seal(CookiePayload(session_id=record.session_id))

        with TestClient(app) as client:
            assert client.get("/echo", headers=_bff(sealed)).json()["has_session"]
            _wait_for(repo.touch_last_seen, client, sealed)

        repo.touch_last_seen.assert_awaited()
        kwargs = repo.touch_last_seen.await_args.kwargs
        assert kwargs["ttl"] == record.created_at + ABSOLUTE
        assert kwargs["last_seen_at"] >= int(time.time()) - 5

    def test_a_browser_row_still_slides_on_the_idle_window(self) -> None:
        """Regression guard for the branch: only native rows are pinned."""
        record = _make_record()
        record.last_seen_at = int(time.time()) - 600
        repo = AsyncMock()
        repo.get.return_value = record
        codec = _make_codec()
        app = _build_app(
            config=_enabled_config(), repository=repo, codec=codec, refresh_client=MagicMock()
        )
        sealed = codec.seal(CookiePayload(session_id=record.session_id))

        with TestClient(app) as client:
            client.get("/echo", headers=_bff(sealed))
            _wait_for(repo.touch_last_seen, client, sealed)

        ttl = repo.touch_last_seen.await_args.kwargs["ttl"]
        assert abs(ttl - (int(time.time()) + 28800)) <= 5

    def test_a_token_refresh_writes_the_cap_and_keeps_the_record_native(self) -> None:
        now = int(time.time())
        record = _native_record(access_token_exp=now + 10)  # inside the leeway
        repo = AsyncMock()
        repo.get.return_value = record
        repo.try_acquire_refresh_lock.return_value = True
        codec = _make_codec()
        cache = SessionCache(ttl_seconds=60)
        refresh = MagicMock()
        refresh.refresh = AsyncMock(
            return_value=RefreshResult(
                access_token="access.refreshed",
                refresh_token="refresh.rotated",
                id_token="id.refreshed",
                access_token_exp=now + 3600,
            )
        )
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=refresh,
            cache=cache,
        )
        sealed = codec.seal(CookiePayload(session_id=record.session_id))

        body = TestClient(app).get("/echo", headers=_bff(sealed)).json()

        assert body["access_token"] == "access.refreshed"
        assert repo.update_tokens.await_args.kwargs["ttl"] == record.created_at + ABSOLUTE
        cached = cache.get(record.session_id)
        assert cached is not None
        assert cached.client == NATIVE_CLIENT
        assert cached.device_model == "iPhone17,1"
        assert cached.app_version == "1.0"
        assert cached.ttl == record.created_at + ABSOLUTE


@pytest.mark.asyncio
async def test_repository_round_trips_the_native_fields(repository, sample_record) -> None:
    record = dataclasses.replace(sample_record(), client=NATIVE_CLIENT)
    await repository.put(record)

    activated = await repository.activate_native(
        record.session_id, ttl=record.created_at + ABSOLUTE, device_model="iPad", app_version="2.1"
    )

    assert activated
    fetched = await repository.get(record.session_id)
    assert fetched.client == NATIVE_CLIENT
    assert fetched.is_native
    assert fetched.device_model == "iPad"
    assert fetched.app_version == "2.1"
    assert fetched.ttl == record.created_at + ABSOLUTE


@pytest.mark.asyncio
async def test_a_browser_row_carries_no_native_attributes(repository, sample_record) -> None:
    record = sample_record()
    await repository.put(record)
    item = repository._table.get_item(Key={"PK": f"SESSION#{record.session_id}", "SK": "META"})["Item"]
    assert not {"client", "device_model", "app_version"} & set(item)
    assert not (await repository.get(record.session_id)).is_native


@pytest.mark.asyncio
async def test_activate_native_never_creates_a_phantom_row(repository) -> None:
    assert not await repository.activate_native(
        "gone", ttl=int(time.time()) + 60, device_model=None, app_version=None
    )
    assert repository._table.scan()["Items"] == []


def test_dynamodb_handoff_is_single_use(monkeypatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="oidc-state",
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
        store = DynamoDBStateStore(table_name="oidc-state", region="us-east-1")
        expires = int(time.time()) + 120
        store.store_handoff("code-1", NativeHandoff("sess-1", "challenge", expires))

        item = store.table.get_item(Key={"PK": "HANDOFF#code-1", "SK": "HANDOFF#code-1"})["Item"]
        assert item["expiresAt"] == expires  # the table's TTL attribute

        assert store.take_handoff("code-1") == NativeHandoff("sess-1", "challenge", expires)
        assert store.take_handoff("code-1") is None
        assert store.take_handoff("never") is None

        # The login's native fields survive the state row's round trip.
        store.store_state(
            "st", OIDCStateData(native_redirect_uri="app://auth/callback", native_code_challenge="c")
        )
        ok, data = store.get_and_delete_state("st")
        assert ok
        assert data.native_redirect_uri == "app://auth/callback"
        assert data.native_code_challenge == "c"
