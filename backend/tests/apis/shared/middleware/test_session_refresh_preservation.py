"""Preservation property tests for SessionRefreshMiddleware.

Property 2: BFF Middleware Contracts Unchanged for Non-Buggy Inputs.

This file encodes the observable contracts (Preservation Requirements 3.1–3.11)
that the event-loop-blocking fix MUST preserve. Tests are run on UNFIXED code
first and MUST PASS — confirming the baseline behavior to lock in. After the
fix lands (task 3.x series) these same tests must continue to pass with no
modifications.

Observation-first methodology: each preservation test encodes behavior
OBSERVED on today's code — response status, `Set-Cookie` headers (including
every attribute), `request.state.bff_session`, `request.state.bff_csrf_token`,
DDB call counts, Cognito call counts, KMS/Secrets Manager call counts — rather
than re-derived from the spec.

The hypothesis strategies cover the axes that exist today: `is_enabled()`
true/false, `__Host-bff_session` cookie present/absent, cookie seal
valid/invalid/expired, `SessionCache` hit/miss, `needs_refresh` yes/no,
refresh-token rotation yes/no, slide warranted yes/no, absolute-lifetime cap
passed yes/no, request method safe/unsafe. Inputs that themselves reproduce
an isBugCondition sub-condition (fan-outs at aligned boundaries, slide timing
vs response timing, etc.) are avoided — preservation is about the externally
observable contract, not about how many DDB calls happen under bug-triggering
inputs.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apis.shared.middleware.csrf import CSRFMiddleware
from apis.shared.middleware.session_refresh import SessionRefreshMiddleware
from apis.shared.sessions_bff import cache as cache_module
from apis.shared.sessions_bff import cookie as cookie_module
from apis.shared.sessions_bff import lock as lock_module
from apis.shared.sessions_bff import refresh as refresh_module
from apis.shared.sessions_bff.cache import SessionCache
from apis.shared.sessions_bff.config import (
    BFFConfig,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    _DEFAULT_REFRESH_LEEWAY_SECONDS,
)
from apis.shared.sessions_bff.cookie import CookieCodec, get_default_codec
from apis.shared.sessions_bff.csrf import CSRFHelper
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord
from apis.shared.sessions_bff.refresh import (
    CognitoRefreshClient,
    CognitoRefreshError,
    RefreshResult,
    _reset_secret_cache_for_tests,
    resolve_bff_client_secret,
)
from apis.shared.sessions_bff.repository import SessionRepository


# ═══════════════════════════════════════════════════════════════════════════
# Shared helpers — duplicated from test_session_refresh_bug_condition.py for
# test-file isolation. Keep the two files' helper shapes in sync.
# ═══════════════════════════════════════════════════════════════════════════


class InstrumentedTable:
    """Synchronous fake of a boto3 DynamoDB Table.

    Records call counts so preservation tests can assert "zero AWS calls"
    for dormant / no-cookie pass-through paths, and "exactly one get_item"
    for the refresh-storm coalescing contract.

    `update_item` writes are classified into three kinds by inspecting the
    `UpdateExpression`:
      - `lock_acquire_calls`: cross-task refresh-lock acquisition (writes
        `refresh_lock_owner` + `refresh_lock_until`, no token columns).
      - `token_persist_calls`: token rotation write (sets
        `cognito_access_token` etc., usually also REMOVE-ing the lock).
      - `slide_calls`: sliding-renewal touch (writes only `last_seen_at`
        and optionally `ttl`).
    `update_item_calls` remains the total (sum) so existing assertions on
    "any update_item issued" continue to hold. The injected side-effect is
    applied only to the token-persist path so tests that simulate "DDB
    throttled during persist" don't accidentally fail at the lock-acquire
    write — that's a different code path with different recovery semantics.
    """

    def __init__(
        self,
        *,
        record: Optional[SessionRecord] = None,
        delay_s: float = 0.0,
        update_item_side_effect: Optional[Exception] = None,
    ) -> None:
        self._delay_s = delay_s
        self._record = record
        self._update_item_side_effect = update_item_side_effect
        self.get_item_calls = 0
        self.update_item_calls = 0
        self.lock_acquire_calls = 0
        self.token_persist_calls = 0
        self.slide_calls = 0
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

    @staticmethod
    def _classify_update(update_expr: str) -> str:
        """Classify which middleware path issued this update_item.

        Token persist writes always set `cognito_access_token`. Pure lock
        acquires write `refresh_lock_owner` without touching tokens. Slide
        writes touch only `last_seen_at` (+ optionally `ttl`).
        """
        if "cognito_access_token" in update_expr:
            return "token_persist"
        if "refresh_lock_owner" in update_expr:
            return "lock_acquire"
        return "slide"

    def update_item(self, **kwargs: Any) -> dict:
        self.update_item_calls += 1
        kind = self._classify_update(kwargs.get("UpdateExpression", ""))
        if kind == "token_persist":
            self.token_persist_calls += 1
        elif kind == "lock_acquire":
            self.lock_acquire_calls += 1
        else:
            self.slide_calls += 1
        self._sleep()
        # Side-effect injection applies only to the token-persist path —
        # tests that simulate "rotation persist exhausted" mean exactly
        # that write, not the upstream lock-acquire.
        if self._update_item_side_effect is not None and kind == "token_persist":
            raise self._update_item_side_effect
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
    """SessionRepository backed by an InstrumentedTable.

    Bypasses boto3.resource() by starting disabled, then flipping `_enabled`
    and injecting the fake table. Exercises the real repository async-method
    bodies so preservation tests see the production code path.
    """
    repo = SessionRepository(table_name="")
    repo._enabled = True
    repo._table = table  # type: ignore[assignment]
    repo._table_name = "test-bff-sessions"
    return repo


def _make_codec() -> CookieCodec:
    codec = CookieCodec(kms_key_arn="arn:aws:kms:fake")
    codec._cipher = AESGCM(secrets.token_bytes(32))
    return codec


def _make_record(
    *,
    session_id: str = "sess-pres-001",
    access_token_exp: Optional[int] = None,
    last_seen_at: Optional[int] = None,
    created_at: Optional[int] = None,
    ttl: Optional[int] = None,
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
        ttl=ttl if ttl is not None else now + 28800,
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
        sliding_renewal_throttle_seconds=60,
    )
    defaults.update(overrides)
    return BFFConfig(**defaults)


def _disabled_config() -> BFFConfig:
    return BFFConfig(
        sessions_table_name=None,
        cookie_signing_key_arn=None,
        session_ttl_seconds=28800,
        refresh_leeway_seconds=60,
        cognito_bff_app_client_id=None,
        cognito_bff_app_client_secret_arn=None,
        inference_api_url=None,
    )


def _build_app(
    *,
    config: BFFConfig,
    repository: Any,
    codec: CookieCodec,
    refresh_client: Any,
    cache: Optional[SessionCache] = None,
    include_csrf: bool = False,
) -> FastAPI:
    app = FastAPI()
    if include_csrf:
        # Added first → innermost relative to SessionRefreshMiddleware.
        # Request order: SessionRefresh → CSRF → route.
        app.add_middleware(CSRFMiddleware)
    app.add_middleware(
        SessionRefreshMiddleware,
        config=config,
        repository=repository,
        cookie_codec=codec,
        refresh_client=refresh_client,
        cache=cache or SessionCache(ttl_seconds=60),
    )

    @app.get("/echo")
    async def echo_get(request: Request) -> dict:
        record = getattr(request.state, "bff_session", None)
        csrf = getattr(request.state, "bff_csrf_token", None)
        return {
            "has_session": record is not None,
            "session_id": record.session_id if record else None,
            "access_token": record.cognito_access_token if record else None,
            "csrf_token": csrf,
        }

    @app.post("/submit")
    async def submit_post(request: Request) -> dict:
        record = getattr(request.state, "bff_session", None)
        return {
            "has_session": record is not None,
            "session_id": record.session_id if record else None,
        }

    return app


@pytest.fixture(autouse=True)
def _reset_session_state() -> Any:
    """Clear process-wide state between tests."""
    lock_module._reset_for_tests()
    _reset_secret_cache_for_tests()
    cache_module._reset_default_cache_for_tests()
    cookie_module._reset_default_codec_for_tests()
    yield
    lock_module._reset_for_tests()
    _reset_secret_cache_for_tests()
    cache_module._reset_default_cache_for_tests()
    cookie_module._reset_default_codec_for_tests()


# ═══════════════════════════════════════════════════════════════════════════
# Set-Cookie parsing helpers — the preservation contract on cookie attributes
# is observed from the raw `Set-Cookie` header, so we parse it here.
# ═══════════════════════════════════════════════════════════════════════════


def _parse_set_cookie(header: str) -> dict[str, Any]:
    """Parse a raw Set-Cookie header into {name, value, attributes}.

    Attributes are keyed case-folded for reliable membership checks.
    Boolean attributes (HttpOnly, Secure) map to True.
    """
    parts = [p.strip() for p in header.split(";")]
    name, _, value = parts[0].partition("=")
    attrs: dict[str, Any] = {}
    for attr in parts[1:]:
        if "=" in attr:
            k, _, v = attr.partition("=")
            attrs[k.strip().lower()] = v.strip()
        else:
            attrs[attr.strip().lower()] = True
    return {"name": name.strip(), "value": value.strip(), "attrs": attrs}


def _find_set_cookies(
    response_headers: Any, cookie_name: str
) -> list[dict[str, Any]]:
    """Return every parsed Set-Cookie for a given cookie name."""
    parsed = []
    for header in response_headers.get_list("set-cookie"):
        pc = _parse_set_cookie(header)
        if pc["name"] == cookie_name:
            parsed.append(pc)
    return parsed


def _wait_for(predicate: Any, *, timeout_s: float = 1.0, interval_s: float = 0.01) -> bool:
    """Poll ``predicate`` until it returns truthy or ``timeout_s`` elapses.

    The slide-write path became fire-and-forget in task 3.5 — `_maybe_slide`
    schedules the DDB `touch_last_seen` on a detached `asyncio.create_task`
    and returns the Max-Age synchronously. `TestClient` returns the response
    before the scheduled task has a chance to run on slower CI schedulers,
    so assertions about `update_item_calls == 1` must poll rather than
    sample immediately. The observable external contract (cookie attributes,
    Max-Age, response body) is unchanged — only the internal timing of the
    background write moves.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.1 — Dormant pass-through with zero AWS calls
# ═══════════════════════════════════════════════════════════════════════════


# Cookie-safe ASCII: printable, no semicolons/commas/whitespace/control chars —
# httpx's cookiejar only accepts ASCII values and rejects the RFC 6265 separators.
_COOKIE_SAFE_ALPHABET = st.characters(
    min_codepoint=0x21,
    max_codepoint=0x7E,
    blacklist_characters=";, \t\"\\",
)


@given(
    method=st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
    path=st.sampled_from(["/echo", "/submit"]),
    with_cookie=st.booleans(),
    cookie_value=st.text(alphabet=_COOKIE_SAFE_ALPHABET, min_size=0, max_size=64),
)
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_3_1_dormant_passthrough_zero_aws_calls(
    method: str, path: str, with_cookie: bool, cookie_value: str
) -> None:
    """(3.1) Dormant pass-through.

    When `BFFConfig.is_enabled() == False`, every request shape (method,
    path, cookie present/absent) short-circuits through `call_next(request)`
    with zero DDB calls and zero Cognito calls.
    """
    table = InstrumentedTable()
    repo = _make_repo(table)
    # Force the repo into the "enabled" posture so we'd observe a call if
    # the middleware mistakenly went past its `is_enabled()` guard.
    codec = _make_codec()
    refresh_client = MagicMock()
    app = _build_app(
        config=_disabled_config(),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
    )

    cookies: dict[str, str] = {}
    if with_cookie:
        cookies[SESSION_COOKIE_NAME] = cookie_value

    with TestClient(app) as client:
        response = client.request(method, path, cookies=cookies)

    # OPTIONS/HEAD may be allowed or not depending on route — we only care
    # that the middleware did not touch AWS regardless of status.
    assert response.status_code < 500, (
        f"[3.1] dormant pass-through produced 5xx for {method} {path}: "
        f"{response.status_code}"
    )
    assert table.get_item_calls == 0, (
        f"[3.1] dormant middleware issued {table.get_item_calls} get_item "
        f"calls — must be zero when is_enabled() == False"
    )
    assert table.update_item_calls == 0, (
        f"[3.1] dormant middleware issued {table.update_item_calls} "
        "update_item calls — must be zero when is_enabled() == False"
    )
    assert table.put_item_calls == 0
    assert table.delete_item_calls == 0
    refresh_client.refresh.assert_not_called()
    # No Set-Cookie emitted by the middleware when dormant.
    assert response.headers.get_list("set-cookie") == []


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.2 — No-cookie pass-through with zero AWS calls
# ═══════════════════════════════════════════════════════════════════════════


@given(
    method=st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE"]),
    path=st.sampled_from(["/echo", "/submit"]),
)
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_3_2_no_cookie_passthrough_zero_aws_calls(
    method: str, path: str
) -> None:
    """(3.2) No-cookie pass-through.

    When `is_enabled() == True` but no `__Host-bff_session` cookie is present
    (Bearer-token requests, anonymous endpoints), the middleware must pass
    through with zero AWS calls and no `request.state.bff_session`.
    """
    table = InstrumentedTable()
    repo = _make_repo(table)
    codec = _make_codec()
    refresh_client = MagicMock()
    app = _build_app(
        config=_enabled_config(),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
    )

    with TestClient(app) as client:
        response = client.request(method, path)

    assert response.status_code < 500
    # When the call returned 200 with body, the handler reports has_session=False.
    if response.status_code == 200 and response.headers.get(
        "content-type", ""
    ).startswith("application/json"):
        body = response.json()
        assert body["has_session"] is False, (
            "[3.2] state.bff_session must NOT be set when no cookie is present"
        )
    assert table.get_item_calls == 0, (
        f"[3.2] no-cookie path issued {table.get_item_calls} get_item calls"
    )
    assert table.update_item_calls == 0
    assert table.put_item_calls == 0
    assert table.delete_item_calls == 0
    refresh_client.refresh.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.3 — Unrecoverable cookie clears BOTH cookies with matching attrs
# ═══════════════════════════════════════════════════════════════════════════


def _assert_clear_cookie_attrs(parsed: dict[str, Any]) -> None:
    """Attributes observed today on a cleared BFF cookie:

        Max-Age=0; Path=/; SameSite=lax; Secure

    HttpOnly is present on the session cookie only (intentional: the CSRF
    cookie is JS-readable). All other attributes are identical across both
    cookies.
    """
    attrs = parsed["attrs"]
    assert attrs.get("max-age") == "0", (
        f"[3.3] clear must set Max-Age=0; got attrs={attrs}"
    )
    assert attrs.get("path") == "/", (
        f"[3.3] clear must set Path=/; got attrs={attrs}"
    )
    assert attrs.get("samesite") == "lax", (
        f"[3.3] clear must set SameSite=lax; got attrs={attrs}"
    )
    assert attrs.get("secure") is True, (
        f"[3.3] clear must set Secure; got attrs={attrs}"
    )


@pytest.mark.parametrize(
    "scenario",
    ["bad_seal", "missing_row", "expired_row", "terminal_refresh_error"],
)
def test_3_3_unrecoverable_cookie_clears_both_cookies_with_matching_attrs(
    scenario: str,
) -> None:
    """(3.3) Unrecoverable cookie → clear both.

    Bad-seal, missing-row, expired-row, and terminal-`CognitoRefreshError`
    inputs all produce Set-Cookie for both `__Host-bff_session` and
    `__Host-bff_csrf` with `Max-Age=0` and the today-observed attribute set.
    The HttpOnly attribute intentionally differs between the two (session
    is HttpOnly; CSRF is JS-readable by design); all other attrs match.
    """
    codec = _make_codec()
    refresh_client = MagicMock()

    if scenario == "bad_seal":
        table = InstrumentedTable()
        cookie_value = "not-a-sealed-cookie"
    elif scenario == "missing_row":
        # No record on the table — get_item returns {} → record None.
        table = InstrumentedTable(record=None)
        cookie_value = codec.seal(CookiePayload(session_id="sess-gone"))
    elif scenario == "expired_row":
        # TTL in the past — repository treats as missing (defense in depth).
        expired = _make_record(ttl=int(time.time()) - 10)
        table = InstrumentedTable(record=expired)
        cookie_value = codec.seal(CookiePayload(session_id=expired.session_id))
    elif scenario == "terminal_refresh_error":
        # Access token within leeway → refresh path → Cognito raises.
        rec = _make_record(access_token_exp=int(time.time()) + 5)
        table = InstrumentedTable(record=rec)
        cookie_value = codec.seal(CookiePayload(session_id=rec.session_id))
        refresh_client.refresh.side_effect = CognitoRefreshError("rotated-dead")
    else:
        pytest.fail(f"unknown scenario: {scenario}")

    repo = _make_repo(table)
    app = _build_app(
        config=_enabled_config(),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
    )

    with TestClient(app) as client:
        response = client.get("/echo", cookies={SESSION_COOKIE_NAME: cookie_value})

    assert response.status_code == 200
    assert response.json()["has_session"] is False, (
        f"[3.3/{scenario}] state.bff_session must NOT be set after clear"
    )

    session_clears = _find_set_cookies(response.headers, SESSION_COOKIE_NAME)
    csrf_clears = _find_set_cookies(response.headers, CSRF_COOKIE_NAME)
    assert len(session_clears) == 1, (
        f"[3.3/{scenario}] expected exactly one Set-Cookie for "
        f"{SESSION_COOKIE_NAME}; got {len(session_clears)}"
    )
    assert len(csrf_clears) == 1, (
        f"[3.3/{scenario}] expected exactly one Set-Cookie for "
        f"{CSRF_COOKIE_NAME}; got {len(csrf_clears)}"
    )

    # Each cleared cookie carries Max-Age=0 and the shared attribute set.
    _assert_clear_cookie_attrs(session_clears[0])
    _assert_clear_cookie_attrs(csrf_clears[0])

    # HttpOnly is the one documented difference between the two cookies.
    assert session_clears[0]["attrs"].get("httponly") is True, (
        f"[3.3/{scenario}] session cookie must remain HttpOnly on clear"
    )
    assert csrf_clears[0]["attrs"].get("httponly") is not True, (
        f"[3.3/{scenario}] CSRF cookie must NOT be HttpOnly (JS must read it)"
    )

    # Shared (non-HttpOnly) attribute set is identical across the two clears.
    shared_keys = {"max-age", "path", "samesite", "secure"}
    sess_shared = {k: session_clears[0]["attrs"].get(k) for k in shared_keys}
    csrf_shared = {k: csrf_clears[0]["attrs"].get(k) for k in shared_keys}
    assert sess_shared == csrf_shared, (
        f"[3.3/{scenario}] shared clear attrs diverge: "
        f"session={sess_shared}, csrf={csrf_shared}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.4 — Max-Age re-emit contract (slide path)
# ═══════════════════════════════════════════════════════════════════════════


@given(
    # Session TTL bounded so it always fits well within the absolute cap.
    session_ttl=st.integers(min_value=120, max_value=28800),
    # Time since the last touch — past the throttle so a slide is warranted.
    seconds_since_last_seen=st.integers(min_value=61, max_value=3600),
)
@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_3_4_slide_max_age_matches_on_both_cookies(
    session_ttl: int, seconds_since_last_seen: int
) -> None:
    """(3.4) Max-Age re-emit contract.

    When `_maybe_slide` returns a non-None Max-Age, the Set-Cookie headers
    for BOTH `__Host-bff_session` and `__Host-bff_csrf` carry that exact
    Max-Age and the attribute set observed today on `_reemit_cookies`:

        Session:  HttpOnly; Max-Age=<n>; Path=/; SameSite=lax; Secure
        CSRF:                Max-Age=<n>; Path=/; SameSite=lax; Secure
    """
    now = int(time.time())
    record = _make_record(last_seen_at=now - seconds_since_last_seen)
    table = InstrumentedTable(record=record)
    repo = _make_repo(table)
    codec = _make_codec()
    refresh_client = MagicMock()
    # Large absolute lifetime so the slide is not capped — the Max-Age we
    # get back must equal session_ttl_seconds exactly.
    app = _build_app(
        config=_enabled_config(
            session_ttl_seconds=session_ttl,
            absolute_lifetime_seconds=30 * 24 * 3600,
            sliding_renewal_throttle_seconds=60,
        ),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    with TestClient(app) as client:
        response = client.get("/echo", cookies={SESSION_COOKIE_NAME: sealed})
        # Slide-write is fire-and-forget (task 3.5) — drive the event
        # loop with a second request to let the background task from the
        # first request flush. MUST happen inside the `with TestClient`
        # block because TestClient tears down its anyio portal (and the
        # event loop) on `__exit__`, which cancels any pending tasks.
        _wait_for(lambda: table.update_item_calls >= 1)
        if table.update_item_calls == 0:
            # A no-op second request keeps the event loop alive long
            # enough for the pending slide task to run.
            client.get("/echo")
            _wait_for(lambda: table.update_item_calls >= 1)

    assert response.status_code == 200
    # Slide must have fired exactly once (one DDB update_item).
    assert table.update_item_calls == 1, (
        f"[3.4] slide must issue exactly one update_item; got "
        f"{table.update_item_calls}"
    )

    session_emits = _find_set_cookies(response.headers, SESSION_COOKIE_NAME)
    csrf_emits = _find_set_cookies(response.headers, CSRF_COOKIE_NAME)
    assert len(session_emits) == 1, (
        f"[3.4] expected exactly one Set-Cookie for {SESSION_COOKIE_NAME}"
    )
    assert len(csrf_emits) == 1, (
        f"[3.4] expected exactly one Set-Cookie for {CSRF_COOKIE_NAME}"
    )

    sess_attrs = session_emits[0]["attrs"]
    csrf_attrs = csrf_emits[0]["attrs"]

    # Max-Age equals session_ttl_seconds on BOTH cookies (no absolute cap).
    assert sess_attrs.get("max-age") == str(session_ttl), (
        f"[3.4] session cookie Max-Age mismatch: expected {session_ttl}, "
        f"got {sess_attrs.get('max-age')}"
    )
    assert csrf_attrs.get("max-age") == str(session_ttl), (
        f"[3.4] csrf cookie Max-Age mismatch: expected {session_ttl}, "
        f"got {csrf_attrs.get('max-age')}"
    )

    # Attribute set observed on today's _reemit_cookies:
    assert sess_attrs.get("path") == "/"
    assert sess_attrs.get("samesite") == "lax"
    assert sess_attrs.get("secure") is True
    assert sess_attrs.get("httponly") is True

    assert csrf_attrs.get("path") == "/"
    assert csrf_attrs.get("samesite") == "lax"
    assert csrf_attrs.get("secure") is True
    # CSRF is JS-readable → MUST NOT be HttpOnly.
    assert csrf_attrs.get("httponly") is not True

    # Shared (non-HttpOnly) attribute set is identical.
    shared = {"max-age", "path", "samesite", "secure"}
    assert {k: sess_attrs.get(k) for k in shared} == {
        k: csrf_attrs.get(k) for k in shared
    }

    # The sealed value on the session cookie is the exact same value the
    # browser already held — slide doesn't mint a new seal.
    assert session_emits[0]["value"] == sealed, (
        "[3.4] slide must re-emit the same sealed session value, not a new seal"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.5 — Refresh-storm coalescing preserved (one initiate_auth per
# session per leeway window)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_3_5_refresh_storm_coalesces_to_single_initiate_auth() -> None:
    """(3.5) Refresh-storm coalescing.

    10 concurrent same-session requests crossing the refresh-leeway window
    must drive exactly ONE `cognito-idp:initiate_auth` call (the existing
    per-session lock coalescing contract). The fix MUST preserve this.
    """
    now = int(time.time())
    record = _make_record(access_token_exp=now + 5)  # within 60s leeway
    table = InstrumentedTable(record=record)
    repo = _make_repo(table)
    codec = _make_codec()

    refresh_call_count = {"n": 0}

    async def _refresh(*, username: str, refresh_token: str) -> RefreshResult:
        refresh_call_count["n"] += 1
        return RefreshResult(
            access_token=f"access.fresh.{refresh_call_count['n']}",
            refresh_token="refresh.original",  # no rotation
            id_token="id.fresh",
            access_token_exp=int(time.time()) + 3600,
        )

    refresh_client = MagicMock()
    refresh_client.refresh = AsyncMock(side_effect=_refresh)

    # After the first refresh lands, later repo.get calls should observe
    # a record that no longer needs refresh (the update_item write is a
    # no-op on the fake, so we pre-refresh the in-memory record copy).
    fresh = _make_record(
        session_id=record.session_id, access_token_exp=now + 3600
    )
    fresh.cognito_access_token = "access.fresh.1"
    # Sequential responses: first few see the stale record, then the fresh one.
    table._record = record  # starts stale
    original_get_item = table.get_item

    get_item_counter = {"n": 0}

    def counting_get_item(Key: dict) -> dict:
        get_item_counter["n"] += 1
        # After the leader's update_item bumps tokens, followers arriving
        # late should see the fresh record. Flip after 2 calls so both
        # pre-lock and post-lock rechecks on the leader path see the stale row.
        if get_item_counter["n"] > 2:
            table._record = fresh
        return original_get_item(Key)

    table.get_item = counting_get_item  # type: ignore[assignment]

    app = _build_app(
        config=_enabled_config(),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        client.cookies.set(SESSION_COOKIE_NAME, sealed)
        responses = await asyncio.gather(
            *(client.get("/echo") for _ in range(10))
        )

    for r in responses:
        assert r.status_code == 200

    assert refresh_call_count["n"] == 1, (
        f"[3.5] 10 concurrent same-session requests drove "
        f"{refresh_call_count['n']} Cognito initiate_auth calls — exactly "
        "one is required per session per leeway window (existing "
        "get_session_lock coalescing)."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.6 — Codec singleton, zero per-request KMS GenerateDataKey
# ═══════════════════════════════════════════════════════════════════════════


def test_3_6_get_default_codec_is_singleton_with_no_per_request_kms() -> None:
    """(3.6) Codec singleton.

    `get_default_codec()` returns the same instance across calls. The
    underlying `secretsmanager:GetSecretValue` call happens at most once
    per process. Hot seal/unseal traffic must not re-fetch.

    (This contract held under the original `kms:GenerateDataKey`-per-process
    design and the interim KMS-wrap design too; only the underlying AWS
    APIs and KDF changed when the codec was moved to a shared
    Secrets-Manager-generated secret for cross-task seal/unseal.)
    """
    sm_client = MagicMock()
    sm_client.get_secret_value.return_value = {
        "SecretString": "secret-3-6-high-entropy-1234567890ABCDEFGHIJ"
    }

    codec = CookieCodec(
        kms_key_arn="arn:aws:kms:fake-3.6",
        data_key_secret_arn="arn:aws:secretsmanager:fake-3.6",
        secrets_manager_client=sm_client,
    )
    cookie_module._set_default_codec_for_tests(codec)

    first = get_default_codec()
    for _ in range(25):
        other = get_default_codec()
        assert other is first, (
            "[3.6] get_default_codec() must return the same instance each call"
        )

    payload = CookiePayload(session_id="sess-3-6")
    for _ in range(20):
        sealed = first.seal(payload)
        roundtripped = first.unseal(sealed)
        assert roundtripped.session_id == "sess-3-6"

    assert sm_client.get_secret_value.call_count <= 1, (
        f"[3.6] Secrets Manager get_secret_value invoked "
        f"{sm_client.get_secret_value.call_count} times — must be at most "
        "one per process."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.7 — Client-secret cache, one Secrets Manager hit per process
# ═══════════════════════════════════════════════════════════════════════════


def test_3_7_client_secret_cache_one_secrets_manager_hit_per_process() -> None:
    """(3.7) Client-secret cache.

    `resolve_bff_client_secret()` must hit Secrets Manager exactly once per
    process regardless of how many times it is called.
    """
    sm_client = MagicMock()
    sm_client.get_secret_value.return_value = {"SecretString": "client-secret-A"}

    first = resolve_bff_client_secret(
        secret_arn="arn:secret",
        region="us-east-1",
        secrets_manager_client=sm_client,
    )
    assert first == "client-secret-A"

    # Many subsequent calls — even with a fresh SM client — must not drive
    # a new GetSecretValue, because the first call populated the cache.
    for _ in range(50):
        value = resolve_bff_client_secret(
            secret_arn="arn:secret",
            region="us-east-1",
            secrets_manager_client=sm_client,
        )
        assert value == "client-secret-A"

    assert sm_client.get_secret_value.call_count == 1, (
        f"[3.7] Secrets Manager get_secret_value called "
        f"{sm_client.get_secret_value.call_count} times — must be exactly one."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.8 — CSRFMiddleware accept/reject unchanged, no new I/O
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "case",
    ["matching", "mismatched", "header_only", "cookie_only", "forged_pair", "missing"],
)
def test_3_8_csrf_decision_unchanged_with_zero_new_io(case: str) -> None:
    """(3.8) CSRF path unchanged.

    With `SessionRefreshMiddleware` upstream populating `state.bff_session`,
    the `CSRFMiddleware` accept/reject decision on unsafe-method requests
    matches today's observed behavior across all five CSRF token cases.
    No new DDB / Cognito / KMS / Secrets Manager I/O is introduced on the
    CSRF path.
    """
    record = _make_record()
    table = InstrumentedTable(record=record)
    repo = _make_repo(table)
    codec = _make_codec()
    refresh_client = MagicMock()
    app = _build_app(
        config=_enabled_config(),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
        include_csrf=True,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    valid_token = CSRFHelper.derive_token(record.csrf_secret, record.session_id)
    forged_token = "0" * 32

    headers: dict[str, str] = {}
    cookies: dict[str, str] = {SESSION_COOKIE_NAME: sealed}

    if case == "matching":
        headers[CSRF_HEADER_NAME] = valid_token
        cookies[CSRF_COOKIE_NAME] = valid_token
        expected_status = 200
    elif case == "mismatched":
        headers[CSRF_HEADER_NAME] = valid_token
        cookies[CSRF_COOKIE_NAME] = "different-value"
        expected_status = 403
    elif case == "header_only":
        headers[CSRF_HEADER_NAME] = valid_token
        expected_status = 403
    elif case == "cookie_only":
        cookies[CSRF_COOKIE_NAME] = valid_token
        expected_status = 403
    elif case == "forged_pair":
        headers[CSRF_HEADER_NAME] = forged_token
        cookies[CSRF_COOKIE_NAME] = forged_token
        expected_status = 403
    elif case == "missing":
        expected_status = 403
    else:
        pytest.fail(f"unknown case: {case}")

    # Snapshot AWS call counters BEFORE the CSRF-exercising request.
    # (Session resolve may have happened on-open via middleware init; we
    # expect exactly one get_item for the resolve, and zero writes.)
    initial_refresh_calls = refresh_client.refresh.call_count
    initial_update_calls = table.update_item_calls

    with TestClient(app) as client:
        response = client.post("/submit", headers=headers, cookies=cookies)

    assert response.status_code == expected_status, (
        f"[3.8/{case}] unexpected CSRF decision: expected {expected_status}, "
        f"got {response.status_code}"
    )
    # Zero NEW Cognito / DDB write I/O on the CSRF path itself.
    assert refresh_client.refresh.call_count == initial_refresh_calls, (
        f"[3.8/{case}] CSRF path triggered an unexpected Cognito refresh"
    )
    # CSRF itself never writes to DDB.
    assert table.update_item_calls - initial_update_calls <= 1, (
        f"[3.8/{case}] more than one update_item observed — at most the "
        "preceding session-resolve slide is expected."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.9 — Absolute-lifetime cap returns None from _maybe_slide
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_3_9_maybe_slide_returns_none_past_absolute_cap() -> None:
    """(3.9) Absolute-lifetime cap.

    When `now > created_at + absolute_lifetime_seconds`, `_maybe_slide`
    returns `None` (no cookie re-emit, no DDB write).
    """
    now = int(time.time())
    # Session was created 200s ago with an absolute lifetime of 100s → cap
    # was reached 100s ago. last_seen_at is past the throttle so otherwise
    # a slide would be warranted.
    record = _make_record(
        created_at=now - 200,
        last_seen_at=now - 120,
    )
    table = InstrumentedTable(record=record)
    repo = _make_repo(table)
    codec = _make_codec()
    refresh_client = MagicMock()
    config = _enabled_config(
        absolute_lifetime_seconds=100,
        sliding_renewal_throttle_seconds=60,
    )

    # Build the middleware directly so we can invoke _maybe_slide in
    # isolation — the preservation contract is specifically that the
    # method returns None past the cap.
    middleware = SessionRefreshMiddleware(
        app=FastAPI(),
        config=config,
        repository=repo,
        cookie_codec=codec,
        refresh_client=refresh_client,
        cache=SessionCache(ttl_seconds=60),
    )
    middleware._ensure_collaborators()

    result = await middleware._maybe_slide(record)
    assert result is None, (
        f"[3.9] _maybe_slide must return None past the absolute cap; "
        f"got {result!r}"
    )
    assert table.update_item_calls == 0, (
        f"[3.9] _maybe_slide must NOT schedule a DDB write past the cap; "
        f"observed {table.update_item_calls} update_item calls."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.10 — Fail-closed rotation: cache invalidated AND cookies cleared
# ═══════════════════════════════════════════════════════════════════════════


def test_3_10_rotation_persist_exhausts_invalidates_cache_and_clears_cookies() -> None:
    """(3.10) Fail-closed rotation.

    When refresh-token rotation kicks in AND `_persist_refresh` exhausts all
    retries (update_item fails every time), the middleware MUST:
      (a) invalidate the cache entry for this session
      (b) clear BOTH BFF cookies on the response
    so the user is forced to re-authenticate before their next request
    hits a dead refresh token.
    """
    now = int(time.time())
    # Access token within leeway → refresh path.
    record = _make_record(access_token_exp=now + 5)
    table = InstrumentedTable(
        record=record,
        update_item_side_effect=RuntimeError("DDB throttled"),
    )
    repo = _make_repo(table)
    codec = _make_codec()
    refresh_client = MagicMock()
    # Rotation kicks in — refresh_token differs from current.
    refresh_client.refresh = AsyncMock(
        return_value=RefreshResult(
            access_token="access.fresh",
            refresh_token="refresh.ROTATED",
            id_token="id.fresh",
            access_token_exp=now + 3600,
        )
    )

    cache = SessionCache(ttl_seconds=60)
    # Pre-seed the cache so we can verify invalidation.
    cache.set(record)
    assert cache.get(record.session_id) is not None

    app = _build_app(
        config=_enabled_config(),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
        cache=cache,
    )

    sealed = codec.seal(CookiePayload(session_id=record.session_id))
    with TestClient(app) as client:
        response = client.get("/echo", cookies={SESSION_COOKIE_NAME: sealed})

    assert response.status_code == 200
    assert response.json()["has_session"] is False, (
        "[3.10] state.bff_session must NOT be set after fail-closed rotation"
    )

    # (a) Cache entry invalidated.
    assert cache.get(record.session_id) is None, (
        "[3.10] cache entry must be invalidated after exhausted rotation persist"
    )

    # (b) Both cookies cleared.
    session_clears = _find_set_cookies(response.headers, SESSION_COOKIE_NAME)
    csrf_clears = _find_set_cookies(response.headers, CSRF_COOKIE_NAME)
    assert len(session_clears) == 1 and len(csrf_clears) == 1, (
        f"[3.10] both BFF cookies must be cleared; got "
        f"session={len(session_clears)}, csrf={len(csrf_clears)}"
    )
    _assert_clear_cookie_attrs(session_clears[0])
    _assert_clear_cookie_attrs(csrf_clears[0])

    # Sanity: update_tokens was retried 3 times on rotation. Use the
    # token_persist sub-counter so we measure persist attempts only,
    # not the (also-incrementing) lock_acquire write that precedes them.
    assert table.token_persist_calls == 3, (
        f"[3.10] rotation must retry update_tokens 3 times; got "
        f"{table.token_persist_calls}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Requirement 3.11 — Cookie decode uniformity (no new timing/shape oracle)
# ═══════════════════════════════════════════════════════════════════════════


@given(
    garbage=st.one_of(
        # Arbitrary non-empty ASCII cookie-safe strings — typical "bad seal"
        # wire shape. Excludes '' because an empty cookie value is treated
        # as "no cookie present" by the middleware (requirement 3.2), not
        # as a decode failure.
        st.text(alphabet=_COOKIE_SAFE_ALPHABET, min_size=1, max_size=64),
        # Hex-encoded random bytes — invalid base64url alphabet and length.
        st.binary(min_size=1, max_size=48).map(lambda b: b.hex()),
    ),
)
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_3_11_cookie_decode_failure_produces_uniform_response_shape(
    garbage: str,
) -> None:
    """(3.11) Cookie decode uniformity.

    Every `CookieDecodeError` branch — bad base64, bad tag, truncated blob,
    wrong version, non-JSON body — produces the SAME externally observable
    response shape: identical status, identical Set-Cookie clearing pattern
    for both BFF cookies, identical handler body (has_session=False).

    The middleware must NOT surface any oracle that lets a caller
    distinguish decode failure modes.
    """
    table = InstrumentedTable()
    repo = _make_repo(table)
    codec = _make_codec()
    refresh_client = MagicMock()
    app = _build_app(
        config=_enabled_config(),
        repository=repo,
        codec=codec,
        refresh_client=refresh_client,
    )

    with TestClient(app) as client:
        response = client.get(
            "/echo", cookies={SESSION_COOKIE_NAME: garbage}
        )

    assert response.status_code == 200, (
        f"[3.11] bad-seal path must return 200 with cleared cookie; "
        f"got {response.status_code}"
    )
    assert response.json() == {
        "has_session": False,
        "session_id": None,
        "access_token": None,
        "csrf_token": None,
    }, (
        f"[3.11] handler body diverges for garbage cookie {garbage!r}: "
        f"{response.json()}"
    )

    # Both cookies cleared with the same attribute set.
    session_clears = _find_set_cookies(response.headers, SESSION_COOKIE_NAME)
    csrf_clears = _find_set_cookies(response.headers, CSRF_COOKIE_NAME)
    assert len(session_clears) == 1, (
        f"[3.11] expected one session-cookie clear; got {len(session_clears)}"
    )
    assert len(csrf_clears) == 1, (
        f"[3.11] expected one csrf-cookie clear; got {len(csrf_clears)}"
    )
    _assert_clear_cookie_attrs(session_clears[0])
    _assert_clear_cookie_attrs(csrf_clears[0])

    # Zero AWS calls — decode failure is caught before any DDB / Cognito I/O.
    assert table.get_item_calls == 0, (
        f"[3.11] bad-seal path must NOT reach DDB; observed "
        f"{table.get_item_calls} get_item calls."
    )
    refresh_client.refresh.assert_not_called()
