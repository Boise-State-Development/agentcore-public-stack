"""Tests for the `Authorization: BFF <sealed>` branch in SessionRefreshMiddleware.

This is the seam that lets the terminal client reach the session-authenticated
surface of app-api without holding a cookie (see
`docs/specs/CLI_DEVICE_AUTH_SPEC.md`). Two properties carry the design:

* the header resolves to exactly the same `SessionRecord` a cookie would, so
  no downstream consumer needs to know which arrived;
* **the header path never writes or clears cookies** — not on success, not on
  a slide, not on an unrecoverable value.

Reuses the helpers from `test_session_refresh_middleware` so both paths are
exercised against identically-shaped collaborators.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apis.shared.middleware.csrf import CSRFMiddleware
from apis.shared.middleware.session_refresh import (
    SessionRefreshMiddleware,
    sealed_session_from_header,
)
from apis.shared.sessions_bff import lock as lock_module
from apis.shared.sessions_bff.cache import SessionCache
from apis.shared.sessions_bff.config import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from apis.shared.sessions_bff.cookie import CookieCodec
from apis.shared.sessions_bff.models import CookiePayload
from apis.shared.sessions_bff.refresh import RefreshResult

from .test_session_refresh_middleware import (
    _enabled_config,
    _make_codec,
    _make_record,
)


@pytest.fixture(autouse=True)
def _reset_session_locks() -> None:
    lock_module._reset_for_tests()


def _build_app(
    *,
    config,
    repository,
    codec: CookieCodec,
    refresh_client,
    cache=None,
) -> FastAPI:
    """Same shape as the cookie tests' app, plus the CSRF token on the echo."""
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
            "user_id": record.user_id if record else None,
            "access_token": record.cognito_access_token if record else None,
            "csrf_token": getattr(request.state, "bff_csrf_token", None),
        }

    return app


def _bff(sealed: str) -> dict:
    return {"Authorization": f"BFF {sealed}"}


# =====================================================================
# Header parsing
# =====================================================================


class TestHeaderParsing:
    def _request(self, value: str | None) -> Request:
        headers = []
        if value is not None:
            headers.append((b"authorization", value.encode()))
        return Request({"type": "http", "headers": headers})

    def test_extracts_the_sealed_value(self) -> None:
        assert sealed_session_from_header(self._request("BFF abc.def")) == "abc.def"

    def test_scheme_is_case_insensitive(self) -> None:
        for scheme in ("BFF", "bff", "Bff", "bFf"):
            assert sealed_session_from_header(self._request(f"{scheme} abc")) == "abc"

    def test_bearer_is_ignored(self) -> None:
        """Bearer requests must keep falling through untouched."""
        assert sealed_session_from_header(self._request("Bearer ey.jwt")) is None

    def test_other_schemes_are_ignored(self) -> None:
        for raw in ("Basic dXNlcg==", "Negotiate abc", "BFFX abc", "abc"):
            assert sealed_session_from_header(self._request(raw)) is None

    def test_missing_header_is_none(self) -> None:
        assert sealed_session_from_header(self._request(None)) is None

    def test_empty_value_is_none(self) -> None:
        assert sealed_session_from_header(self._request("BFF")) is None
        assert sealed_session_from_header(self._request("BFF   ")) is None

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert sealed_session_from_header(self._request("BFF  abc  ")) == "abc"


# =====================================================================
# Resolution
# =====================================================================


class TestHeaderResolution:
    def test_valid_header_attaches_the_session(self) -> None:
        record = _make_record()
        repo = AsyncMock()
        repo.get.return_value = record
        codec = _make_codec()
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=MagicMock(),
        )

        sealed = codec.seal(CookiePayload(session_id=record.session_id))
        response = TestClient(app).get("/echo", headers=_bff(sealed))

        assert response.status_code == 200
        body = response.json()
        assert body["has_session"] is True
        assert body["session_id"] == record.session_id
        assert body["user_id"] == "user-sub-001"

    def test_csrf_token_is_still_populated(self) -> None:
        """Downstream code (e.g. `GET /auth/session`) reads it unconditionally.

        CSRF is not *enforced* on this path — `CSRFMiddleware` only fires when
        a session cookie is present — but leaving the attribute unset would
        make the header path a special case for every consumer.
        """
        record = _make_record()
        repo = AsyncMock()
        repo.get.return_value = record
        codec = _make_codec()
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=MagicMock(),
        )

        sealed = codec.seal(CookiePayload(session_id=record.session_id))
        body = TestClient(app).get("/echo", headers=_bff(sealed)).json()
        assert body["csrf_token"]

    def test_bad_seal_yields_no_session(self) -> None:
        repo = AsyncMock()
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=_make_codec(),
            refresh_client=MagicMock(),
        )

        response = TestClient(app).get("/echo", headers=_bff("not-a-sealed-value"))

        assert response.status_code == 200
        assert response.json()["has_session"] is False
        repo.get.assert_not_called()

    def test_missing_session_row_yields_no_session(self) -> None:
        repo = AsyncMock()
        repo.get.return_value = None
        codec = _make_codec()
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=MagicMock(),
        )

        sealed = codec.seal(CookiePayload(session_id="sess-gone"))
        response = TestClient(app).get("/echo", headers=_bff(sealed))

        assert response.json()["has_session"] is False

    def test_bearer_header_does_not_touch_the_repository(self) -> None:
        repo = AsyncMock()
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=_make_codec(),
            refresh_client=MagicMock(),
        )

        response = TestClient(app).get("/echo", headers={"Authorization": "Bearer ey.some.jwt"})

        assert response.json()["has_session"] is False
        repo.get.assert_not_called()

    def test_header_triggers_refresh_like_a_cookie(self) -> None:
        """The refresh machinery is shared; the header must not bypass it."""
        now = int(time.time())
        record = _make_record(access_token_exp=now + 10)  # inside the leeway
        repo = AsyncMock()
        repo.get.return_value = record
        repo.try_acquire_refresh_lock.return_value = True
        codec = _make_codec()
        refresh = MagicMock()
        refresh.refresh = AsyncMock(
            return_value=RefreshResult(
                access_token="access.refreshed",
                refresh_token="refresh.original",
                id_token="id.refreshed",
                access_token_exp=now + 3600,
            )
        )
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=refresh,
        )

        sealed = codec.seal(CookiePayload(session_id=record.session_id))
        body = TestClient(app).get("/echo", headers=_bff(sealed)).json()

        assert body["access_token"] == "access.refreshed"
        refresh.refresh.assert_awaited_once()


# =====================================================================
# The no-cookie invariant
# =====================================================================


class TestHeaderPathNeverTouchesCookies:
    def test_success_sets_no_cookies(self) -> None:
        record = _make_record()
        repo = AsyncMock()
        repo.get.return_value = record
        codec = _make_codec()
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=MagicMock(),
        )

        sealed = codec.seal(CookiePayload(session_id=record.session_id))
        response = TestClient(app).get("/echo", headers=_bff(sealed))

        assert not response.headers.get_list("set-cookie")

    def test_bad_seal_does_not_emit_a_cookie_clear(self) -> None:
        """A cookie-path bad seal clears cookies. The header path must not —
        the caller has no cookie jar, and a request that merely carried a
        header would otherwise strip an unrelated browser session."""
        app = _build_app(
            config=_enabled_config(),
            repository=AsyncMock(),
            codec=_make_codec(),
            refresh_client=MagicMock(),
        )

        response = TestClient(app).get("/echo", headers=_bff("garbage"))

        assert not response.headers.get_list("set-cookie")

    def test_missing_row_does_not_emit_a_cookie_clear(self) -> None:
        repo = AsyncMock()
        repo.get.return_value = None
        codec = _make_codec()
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=MagicMock(),
        )

        sealed = codec.seal(CookiePayload(session_id="sess-gone"))
        response = TestClient(app).get("/echo", headers=_bff(sealed))

        assert not response.headers.get_list("set-cookie")

    def test_slide_writes_ddb_but_emits_no_cookie(self) -> None:
        """An active CLI keeps its session alive — the DDB TTL still slides —
        but the Max-Age that a browser would receive is discarded."""
        record = _make_record()
        record.last_seen_at = int(time.time()) - 120  # past the 60s throttle
        repo = AsyncMock()
        repo.get.return_value = record
        codec = _make_codec()
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=MagicMock(),
        )

        sealed = codec.seal(CookiePayload(session_id=record.session_id))
        with TestClient(app) as client:
            response = client.get("/echo", headers=_bff(sealed))
            # The slide write is fire-and-forget; poll for it inside the
            # `with` so TestClient's portal teardown doesn't cancel the task.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and repo.touch_last_seen.await_count == 0:
                time.sleep(0.01)
            if repo.touch_last_seen.await_count == 0:
                client.get("/echo", headers=_bff(sealed))
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and repo.touch_last_seen.await_count == 0:
                    time.sleep(0.01)

        assert response.status_code == 200
        repo.touch_last_seen.assert_awaited()
        assert not response.headers.get_list("set-cookie")


# =====================================================================
# Precedence and cookie-path regression
# =====================================================================


class TestPrecedence:
    def test_cookie_wins_when_both_are_present(self) -> None:
        """Browser behaviour must be bit-identical to before this branch.

        With a cookie present the header is never read, so no existing request
        can change path — and the response still carries cookie handling.
        """
        cookie_record = _make_record(session_id="sess-cookie")
        header_record = _make_record(session_id="sess-header")
        repo = AsyncMock()
        repo.get.side_effect = lambda sid: {
            "sess-cookie": cookie_record,
            "sess-header": header_record,
        }.get(sid)
        codec = _make_codec()
        app = _build_app(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=MagicMock(),
        )

        cookie_sealed = codec.seal(CookiePayload(session_id="sess-cookie"))
        header_sealed = codec.seal(CookiePayload(session_id="sess-header"))
        response = TestClient(app).get(
            "/echo",
            cookies={SESSION_COOKIE_NAME: cookie_sealed},
            headers=_bff(header_sealed),
        )

        assert response.json()["session_id"] == "sess-cookie"

    def test_cookie_path_still_clears_on_bad_seal(self) -> None:
        """Regression guard: the new branch must not have altered this."""
        app = _build_app(
            config=_enabled_config(),
            repository=AsyncMock(),
            codec=_make_codec(),
            refresh_client=MagicMock(),
        )

        response = TestClient(app).get("/echo", cookies={SESSION_COOKIE_NAME: "garbage"})

        blob = " ".join(response.headers.get_list("set-cookie"))
        assert SESSION_COOKIE_NAME in blob
        assert CSRF_COOKIE_NAME in blob

    def test_header_is_ignored_when_bff_is_disabled(self) -> None:
        from apis.shared.sessions_bff.config import BFFConfig

        disabled = BFFConfig(
            sessions_table_name=None,
            cookie_signing_key_arn=None,
            session_ttl_seconds=28800,
            refresh_leeway_seconds=60,
            cognito_bff_app_client_id=None,
            cognito_bff_app_client_secret_arn=None,
            inference_api_url=None,
        )
        repo = AsyncMock()
        app = _build_app(
            config=disabled,
            repository=repo,
            codec=_make_codec(),
            refresh_client=MagicMock(),
        )

        response = TestClient(app).get("/echo", headers=_bff("anything"))

        assert response.json()["has_session"] is False
        repo.get.assert_not_called()


# =====================================================================
# CSRF interaction
# =====================================================================


def _build_app_with_csrf(*, config, repository, codec, refresh_client) -> FastAPI:
    """Both middlewares, in the order `main.py` installs them.

    Starlette runs `add_middleware` in reverse order of registration, so
    registering CSRF first and session-refresh second makes session-refresh
    the outer layer — which is required, because CSRF reads the state that
    session-refresh populates.
    """
    app = FastAPI()

    @app.post("/mutate")
    async def mutate(request: Request):
        record = getattr(request.state, "bff_session", None)
        return {"has_session": record is not None}

    app.add_middleware(CSRFMiddleware)
    app.add_middleware(
        SessionRefreshMiddleware,
        config=config,
        repository=repository,
        cookie_codec=codec,
        refresh_client=refresh_client,
        cache=SessionCache(ttl_seconds=60),
    )
    return app


class TestCsrfInteraction:
    """`CSRFMiddleware` gates on `request.state.bff_session`, not on cookie
    presence. Attaching a session from a header therefore drags every
    state-changing CLI request into CSRF enforcement unless it is explicitly
    exempted — which is what `bff_session_from_header` does.
    """

    def test_header_authenticated_post_is_not_blocked(self) -> None:
        record = _make_record()
        repo = AsyncMock()
        repo.get.return_value = record
        codec = _make_codec()
        app = _build_app_with_csrf(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=MagicMock(),
        )

        sealed = codec.seal(CookiePayload(session_id=record.session_id))
        response = TestClient(app).post("/mutate", headers=_bff(sealed))

        assert response.status_code == 200
        assert response.json()["has_session"] is True

    def test_cookie_authenticated_post_without_csrf_is_still_blocked(self) -> None:
        """The exemption must be scoped to the header path only."""
        record = _make_record()
        repo = AsyncMock()
        repo.get.return_value = record
        codec = _make_codec()
        app = _build_app_with_csrf(
            config=_enabled_config(),
            repository=repo,
            codec=codec,
            refresh_client=MagicMock(),
        )

        sealed = codec.seal(CookiePayload(session_id=record.session_id))
        response = TestClient(app).post("/mutate", cookies={SESSION_COOKIE_NAME: sealed})

        assert response.status_code == 403

    def test_flag_is_false_on_the_cookie_path(self) -> None:
        record = _make_record()
        repo = AsyncMock()
        repo.get.return_value = record
        codec = _make_codec()

        app = FastAPI()

        @app.get("/flag")
        async def flag(request: Request):
            return {"from_header": getattr(request.state, "bff_session_from_header", None)}

        app.add_middleware(
            SessionRefreshMiddleware,
            config=_enabled_config(),
            repository=repo,
            cookie_codec=codec,
            refresh_client=MagicMock(),
            cache=SessionCache(ttl_seconds=60),
        )

        sealed = codec.seal(CookiePayload(session_id=record.session_id))
        cookie_flag = TestClient(app).get("/flag", cookies={SESSION_COOKIE_NAME: sealed}).json()["from_header"]
        header_flag = TestClient(app).get("/flag", headers=_bff(sealed)).json()["from_header"]

        assert cookie_flag is False
        assert header_flag is True
