"""Tests for SessionRefreshMiddleware.

Covered:
    - Pass-through when BFF is not enabled (env vars unset).
    - Pass-through when no session cookie is present (Bearer-token requests).
    - Cookie present + session valid → record attached to `request.state`.
    - Cookie present + bad seal → cookie cleared on response.
    - Cookie present + session row missing → cookie cleared.
    - Cookie present + access token within leeway → refresh path called once.
    - Storm coalescing: N concurrent requests for the same session trigger
      exactly one Cognito refresh exchange.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apis.shared.middleware.session_refresh import SessionRefreshMiddleware
from apis.shared.sessions_bff import lock as lock_module
from apis.shared.sessions_bff.cache import SessionCache
from apis.shared.sessions_bff.config import (
    BFFConfig,
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from apis.shared.sessions_bff.cookie import CookieCodec
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord
from apis.shared.sessions_bff.refresh import RefreshResult


@pytest.fixture(autouse=True)
def _reset_session_locks() -> None:
    """Clear the process-wide lock registry between tests so storm-coalescing
    behavior is independent across cases."""
    lock_module._reset_for_tests()


def _make_codec() -> CookieCodec:
    codec = CookieCodec(kms_key_arn="arn:aws:kms:fake")
    codec._cipher = AESGCM(secrets.token_bytes(32))
    return codec


def _make_record(
    *,
    session_id: str = "sess-001",
    access_token_exp: Optional[int] = None,
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
        csrf_secret="csrf-secret",
        created_at=now,
        last_seen_at=now,
        ttl=now + 28800,
    )


def _enabled_config(
    *,
    sliding_renewal_throttle_seconds: int = 60,
    absolute_lifetime_seconds: int = 30 * 24 * 3600,
    session_ttl_seconds: int = 28800,
) -> BFFConfig:
    return BFFConfig(
        sessions_table_name="tbl",
        cookie_signing_key_arn="arn:aws:kms:fake",
        session_ttl_seconds=session_ttl_seconds,
        refresh_leeway_seconds=60,
        cognito_bff_app_client_id="client-id",
        cognito_bff_app_client_secret_arn="arn:secret",
        inference_api_url=None,
        absolute_lifetime_seconds=absolute_lifetime_seconds,
        sliding_renewal_throttle_seconds=sliding_renewal_throttle_seconds,
    )


def _build_app(
    *,
    config: BFFConfig,
    repository,
    codec: CookieCodec,
    refresh_client,
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
    async def echo(request: Request):
        record = getattr(request.state, "bff_session", None)
        return {
            "has_session": record is not None,
            "session_id": record.session_id if record else None,
            "access_token": record.cognito_access_token if record else None,
        }

    return app


def test_passthrough_when_bff_disabled() -> None:
    config = BFFConfig(
        sessions_table_name=None,
        cookie_signing_key_arn=None,
        session_ttl_seconds=28800,
        refresh_leeway_seconds=60,
        cognito_bff_app_client_id=None,
        cognito_bff_app_client_secret_arn=None,
        inference_api_url=None,
    )
    repo = AsyncMock()
    refresh = MagicMock()
    app = _build_app(
        config=config, repository=repo, codec=_make_codec(), refresh_client=refresh
    )
    response = TestClient(app).get("/echo")
    assert response.status_code == 200
    assert response.json() == {
        "has_session": False,
        "session_id": None,
        "access_token": None,
    }
    repo.get.assert_not_called()


def test_passthrough_when_no_cookie_present() -> None:
    repo = AsyncMock()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(),
        repository=repo,
        codec=_make_codec(),
        refresh_client=refresh,
    )
    response = TestClient(app).get("/echo")
    assert response.status_code == 200
    assert response.json()["has_session"] is False
    repo.get.assert_not_called()


def test_valid_cookie_attaches_session_to_request_state() -> None:
    record = _make_record()
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    body = response.json()
    assert body["has_session"] is True
    assert body["session_id"] == record.session_id
    assert body["access_token"] == "access.original"
    refresh.refresh.assert_not_called()


def test_unrecoverable_cookie_is_cleared() -> None:
    repo = AsyncMock()
    repo.get.return_value = None
    codec = _make_codec()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    response = TestClient(app).get(
        "/echo", cookies={SESSION_COOKIE_NAME: "garbage-value"}
    )
    assert response.status_code == 200
    assert response.json()["has_session"] is False
    # Both BFF cookies must be cleared so the browser stops echoing the
    # bad pair on every subsequent request. `getlist` because Set-Cookie
    # appears once per cookie cleared.
    set_cookie_headers = response.headers.get_list("set-cookie")
    cleared = " ".join(set_cookie_headers)
    assert SESSION_COOKIE_NAME in cleared
    assert CSRF_COOKIE_NAME in cleared


def test_missing_session_row_clears_cookie() -> None:
    """Cookie unseals fine but the DDB row is gone — clear the cookie."""
    repo = AsyncMock()
    repo.get.return_value = None
    codec = _make_codec()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id="sess-gone"))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})
    assert response.status_code == 200
    assert response.json()["has_session"] is False
    assert SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")


def test_near_expiry_session_triggers_refresh_once() -> None:
    record = _make_record(access_token_exp=int(time.time()) + 5)  # within 60s leeway
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    # `refresh_client.refresh` is now `async` (task 3.2) — use AsyncMock so
    # `await self._refresh_client.refresh(...)` in the middleware resolves.
    refresh = MagicMock()
    refresh.refresh = AsyncMock(
        return_value=RefreshResult(
            access_token="access.fresh",
            refresh_token="refresh.fresh",
            id_token="id.fresh",
            access_token_exp=int(time.time()) + 3600,
        )
    )
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    body = response.json()
    assert body["has_session"] is True
    # The refreshed token should be exposed downstream.
    assert body["access_token"] == "access.fresh"
    refresh.refresh.assert_awaited_once_with(
        username="alice", refresh_token="refresh.original"
    )
    repo.update_tokens.assert_awaited_once()


def test_refresh_failure_clears_cookie() -> None:
    from apis.shared.sessions_bff.refresh import CognitoRefreshError

    record = _make_record(access_token_exp=int(time.time()) + 5)
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    refresh.refresh = AsyncMock(side_effect=CognitoRefreshError("rotated"))
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    assert response.json()["has_session"] is False
    assert SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_storm_coalesces_to_single_refresh() -> None:
    """N concurrent in-flight requests for the same session id must only
    drive one Cognito refresh exchange, even if the refresh itself is slow.

    This guards against the multi-tab refresh-token-rotation storm called
    out in the project memory."""
    record = _make_record(access_token_exp=int(time.time()) + 5)
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()

    call_count = {"n": 0}

    def slow_refresh(*, username: str, refresh_token: str) -> RefreshResult:
        # Tight, synchronous bump — the lock guards entry, so this only
        # needs to be called sequentially to make the test meaningful.
        call_count["n"] += 1
        return RefreshResult(
            access_token=f"access.fresh.{call_count['n']}",
            refresh_token="refresh.fresh",
            id_token="id.fresh",
            access_token_exp=int(time.time()) + 3600,
        )

    refresh = MagicMock()
    refresh.refresh = AsyncMock(side_effect=slow_refresh)

    # After the first refresh, repo.get returns the *fresh* record so other
    # waiters short-circuit out of the refresh branch.
    fresh_record = _make_record(
        access_token_exp=int(time.time()) + 3600
    )
    fresh_record.cognito_access_token = "access.fresh.1"
    fresh_record.cognito_refresh_token = "refresh.fresh"
    repo.get.side_effect = [record, record, fresh_record, fresh_record, fresh_record]
    repo.update_tokens = AsyncMock()

    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))

    # Use httpx AsyncClient driven against the ASGI app for true concurrency.
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, sealed)
        responses = await asyncio.gather(
            *(client.get("/echo") for _ in range(5))
        )

    for r in responses:
        assert r.status_code == 200
    # Critical assertion: only one refresh call across all 5 concurrent reqs.
    assert call_count["n"] == 1


# ─── Sliding-session tests ─────────────────────────────────────────────


def _slide_set_cookie_max_age(set_cookie_headers: list[str]) -> Optional[int]:
    """Pull the session-cookie Max-Age out of the response's Set-Cookie list.

    Returns None when no session cookie was emitted. Used to assert that
    a slide was reflected to the browser, not just to DDB.
    """
    for header in set_cookie_headers:
        if not header.startswith(f"{SESSION_COOKIE_NAME}="):
            continue
        for part in header.split(";"):
            part = part.strip()
            if part.lower().startswith("max-age="):
                return int(part.split("=", 1)[1])
    return None


def test_slide_within_throttle_window_does_not_write_or_reemit() -> None:
    """A request arriving within `sliding_renewal_throttle_seconds` of the
    last touch must not generate a DDB write or re-set the cookie. Without
    the throttle, every request would cost a write."""
    record = _make_record()
    # Pretend the row was touched 5s ago — well inside the 60s throttle.
    record.last_seen_at = int(time.time()) - 5
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    repo.touch_last_seen.assert_not_called()
    repo.update_tokens.assert_not_called()
    # No Set-Cookie for the session cookie — we left the existing one alone.
    assert _slide_set_cookie_max_age(response.headers.get_list("set-cookie")) is None


def test_slide_past_throttle_writes_ddb_and_reemits_cookie() -> None:
    """Once `last_seen_at` is older than the throttle window, the slide
    fires: one DDB touch with a fresh ttl, plus a Set-Cookie carrying a
    fresh Max-Age = session_ttl_seconds.

    The slide-write is fire-and-forget (task 3.5) — we poll for the
    background task's side effect rather than sample immediately. The
    observable external contract (Set-Cookie Max-Age) is unchanged; only
    the internal timing of the write moves off the request path.
    """
    record = _make_record()
    record.last_seen_at = int(time.time()) - 120  # past the 60s throttle
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    with TestClient(app) as client:
        response = client.get("/echo", cookies={SESSION_COOKIE_NAME: sealed})
        # Poll for the fire-and-forget slide-write (task 3.5) INSIDE the
        # `with` block — TestClient tears down its anyio portal (and the
        # event loop) on `__exit__`, cancelling any unfinished tasks.
        # Drive the loop with a second GET if the first request's
        # background task hasn't flushed yet.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and repo.touch_last_seen.await_count == 0:
            time.sleep(0.01)
        if repo.touch_last_seen.await_count == 0:
            client.get("/echo")
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and repo.touch_last_seen.await_count == 0:
                time.sleep(0.01)

    assert response.status_code == 200
    # Exactly one slide-write, and it carries a ttl bumped by ~session_ttl_seconds.
    repo.touch_last_seen.assert_awaited_once()
    args, kwargs = repo.touch_last_seen.await_args
    # session_id passed positionally; last_seen_at/ttl by keyword.
    assert (args[0] if args else kwargs.get("session_id")) == record.session_id
    now = int(time.time())
    assert abs(kwargs["last_seen_at"] - now) < 5
    # ttl must be roughly now + session_ttl_seconds (28800), not the original
    # ttl on the record. Wide window because TestClient adds latency.
    assert kwargs["ttl"] - now > 28000
    repo.update_tokens.assert_not_called()  # slide path, not refresh path

    max_age = _slide_set_cookie_max_age(response.headers.get_list("set-cookie"))
    assert max_age is not None
    assert max_age == 28800


def test_slide_past_absolute_cap_does_not_extend() -> None:
    """When `created_at + absolute_lifetime` has already passed, the slide
    must be a no-op — we don't roll a session beyond its hard cap. The
    user keeps the rest of whatever validity their cookie still has."""
    record = _make_record()
    # absolute_lifetime = 100s; created 200s ago → past the cap.
    record.created_at = int(time.time()) - 200
    record.last_seen_at = int(time.time()) - 120  # past throttle
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(absolute_lifetime_seconds=100),
        repository=repo,
        codec=codec,
        refresh_client=refresh,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    repo.touch_last_seen.assert_not_called()
    assert _slide_set_cookie_max_age(response.headers.get_list("set-cookie")) is None


def test_slide_max_age_capped_by_remaining_absolute_lifetime() -> None:
    """When session_ttl_seconds would exceed remaining absolute lifetime,
    the slide caps Max-Age at the remaining window so the cookie can't
    outlive the absolute cap."""
    record = _make_record()
    # absolute_lifetime = 1000s; created 600s ago → 400s remaining.
    record.created_at = int(time.time()) - 600
    record.last_seen_at = int(time.time()) - 120
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    app = _build_app(
        config=_enabled_config(
            absolute_lifetime_seconds=1000,
            session_ttl_seconds=28800,  # would normally be Max-Age
        ),
        repository=repo,
        codec=codec,
        refresh_client=refresh,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    max_age = _slide_set_cookie_max_age(response.headers.get_list("set-cookie"))
    # Capped near 400s, not 28800s. Wide tolerance for TestClient latency.
    assert max_age is not None
    assert 350 <= max_age <= 400


def test_refresh_path_past_absolute_cap_clears_cookie_without_calling_cognito() -> None:
    """The refresh path must mirror the slide path's absolute-cap behavior:
    once `created_at + absolute_lifetime` has passed, do NOT mint fresh
    tokens. Persisting them would also write a past-dated `ttl`
    (`min(now + session_ttl_seconds, absolute_cap)` is `< now` past the
    cap), which would instantly TTL-evict the row right after the write
    and silently log the user out one request later. Failing closed up
    front avoids burning a Cognito refresh-token rotation we'd just
    throw away."""
    record = _make_record(access_token_exp=int(time.time()) + 5)  # within leeway
    record.created_at = int(time.time()) - 200  # past 100s absolute cap
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    refresh.refresh = AsyncMock(
        side_effect=AssertionError(
            "Cognito refresh MUST NOT be called past absolute lifetime"
        )
    )
    app = _build_app(
        config=_enabled_config(absolute_lifetime_seconds=100),
        repository=repo,
        codec=codec,
        refresh_client=refresh,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    assert response.json()["has_session"] is False
    refresh.refresh.assert_not_called()
    repo.update_tokens.assert_not_called()
    cleared = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in cleared and "Max-Age=0" in cleared


def test_refresh_path_bumps_ttl_when_persisting_tokens() -> None:
    """The token-rotation write must also slide the row's ttl forward —
    otherwise a session that just refreshed could still expire moments
    later because the original ttl was set at login."""
    record = _make_record(access_token_exp=int(time.time()) + 5)  # within leeway
    repo = AsyncMock()
    repo.get.return_value = record
    codec = _make_codec()
    refresh = MagicMock()
    refresh.refresh = AsyncMock(
        return_value=RefreshResult(
            access_token="access.fresh",
            refresh_token="refresh.original",  # no rotation
            id_token="id.fresh",
            access_token_exp=int(time.time()) + 3600,
        )
    )
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    repo.update_tokens.assert_awaited_once()
    kwargs = repo.update_tokens.await_args.kwargs
    assert "ttl" in kwargs
    now = int(time.time())
    assert kwargs["ttl"] - now > 28000  # bumped by ~session_ttl_seconds


# ─── Refresh-token rotation hardening ───────────────────────────────────


def test_rotation_persist_failure_invalidates_session() -> None:
    """When Cognito rotates the refresh token (returns a new one) AND every
    DDB write retry fails, the session must be invalidated immediately:
    the old refresh token is dead at Cognito, so leaving the row stale
    guarantees a silent 401 on the next request. Better to force re-auth
    now while the user is in the loop."""
    record = _make_record(access_token_exp=int(time.time()) + 5)
    repo = AsyncMock()
    repo.get.return_value = record
    repo.update_tokens.side_effect = RuntimeError("DDB throttled")
    codec = _make_codec()
    refresh = MagicMock()
    refresh.refresh = AsyncMock(
        return_value=RefreshResult(
            access_token="access.fresh",
            refresh_token="refresh.ROTATED",  # rotation kicked in
            id_token="id.fresh",
            access_token_exp=int(time.time()) + 3600,
        )
    )
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    assert response.json()["has_session"] is False
    # Three retry attempts on rotation.
    assert repo.update_tokens.await_count == 3
    # Cookie must be cleared so the browser stops carrying a now-zombie session.
    cleared = " ".join(response.headers.get_list("set-cookie"))
    assert SESSION_COOKIE_NAME in cleared


def test_non_rotation_persist_failure_does_not_invalidate() -> None:
    """If Cognito did NOT rotate (returned the same refresh token) and the
    DDB write fails, the session is still serviceable — the existing
    refresh token is still valid for the next attempt. Don't punish the
    user with a re-auth for a transient DDB blip."""
    record = _make_record(access_token_exp=int(time.time()) + 5)
    repo = AsyncMock()
    repo.get.return_value = record
    repo.update_tokens.side_effect = RuntimeError("DDB throttled")
    codec = _make_codec()
    refresh = MagicMock()
    refresh.refresh = AsyncMock(
        return_value=RefreshResult(
            access_token="access.fresh",
            refresh_token="refresh.original",  # SAME — no rotation
            id_token="id.fresh",
            access_token_exp=int(time.time()) + 3600,
        )
    )
    app = _build_app(
        config=_enabled_config(), repository=repo, codec=codec, refresh_client=refresh
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    body = response.json()
    assert body["has_session"] is True
    assert body["access_token"] == "access.fresh"  # in-memory record updated
    # Single attempt only when no rotation.
    assert repo.update_tokens.await_count == 1
    # Cookie must NOT be cleared.
    cleared = " ".join(response.headers.get_list("set-cookie"))
    assert "Max-Age=0" not in cleared


