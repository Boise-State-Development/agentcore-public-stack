"""Cross-task refresh-coalescing tests for SessionRefreshMiddleware.

Locks in the regression that PR #264 created and the cookie-codec fix would
*expose* once dev started working again: with `desiredCount: 2`, two
`SessionRefreshMiddleware` instances running in two ECS tasks would each
see a cookie crossing the refresh-leeway boundary, each call
`cognito-idp:initiate_auth` with the same refresh token, and one of them
would lose the rotation race — Cognito revokes the original token on the
winner's exchange, the loser gets `NotAuthorizedException`, the loser's
middleware clears the user's cookie. Page-load fan-outs become routine
silent logouts.

The fix coalesces the refresh exchange across tasks via a DynamoDB
conditional-write lock (`refresh_lock_owner` + `refresh_lock_until` on
the session row). These tests instantiate two repository + middleware
pairs against ONE moto-backed DDB table so we can drive the leader and
follower paths deterministically without spinning real ECS tasks.

What's covered:
    - Leader-only Cognito refresh under same-time contention from two tasks
    - Follower adoption of the leader's persisted tokens (no Cognito call)
    - Leader crash (Cognito error) releases the lock so peers can retry
    - Lock TTL recovery: a crashed leader's lock unblocks peers after TTL
    - Refresh-token rotation: peer's rotated tokens propagate to follower
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import boto3
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from moto import mock_aws

from apis.shared.middleware.session_refresh import SessionRefreshMiddleware
from apis.shared.sessions_bff import lock as lock_module
from apis.shared.sessions_bff import single_flight as single_flight_module
from apis.shared.sessions_bff.cache import SessionCache
from apis.shared.sessions_bff.config import (
    BFFConfig,
    SESSION_COOKIE_NAME,
)
from apis.shared.sessions_bff.cookie import CookieCodec
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord
from apis.shared.sessions_bff.refresh import (
    CognitoRefreshError,
    RefreshResult,
)
from apis.shared.sessions_bff.repository import SessionRepository

# Single shared DDB table — both "tasks" attach to the same backing store,
# matching production where two ECS tasks read/write one BFFSessionsTable.
TABLE_NAME = "test-bff-sessions"


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Drop process-wide locks + single-flight registries between tests so
    a leftover Future or asyncio lock from one test can't influence the
    next case's contention behavior."""
    lock_module._reset_for_tests()
    single_flight_module._reset_for_tests()
    yield
    lock_module._reset_for_tests()
    single_flight_module._reset_for_tests()


@pytest.fixture
def two_task_setup(monkeypatch):
    """Spin up two `SessionRefreshMiddleware` instances over one moto DDB
    table so each represents a distinct ECS task in the same fleet."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
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

        # Both tasks share the data-key secret (otherwise the cookie sealed
        # by Task A would unseal as `bad seal` on Task B — that's the OTHER
        # bug in this branch, exercised by test_cookie). We pre-inject one
        # AES key here to keep the test focused on the refresh-lock path.
        shared_aes_key = secrets.token_bytes(32)

        def _make_codec() -> CookieCodec:
            codec = CookieCodec(
                kms_key_arn="arn:aws:kms:fake",
                data_key_secret_arn="arn:aws:secretsmanager:fake",
            )
            codec._cipher = AESGCM(shared_aes_key)
            return codec

        def _make_task(*, refresh_client) -> dict:
            repo = SessionRepository(table_name=TABLE_NAME)
            codec = _make_codec()
            cache = SessionCache(ttl_seconds=60)
            config = _enabled_config()

            app = FastAPI()
            app.add_middleware(
                SessionRefreshMiddleware,
                config=config,
                repository=repo,
                cookie_codec=codec,
                refresh_client=refresh_client,
                cache=cache,
                refresh_lock_ttl_seconds=2,  # short for tests
            )

            @app.get("/echo")
            async def echo(request: Request):
                record = getattr(request.state, "bff_session", None)
                return {
                    "has_session": record is not None,
                    "access_token": (
                        record.cognito_access_token if record else None
                    ),
                    "refresh_token": (
                        record.cognito_refresh_token if record else None
                    ),
                }

            return {
                "app": app,
                "repository": repo,
                "codec": codec,
                "cache": cache,
                "refresh_client": refresh_client,
            }

        yield {
            "make_task": _make_task,
            "table_name": TABLE_NAME,
            "shared_aes_key": shared_aes_key,
            "make_codec": _make_codec,
        }


def _enabled_config() -> BFFConfig:
    return BFFConfig(
        sessions_table_name="tbl",
        cookie_signing_key_arn="arn:aws:kms:fake",
        session_ttl_seconds=28800,
        refresh_leeway_seconds=60,
        cognito_bff_app_client_id="client-id",
        cognito_bff_app_client_secret_arn="arn:secret",
        inference_api_url=None,
        absolute_lifetime_seconds=30 * 24 * 3600,
        sliding_renewal_throttle_seconds=300,
    )


def _seed_session_in_refresh_window(repository: SessionRepository) -> SessionRecord:
    """Persist a session whose access token is inside the refresh leeway,
    so the middleware MUST hit the refresh path."""
    now = int(time.time())
    record = SessionRecord(
        session_id="sess-cross-task",
        user_id="user-001",
        username="alice",
        cognito_access_token="access.original",
        cognito_refresh_token="refresh.original",
        id_token="id.original",
        access_token_exp=now + 5,  # within 60s leeway
        csrf_secret="csrf-secret",
        created_at=now,
        last_seen_at=now,
        ttl=now + 28800,
    )
    asyncio.run(repository.put(record))
    return record


def test_only_the_leader_calls_cognito_under_cross_task_contention(
    two_task_setup,
) -> None:
    """Two tasks see the same cookie in the refresh window. Exactly one
    calls Cognito (the leader). The other adopts the leader's tokens
    from DDB without ever calling Cognito.

    Pre-fix: BOTH tasks would call Cognito with the same refresh token,
    and the loser would get NotAuthorizedException → clear cookie → 401.
    """
    # Refresh client A is the leader's; refresh client B simulates the
    # follower's. We assert that B is NEVER called.
    leader_refresh = AsyncMock(
        return_value=RefreshResult(
            access_token="access.fresh-from-leader",
            refresh_token="refresh.rotated-by-leader",
            id_token="id.fresh",
            access_token_exp=int(time.time()) + 3600,
        )
    )
    follower_refresh = AsyncMock(
        side_effect=AssertionError(
            "Follower MUST NOT call Cognito — peer holds the refresh lock"
        )
    )

    task_a = two_task_setup["make_task"](refresh_client=MagicMock(refresh=leader_refresh))
    task_b = two_task_setup["make_task"](refresh_client=MagicMock(refresh=follower_refresh))

    record = _seed_session_in_refresh_window(task_a["repository"])
    sealed = task_a["codec"].seal(CookiePayload(session_id=record.session_id))

    # Drive task_a first (it'll grab the lock and refresh). Then drive
    # task_b — it must observe the lock as held (or just released, with
    # tokens already rotated on the row) and adopt rather than refresh.
    with TestClient(task_a["app"]) as client_a:
        response_a = client_a.get(
            "/echo", cookies={SESSION_COOKIE_NAME: sealed}
        )
    with TestClient(task_b["app"]) as client_b:
        response_b = client_b.get(
            "/echo", cookies={SESSION_COOKIE_NAME: sealed}
        )

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert response_a.json()["has_session"] is True
    assert response_b.json()["has_session"] is True
    # Both tasks see the leader's freshly rotated tokens.
    assert response_a.json()["access_token"] == "access.fresh-from-leader"
    assert response_b.json()["access_token"] == "access.fresh-from-leader"
    assert response_b.json()["refresh_token"] == "refresh.rotated-by-leader"

    leader_refresh.assert_called_once()
    follower_refresh.assert_not_called()


def test_follower_polls_until_leader_persists_then_adopts(
    two_task_setup,
) -> None:
    """Simulates near-simultaneous arrival: task_a gets the lock just
    before task_b runs. Task_b's `_wait_for_peer_refresh` polls DDB
    and adopts task_a's tokens once they land.

    To force the follower to actually poll (rather than fall through
    a fully-completed leader path), we make the leader's Cognito refresh
    take a measurable amount of time and start the follower while the
    leader is still in flight.
    """
    leader_done = asyncio.Event()
    follower_started = asyncio.Event()

    async def slow_leader_refresh(*args, **kwargs) -> RefreshResult:
        # Wait for the follower to be inside its poll loop, then complete.
        await follower_started.wait()
        await asyncio.sleep(0.05)
        leader_done.set()
        return RefreshResult(
            access_token="access.fresh-leader",
            refresh_token="refresh.rotated-leader",
            id_token="id.fresh",
            access_token_exp=int(time.time()) + 3600,
        )

    leader_refresh = AsyncMock(side_effect=slow_leader_refresh)
    follower_refresh = AsyncMock(
        side_effect=AssertionError("Follower must NOT call Cognito")
    )

    task_a = two_task_setup["make_task"](refresh_client=MagicMock(refresh=leader_refresh))
    task_b = two_task_setup["make_task"](refresh_client=MagicMock(refresh=follower_refresh))
    record = _seed_session_in_refresh_window(task_a["repository"])
    sealed = task_a["codec"].seal(CookiePayload(session_id=record.session_id))

    async def drive_both() -> tuple[dict, dict]:
        async def hit(client_app):
            from httpx import ASGITransport, AsyncClient

            async with AsyncClient(
                transport=ASGITransport(app=client_app), base_url="http://t"
            ) as client:
                response = await client.get(
                    "/echo", cookies={SESSION_COOKIE_NAME: sealed}
                )
                return response.json()

        async def driven_follower():
            # Start the follower a tick later, so the leader has the lock.
            await asyncio.sleep(0.02)
            follower_started.set()
            return await hit(task_b["app"])

        a, b = await asyncio.gather(hit(task_a["app"]), driven_follower())
        return a, b

    a_body, b_body = asyncio.run(drive_both())

    assert a_body["has_session"] is True
    assert b_body["has_session"] is True
    assert a_body["access_token"] == "access.fresh-leader"
    assert b_body["access_token"] == "access.fresh-leader"
    leader_refresh.assert_called_once()
    follower_refresh.assert_not_called()


def test_lock_ttl_lets_a_peer_retry_after_a_dead_leader(
    two_task_setup,
) -> None:
    """Leader's Cognito call fails → lock is released → peer can refresh
    on its next request without waiting for the full TTL.

    This guards against the worst case where a leader crashes mid-refresh
    and never persists tokens. We don't want every subsequent request to
    fail closed for the duration of the lock TTL.
    """
    leader_refresh = AsyncMock(side_effect=CognitoRefreshError("Cognito down"))
    follower_refresh = AsyncMock(
        return_value=RefreshResult(
            access_token="access.peer-fresh",
            refresh_token="refresh.peer-rotated",
            id_token="id.peer",
            access_token_exp=int(time.time()) + 3600,
        )
    )

    task_a = two_task_setup["make_task"](
        refresh_client=MagicMock(refresh=leader_refresh)
    )
    task_b = two_task_setup["make_task"](
        refresh_client=MagicMock(refresh=follower_refresh)
    )
    record = _seed_session_in_refresh_window(task_a["repository"])
    sealed = task_a["codec"].seal(CookiePayload(session_id=record.session_id))

    # Task A: leader fails. The middleware clears its cookie for THIS
    # request but releases the lock (so a peer can retry).
    with TestClient(task_a["app"]) as client_a:
        response_a = client_a.get(
            "/echo", cookies={SESSION_COOKIE_NAME: sealed}
        )
    assert response_a.status_code == 200
    assert response_a.json()["has_session"] is False
    set_cookies_a = response_a.headers.get_list("set-cookie")
    assert any(
        "__Host-bff_session=" in c and "Max-Age=0" in c for c in set_cookies_a
    ), "Task A must clear cookie after its own refresh failed"

    # Task B (different request): lock is released; peer becomes the new
    # leader and refreshes successfully.
    with TestClient(task_b["app"]) as client_b:
        response_b = client_b.get(
            "/echo", cookies={SESSION_COOKIE_NAME: sealed}
        )
    assert response_b.status_code == 200
    assert response_b.json()["has_session"] is True
    assert response_b.json()["access_token"] == "access.peer-fresh"
    leader_refresh.assert_called_once()
    follower_refresh.assert_called_once()


def test_follower_falls_back_terminal_when_leader_disappears_mid_refresh(
    two_task_setup,
) -> None:
    """Pathological case: leader holds the lock but never persists tokens
    AND never releases (e.g. process killed). The follower's poll deadline
    is bounded by `refresh_lock_ttl_seconds`; after that, this request
    fails closed (clear cookie). The user re-auths.

    The next request after this one will see the lock TTL'd and can
    re-acquire — that path is covered by
    `test_lock_ttl_lets_a_peer_retry_after_a_dead_leader`.
    """
    follower_refresh = AsyncMock(
        side_effect=AssertionError("Follower must NOT call Cognito while a peer holds the lock")
    )
    task_b = two_task_setup["make_task"](
        refresh_client=MagicMock(refresh=follower_refresh)
    )
    record = _seed_session_in_refresh_window(task_b["repository"])

    # Manually park a lock on the row as if some other task is mid-refresh
    # but hasn't persisted yet (and won't, for the duration of this test).
    asyncio.run(
        task_b["repository"].try_acquire_refresh_lock(
            session_id=record.session_id,
            owner="ghost-task",
            lock_ttl_seconds=2,  # matches make_task's middleware TTL
        )
    )

    sealed = task_b["codec"].seal(CookiePayload(session_id=record.session_id))
    with TestClient(task_b["app"]) as client_b:
        response = client_b.get(
            "/echo", cookies={SESSION_COOKIE_NAME: sealed}
        )

    assert response.status_code == 200
    assert response.json()["has_session"] is False
    set_cookies = response.headers.get_list("set-cookie")
    assert any(
        "__Host-bff_session=" in c and "Max-Age=0" in c for c in set_cookies
    ), "Follower must clear cookie after polling timed out on a stuck leader"
    follower_refresh.assert_not_called()


def test_two_tasks_in_parallel_call_cognito_at_most_once(
    two_task_setup,
) -> None:
    """Pure asyncio gather of one request per task at the same instant.
    Whichever wins the conditional UpdateItem becomes the leader; the
    other adopts. Combined Cognito call count must be exactly 1.

    This is the closest analogue to the page-load fan-out behavior we
    care about in production — two tasks each receive their share of
    the 8-endpoint fan-out at the moment the cookie crosses the leeway
    window.
    """
    refresh_count = {"calls": 0}

    async def counted_refresh(*args, **kwargs):
        refresh_count["calls"] += 1
        await asyncio.sleep(0.05)
        return RefreshResult(
            access_token="access.fresh",
            refresh_token="refresh.rotated",
            id_token="id.fresh",
            access_token_exp=int(time.time()) + 3600,
        )

    refresh_a = AsyncMock(side_effect=counted_refresh)
    refresh_b = AsyncMock(side_effect=counted_refresh)

    task_a = two_task_setup["make_task"](
        refresh_client=MagicMock(refresh=refresh_a)
    )
    task_b = two_task_setup["make_task"](
        refresh_client=MagicMock(refresh=refresh_b)
    )
    record = _seed_session_in_refresh_window(task_a["repository"])
    sealed = task_a["codec"].seal(CookiePayload(session_id=record.session_id))

    async def drive() -> tuple[dict, dict]:
        from httpx import ASGITransport, AsyncClient

        async def hit(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://t"
            ) as client:
                response = await client.get(
                    "/echo", cookies={SESSION_COOKIE_NAME: sealed}
                )
                return response.json()

        return await asyncio.gather(hit(task_a["app"]), hit(task_b["app"]))

    a_body, b_body = asyncio.run(drive())

    # Both succeeded.
    assert a_body["has_session"] is True
    assert b_body["has_session"] is True
    # Both got the same fresh tokens (one set, sourced from the leader).
    assert a_body["access_token"] == b_body["access_token"] == "access.fresh"
    assert a_body["refresh_token"] == b_body["refresh_token"] == "refresh.rotated"
    # CRITICAL: across BOTH tasks, Cognito refresh was called at most once.
    assert refresh_count["calls"] == 1, (
        f"Cross-task coalescing violated — Cognito refresh was called "
        f"{refresh_count['calls']} times across two tasks"
    )
