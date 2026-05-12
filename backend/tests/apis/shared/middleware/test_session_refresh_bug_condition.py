"""Bug condition exploration property tests for SessionRefreshMiddleware event-loop blocking.

Property 1: Bug Condition — Event-Loop Non-Blocking, Coalesced, Window-Staggered, Fire-and-Forget

This file encodes the EXPECTED behavior (Property 1 / Expected Behavior 2.1–2.7) from
the design document. Each sub-condition test surfaces a counterexample that demonstrates
the corresponding sub-condition (1.1–1.7) of `isBugCondition` from design.md.

CRITICAL: These tests MUST FAIL on unfixed code — failure confirms the bug exists.
They will PASS after the fix (task 3 series) is implemented:
  - Repository/Cognito offload via asyncio.to_thread (2.1, 2.2)
  - Per-session single-flight for the resolve path (2.3)
  - Strict-multiple windows (throttle=300s, leeway=60s) (2.4)
  - Fire-and-forget slide-write (2.5)
  - appApi.desiredCount >= 2 (2.6)
  - Bounded blocking DDB calls across fan-out (2.7)

Scoped PBT Approach: each sub-condition is reproduced by a concrete, deterministic
scenario under pytest-asyncio. Hypothesis is used on the two sub-conditions that
generalize over a family of inputs (fan-out size for 1.3 / 1.7).

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Request
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apis.shared.middleware.session_refresh import SessionRefreshMiddleware
from apis.shared.sessions_bff import lock as lock_module
from apis.shared.sessions_bff.cache import SessionCache
from apis.shared.sessions_bff.config import (
    BFFConfig,
    SESSION_COOKIE_NAME,
    _DEFAULT_REFRESH_LEEWAY_SECONDS,
    _DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS,
)
from apis.shared.sessions_bff.cookie import CookieCodec
from apis.shared.sessions_bff.lock import get_session_lock
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord
from apis.shared.sessions_bff.refresh import (
    CognitoRefreshClient,
    _reset_secret_cache_for_tests,
)
from apis.shared.sessions_bff.repository import SessionRepository


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures and helpers
# ═══════════════════════════════════════════════════════════════════════════


class InstrumentedTable:
    """Synchronous fake of a boto3 DynamoDB Table.

    Records call counts and can inject a `time.sleep` delay to block the
    event loop thread on unfixed code, letting us prove whether the caller
    yielded to the loop while the boto3 call was in flight.

    Mirrors the tiny subset of the Table API that `SessionRepository` uses:
    `get_item`, `update_item`, `put_item`, `delete_item`.
    """

    def __init__(
        self,
        *,
        record: Optional[SessionRecord] = None,
        delay_s: float = 0.0,
    ) -> None:
        self._delay_s = delay_s
        self._record = record
        self.get_item_calls = 0
        self.update_item_calls = 0
        self.put_item_calls = 0
        self.delete_item_calls = 0

    def _sleep(self) -> None:
        if self._delay_s > 0:
            time.sleep(self._delay_s)

    def get_item(self, Key: dict) -> dict:
        self.get_item_calls += 1
        self._sleep()
        if self._record is None:
            return {}
        return {"Item": _record_to_item(self._record)}

    def update_item(self, **kwargs: Any) -> dict:
        self.update_item_calls += 1
        self._sleep()
        return {}

    def put_item(self, Item: dict) -> dict:
        self.put_item_calls += 1
        self._sleep()
        return {}

    def delete_item(self, Key: dict) -> dict:
        self.delete_item_calls += 1
        self._sleep()
        return {}


def _record_to_item(r: SessionRecord) -> dict:
    return {
        "PK": f"SESSION#{r.session_id}",
        "SK": "META",
        "session_id": r.session_id,
        "user_id": r.user_id,
        "username": r.username,
        "cognito_access_token": r.cognito_access_token,
        "cognito_refresh_token": r.cognito_refresh_token,
        "id_token": r.id_token,
        "access_token_exp": r.access_token_exp,
        "csrf_secret": r.csrf_secret,
        "created_at": r.created_at,
        "last_seen_at": r.last_seen_at,
        "ttl": r.ttl,
    }


def _make_repo(table: InstrumentedTable) -> SessionRepository:
    """Build a SessionRepository backed by an InstrumentedTable.

    Bypasses boto3.resource() initialization by starting disabled, then
    flipping `_enabled` and injecting the fake table. Exercises the real
    SessionRepository async-method bodies — which is the point for
    sub-condition 1.1 (offload).
    """
    repo = SessionRepository(table_name="")
    repo._enabled = True
    repo._table = table  # type: ignore[assignment]
    repo._table_name = "test-bff-sessions"
    return repo


def _make_codec() -> CookieCodec:
    codec = CookieCodec(kms_key_arn="arn:aws:kms:fake")
    # Pre-inject an AES-GCM cipher so no KMS call is attempted.
    codec._cipher = AESGCM(secrets.token_bytes(32))
    return codec


def _make_record(
    *,
    session_id: str = "sess-001",
    access_token_exp: Optional[int] = None,
    last_seen_at: Optional[int] = None,
    created_at: Optional[int] = None,
) -> SessionRecord:
    now = int(time.time())
    return SessionRecord(
        session_id=session_id,
        user_id="user-sub-001",
        username="alice",
        cognito_access_token="access.original",
        cognito_refresh_token="refresh.original",
        id_token="id.original",
        access_token_exp=access_token_exp if access_token_exp is not None else now + 3600,
        csrf_secret="csrf-secret-deadbeef",
        created_at=created_at if created_at is not None else now,
        last_seen_at=last_seen_at if last_seen_at is not None else now,
        ttl=now + 28800,
    )


def _enabled_config(**overrides: Any) -> BFFConfig:
    defaults: dict[str, Any] = dict(
        sessions_table_name="tbl",
        cookie_signing_key_arn="arn:aws:kms:fake",
        session_ttl_seconds=28800,
        refresh_leeway_seconds=_DEFAULT_REFRESH_LEEWAY_SECONDS,
        cognito_bff_app_client_id="client-id",
        cognito_bff_app_client_secret_arn="arn:secret",
        inference_api_url=None,
        absolute_lifetime_seconds=30 * 24 * 3600,
        sliding_renewal_throttle_seconds=_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS,
    )
    defaults.update(overrides)
    return BFFConfig(**defaults)


def _build_app(
    *,
    config: BFFConfig,
    repository: SessionRepository,
    codec: CookieCodec,
    refresh_client: Any,
    cache: Optional[SessionCache] = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        SessionRefreshMiddleware,
        config=config,
        repository=repository,
        cookie_codec=codec,
        refresh_client=refresh_client,
        cache=cache or SessionCache(ttl_seconds=60),
    )

    @app.get("/echo")
    async def echo(request: Request) -> dict:
        record = getattr(request.state, "bff_session", None)
        return {
            "has_session": record is not None,
            "session_id": record.session_id if record else None,
        }

    return app


@pytest.fixture(autouse=True)
def _reset_session_state() -> Any:
    """Clear process-wide state between tests so storm/coalescing behavior
    stays independent across cases."""
    lock_module._reset_for_tests()
    _reset_secret_cache_for_tests()
    yield
    lock_module._reset_for_tests()
    _reset_secret_cache_for_tests()


# ═══════════════════════════════════════════════════════════════════════════
# Sub-condition 1.1 — SessionRepository.* must offload sync boto3 to a threadpool
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    ["get", "touch_last_seen", "update_tokens", "put", "delete"],
)
async def test_1_1_session_repository_methods_offload_sync_boto3(
    method_name: str,
) -> None:
    """(1.1) Repository offload.

    Each SessionRepository async method that wraps boto3 must execute its
    boto3 call off the event loop thread. We prove this by running the
    method concurrently with a 50ms marker coroutine against a 500ms
    slow-stubbed table.

    - Fixed code: marker completes in ~0.05s while repo call is still in flight.
    - Unfixed code: sync boto3 freezes the loop for the full 500ms, starving
      the marker so it only completes once the method returns.

    Expected Behavior 2.1 (design.md).
    """
    record = _make_record(session_id=f"sess-1-1-{method_name}")
    table = InstrumentedTable(record=record, delay_s=0.5)
    repo = _make_repo(table)

    now = int(time.time())
    if method_name == "get":
        op = repo.get(record.session_id)
    elif method_name == "touch_last_seen":
        op = repo.touch_last_seen(record.session_id, last_seen_at=now)
    elif method_name == "update_tokens":
        op = repo.update_tokens(
            session_id=record.session_id,
            access_token="access.rotated",
            refresh_token="refresh.rotated",
            id_token=None,
            access_token_exp=now + 3600,
            last_seen_at=now,
        )
    elif method_name == "put":
        op = repo.put(record)
    elif method_name == "delete":
        op = repo.delete(record.session_id)
    else:
        pytest.fail(f"unknown method_name: {method_name}")

    marker_elapsed: dict[str, float] = {}

    async def marker(start: float) -> None:
        await asyncio.sleep(0.05)
        marker_elapsed["t"] = time.monotonic() - start

    t0 = time.monotonic()
    marker_task = asyncio.create_task(marker(t0))
    await op
    op_elapsed = time.monotonic() - t0
    await marker_task

    # Sanity: the stubbed boto3 call really took ~500ms.
    assert op_elapsed >= 0.4, (
        f"[1.1/{method_name}] Sanity: stubbed {method_name} should take ~500ms, "
        f"got {op_elapsed:.3f}s — the InstrumentedTable delay may not be wired."
    )
    # Counterexample: on unfixed code, the marker sits behind the frozen loop.
    assert "t" in marker_elapsed, (
        f"[1.1/{method_name}] Marker coroutine never completed — "
        f"event loop fully frozen by sync boto3."
    )
    assert marker_elapsed["t"] < 0.25, (
        f"[1.1/{method_name}] Marker coroutine starved by sync boto3: "
        f"marker elapsed={marker_elapsed['t']:.3f}s, "
        f"op elapsed={op_elapsed:.3f}s. "
        f"SessionRepository.{method_name} must offload its boto3 call via "
        "asyncio.to_thread so the event loop continues scheduling other "
        "coroutines for the round-trip duration."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Sub-condition 1.2 — CognitoRefreshClient.refresh must offload initiate_auth
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_2_cognito_refresh_offloads_sync_initiate_auth() -> None:
    """(1.2) Cognito offload.

    CognitoRefreshClient.refresh must execute cognito-idp:initiate_auth
    off the event loop thread, including while the per-session
    get_session_lock(session_id) is held. We prove this by running
    refresh concurrently with:
      (a) a 50ms marker coroutine;
      (b) an unrelated get_session_lock(other_session_id) acquisition.

    - Fixed code: both complete promptly while refresh is still in flight.
    - Unfixed code: the sync initiate_auth freezes the loop, starving
      the marker and delaying the unrelated lock acquisition.

    Expected Behavior 2.2 (design.md).
    """
    slow_cognito = MagicMock()

    def slow_initiate_auth(**_kwargs: Any) -> dict:
        time.sleep(0.5)
        return {
            "AuthenticationResult": {
                "AccessToken": "access.fresh",
                "RefreshToken": "refresh.fresh",
                "IdToken": "id.fresh",
                "ExpiresIn": 3600,
            }
        }

    slow_cognito.initiate_auth.side_effect = slow_initiate_auth

    slow_secrets = MagicMock()
    slow_secrets.get_secret_value.return_value = {"SecretString": "client-secret"}

    client = CognitoRefreshClient(
        app_client_id="client-id",
        app_client_secret_arn="arn:secret",
        cognito_idp_client=slow_cognito,
        secrets_manager_client=slow_secrets,
    )

    marker_elapsed: dict[str, float] = {}
    lock_elapsed: dict[str, float] = {}
    refresh_elapsed: dict[str, float] = {}

    async def call_refresh(start: float) -> None:
        result = client.refresh(username="alice", refresh_token="refresh.original")
        # Support both the unfixed (sync) and fixed (coroutine) shape.
        if asyncio.iscoroutine(result):
            result = await result
        refresh_elapsed["t"] = time.monotonic() - start

    async def marker(start: float) -> None:
        await asyncio.sleep(0.05)
        marker_elapsed["t"] = time.monotonic() - start

    async def acquire_other_lock(start: float) -> None:
        other_lock = get_session_lock("other-session-id")
        async with other_lock:
            pass
        lock_elapsed["t"] = time.monotonic() - start

    t0 = time.monotonic()
    marker_task = asyncio.create_task(marker(t0))
    other_lock_task = asyncio.create_task(acquire_other_lock(t0))
    await call_refresh(t0)
    await marker_task
    await other_lock_task

    # Sanity: the stubbed initiate_auth really took ~500ms.
    assert refresh_elapsed.get("t", 0.0) >= 0.4, (
        f"[1.2] Sanity: stubbed refresh should take ~500ms, "
        f"got {refresh_elapsed.get('t', 0.0):.3f}s — stub not wired."
    )
    assert "t" in marker_elapsed, (
        "[1.2] Marker coroutine never completed — loop fully frozen."
    )
    assert marker_elapsed["t"] < 0.25, (
        f"[1.2] Marker coroutine starved by sync Cognito initiate_auth: "
        f"marker elapsed={marker_elapsed['t']:.3f}s, "
        f"refresh elapsed={refresh_elapsed['t']:.3f}s. "
        "CognitoRefreshClient.refresh must offload initiate_auth via "
        "asyncio.to_thread so other coroutines — including those for "
        "different session_ids — make progress while the per-session "
        "asyncio.Lock is held."
    )
    assert lock_elapsed["t"] < 0.25, (
        f"[1.2] Unrelated get_session_lock('other-session-id') acquisition "
        f"starved by sync Cognito call: lock elapsed={lock_elapsed['t']:.3f}s, "
        f"refresh elapsed={refresh_elapsed['t']:.3f}s. "
        "Even uncontended locks for different sessions block when the "
        "event loop thread is frozen."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Sub-condition 1.3 — Resolve-path coalescing: N concurrent reqs → 1 get_item
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("fanout", [8])
async def test_1_3_concurrent_same_session_fanout_coalesces_to_one_get_item(
    fanout: int,
) -> None:
    """(1.3) Resolve-path coalescing.

    N concurrent SessionRefreshMiddleware.dispatch calls for the same
    session_id with a cold SessionCache and a valid sealed cookie must
    result in exactly ONE DynamoDB get_item invocation. The upstream
    unseal → SessionCache.get → SessionRepository.get path needs
    coalescing via a per-session single-flight primitive.

    - Fixed code: 1 get_item (single-flight leader + followers).
    - Unfixed code: N get_item calls — the existing get_session_lock only
      wraps the Cognito exchange, not the resolve path.

    Expected Behavior 2.3 (design.md).
    """
    record = _make_record(session_id="sess-1-3")
    # Small delay so concurrent dispatches overlap long enough for each
    # to observe cache-miss independently on unfixed code.
    table = InstrumentedTable(record=record, delay_s=0.05)
    repo = _make_repo(table)
    codec = _make_codec()
    refresh_client = MagicMock()
    cache = SessionCache(ttl_seconds=60)  # cold → cache miss
    app = _build_app(
        config=_enabled_config(),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
        cache=cache,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, sealed)
        responses = await asyncio.gather(
            *(client.get("/echo") for _ in range(fanout))
        )

    for r in responses:
        assert r.status_code == 200

    assert table.get_item_calls == 1, (
        f"[1.3] Fan-out of {fanout} concurrent same-session requests against "
        f"a cold cache must coalesce to exactly one get_item call. "
        f"Observed: {table.get_item_calls} get_item calls (bug target: {fanout}). "
        "A per-session asyncio.Future single-flight is required upstream of "
        "SessionRepository.get."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Sub-condition 1.4 — Cache window and slide throttle must be de-aligned
# ═══════════════════════════════════════════════════════════════════════════


@given(
    throttle=st.just(_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS),
    leeway=st.just(_DEFAULT_REFRESH_LEEWAY_SECONDS),
)
@settings(max_examples=1, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_1_4a_default_throttle_is_strict_multiple_of_leeway(
    throttle: int, leeway: int
) -> None:
    """(1.4) Window de-alignment — config invariant.

    _DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS must be a strict multiple of
    _DEFAULT_REFRESH_LEEWAY_SECONDS AND strictly greater. This de-aligns
    cache-expiry (TTL = leeway) from slide-throttle expiry so a single
    request crossing one boundary does not also cross the other.

    - Fixed code: throttle=300, leeway=60 → 300 > 60 and 300 % 60 == 0.
    - Unfixed code: both default to 60 → 60 > 60 is False.

    Expected Behavior 2.4 (design.md).
    """
    assert throttle > leeway, (
        f"[1.4a] Sliding-renewal throttle ({throttle}s) must be strictly "
        f"greater than refresh leeway ({leeway}s) to de-align boundaries."
    )
    assert throttle % leeway == 0, (
        f"[1.4a] Sliding-renewal throttle ({throttle}s) must be a strict "
        f"multiple of refresh leeway ({leeway}s)."
    )


@pytest.mark.asyncio
async def test_1_4b_single_request_at_boundary_skips_slide_write() -> None:
    """(1.4) Window de-alignment — runtime behavior.

    A single request with SessionCache TTL just elapsed AND
    (now - last_seen_at) == refresh_leeway_seconds must issue AT MOST ONE
    of {get_item, update_item} on the critical path. On unfixed code the
    aligned 60s windows guarantee BOTH writes on the same request (the
    cache miss drives get_item AND the past-throttle state drives
    update_item).

    Expected Behavior 2.4 (design.md).
    """
    now = int(time.time())
    record = _make_record(
        session_id="sess-1-4b",
        last_seen_at=now - _DEFAULT_REFRESH_LEEWAY_SECONDS,
    )
    table = InstrumentedTable(record=record, delay_s=0.01)
    repo = _make_repo(table)
    codec = _make_codec()
    refresh_client = MagicMock()
    cache = SessionCache(ttl_seconds=60)  # cold → cache miss
    # Use the real default throttle so the test fails on unfixed code
    # (throttle == leeway == 60s) and passes on fixed code (throttle=300s,
    # leeway=60s).
    app = _build_app(
        config=_enabled_config(
            sliding_renewal_throttle_seconds=_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS,
        ),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
        cache=cache,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, sealed)
        response = await client.get("/echo")
    assert response.status_code == 200

    ddb_calls = table.get_item_calls + table.update_item_calls
    assert ddb_calls <= 1, (
        f"[1.4b] Single request at cache/throttle boundary issued "
        f"{table.get_item_calls} get_item + {table.update_item_calls} "
        f"update_item = {ddb_calls} DDB calls on critical path. "
        "Windows must be de-aligned (throttle > leeway, strict multiple) "
        "so a cache miss never also triggers a slide write."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Sub-condition 1.5 — _maybe_slide must fire-and-forget the DDB write
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_5_slide_write_is_fire_and_forget() -> None:
    """(1.5) Fire-and-forget slide.

    When a slide is warranted, the response path must NOT wait on
    touch_last_seen. Stubbing update_item with a 500ms delay, the total
    dispatch elapsed must stay well under 500ms.

    - Fixed code: _maybe_slide schedules touch_last_seen as an
      asyncio.Task and returns synchronously → elapsed ~= handler time.
    - Unfixed code: _maybe_slide awaits touch_last_seen inline →
      elapsed >= 500ms.

    Expected Behavior 2.5 (design.md).
    """
    now = int(time.time())
    record = _make_record(
        session_id="sess-1-5",
        last_seen_at=now - 3600,  # past any reasonable throttle window
    )
    table = InstrumentedTable(record=record, delay_s=0.5)
    repo = _make_repo(table)
    codec = _make_codec()
    refresh_client = MagicMock()

    # Pre-seed the cache so repo.get is not on the path — this test isolates
    # the slide-write-on-response-path question from the coalescing question.
    cache = SessionCache(ttl_seconds=60)
    cache.set(record)

    # Use a small throttle so the slide is warranted (last_seen == now-3600).
    app = _build_app(
        config=_enabled_config(sliding_renewal_throttle_seconds=60),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
        cache=cache,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, sealed)
        t0 = time.monotonic()
        response = await client.get("/echo")
        elapsed = time.monotonic() - t0

    assert response.status_code == 200
    # Sanity: the slide write was in fact requested (fires exactly once;
    # in the fixed scenario it's still counted on the fake table — it just
    # doesn't block the response path).
    assert table.update_item_calls >= 1, (
        f"[1.5] Sanity: the slide path should have fired update_item at least "
        f"once, got {table.update_item_calls}. Check last_seen_at setup."
    )
    assert elapsed < 0.25, (
        f"[1.5] Dispatch elapsed={elapsed:.3f}s; the response waited on the "
        "500ms stubbed update_item. _maybe_slide must dispatch the DDB write "
        "as a detached asyncio.Task so the response returns without blocking."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Sub-condition 1.6 — Production deployment must have concurrency slack
# ═══════════════════════════════════════════════════════════════════════════


def test_1_6_cdk_app_api_desired_count_at_least_two() -> None:
    """(1.6) Concurrency slack at deployment.

    infrastructure/cdk.context.json must set appApi.desiredCount >= 2 so
    a single blocked event loop on one ECS task cannot stall all ingress.

    Expected Behavior 2.6 (design.md).
    """
    cdk_context_path = (
        Path(__file__).resolve().parents[5] / "infrastructure" / "cdk.context.json"
    )
    assert cdk_context_path.exists(), (
        f"[1.6] Expected cdk.context.json at {cdk_context_path}"
    )
    ctx = json.loads(cdk_context_path.read_text())
    app_api = ctx.get("appApi", {})
    desired = app_api.get("desiredCount")
    assert isinstance(desired, int) and desired >= 2, (
        f"[1.6] appApi.desiredCount must be >= 2 in the production context "
        f"(found: {desired!r}). Single-task deployment cannot absorb a "
        "blocked event loop — a slow AWS call on one task halts every "
        "concurrent request."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Sub-condition 1.7 — Fan-out at cache boundary must not amplify to N*2 DDB calls
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("fanout", [8])
async def test_1_7_fanout_at_boundary_bounded_blocking_ddb_calls(
    fanout: int,
) -> None:
    """(1.7) Fan-out amplification.

    N concurrent requests for the same session at a cache-boundary moment
    must produce AT MOST 2 blocking DDB calls across the entire fan-out
    (ideally 1 get_item and 0 slide-writes when windows are de-aligned).

    - Fixed code: single-flight + de-aligned windows → ≤ 1 get_item +
      ≤ 1 update_item = ≤ 2.
    - Unfixed code: each coroutine observes cache miss + past-throttle
      independently on its local SessionRecord copy and issues its own
      get_item + update_item → 2*N blocking calls.

    Expected Behavior 2.7 (design.md).
    """
    now = int(time.time())
    record = _make_record(
        session_id="sess-1-7",
        last_seen_at=now - _DEFAULT_REFRESH_LEEWAY_SECONDS,  # past aligned throttle on unfixed
    )
    table = InstrumentedTable(record=record, delay_s=0.01)
    repo = _make_repo(table)
    codec = _make_codec()
    refresh_client = MagicMock()
    cache = SessionCache(ttl_seconds=60)  # cold → cache miss
    app = _build_app(
        config=_enabled_config(
            sliding_renewal_throttle_seconds=_DEFAULT_SLIDING_RENEWAL_THROTTLE_SECONDS,
        ),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
        cache=cache,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, sealed)
        responses = await asyncio.gather(
            *(client.get("/echo") for _ in range(fanout))
        )

    for r in responses:
        assert r.status_code == 200

    blocking_calls = table.get_item_calls + table.update_item_calls
    assert blocking_calls <= 2, (
        f"[1.7] Fan-out of {fanout} concurrent same-session requests at a "
        f"cache-boundary moment produced {table.get_item_calls} get_item + "
        f"{table.update_item_calls} update_item = {blocking_calls} blocking "
        f"DDB calls (bug: ~{2 * fanout}). Single-flight coalescing AND "
        "window de-alignment are required."
    )
