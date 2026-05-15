"""Tests for CSRFMiddleware.

The middleware only enforces when an upstream layer has already populated
`request.state.bff_session`. We simulate that with a tiny test middleware
that injects a known SessionRecord — keeps these tests focused on CSRF
behavior rather than re-exercising session refresh.
"""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from apis.shared.middleware.csrf import CSRFMiddleware
from apis.shared.sessions_bff.config import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
)
from apis.shared.sessions_bff.csrf import CSRFHelper
from apis.shared.sessions_bff.models import SessionRecord


def _record() -> SessionRecord:
    now = int(time.time())
    return SessionRecord(
        session_id="sess-001",
        user_id="user-sub",
        username="alice",
        cognito_access_token="at",
        cognito_refresh_token="rt",
        id_token="id",
        access_token_exp=now + 3600,
        csrf_secret="csrf-secret",
        created_at=now,
        last_seen_at=now,
        ttl=now + 28800,
    )


class _AttachSession(BaseHTTPMiddleware):
    """Test helper — pretends SessionRefreshMiddleware ran successfully."""

    def __init__(self, app, record: SessionRecord | None) -> None:
        super().__init__(app)
        self._record = record

    async def dispatch(self, request, call_next):
        if self._record is not None:
            request.state.bff_session = self._record
        return await call_next(request)


def _build_app(*, attach_record: SessionRecord | None) -> FastAPI:
    app = FastAPI()
    # Outer (added last) wraps inner — request order: AttachSession → CSRF → route.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(_AttachSession, record=attach_record)

    @app.get("/safe")
    def safe() -> dict:
        return {"ok": True}

    @app.post("/submit")
    def submit() -> dict:
        return {"ok": True}

    return app


def test_safe_method_bypasses_csrf() -> None:
    record = _record()
    app = _build_app(attach_record=record)
    response = TestClient(app).get("/safe")
    assert response.status_code == 200


def test_post_without_session_bypasses_csrf() -> None:
    """Bearer-token requests have no `bff_session` on state — must pass through."""
    app = _build_app(attach_record=None)
    response = TestClient(app).post("/submit")
    assert response.status_code == 200


def test_post_with_matching_token_succeeds() -> None:
    record = _record()
    app = _build_app(attach_record=record)
    token = CSRFHelper.derive_token(record.csrf_secret, record.session_id)

    response = TestClient(app).post(
        "/submit",
        headers={CSRF_HEADER_NAME: token},
        cookies={CSRF_COOKIE_NAME: token},
    )
    assert response.status_code == 200


def test_post_without_csrf_token_returns_403() -> None:
    record = _record()
    app = _build_app(attach_record=record)
    response = TestClient(app).post("/submit")
    assert response.status_code == 403


def test_post_with_only_header_returns_403() -> None:
    record = _record()
    app = _build_app(attach_record=record)
    token = CSRFHelper.derive_token(record.csrf_secret, record.session_id)
    response = TestClient(app).post("/submit", headers={CSRF_HEADER_NAME: token})
    assert response.status_code == 403


def test_post_with_mismatched_header_and_cookie_returns_403() -> None:
    record = _record()
    app = _build_app(attach_record=record)
    token = CSRFHelper.derive_token(record.csrf_secret, record.session_id)
    response = TestClient(app).post(
        "/submit",
        headers={CSRF_HEADER_NAME: token},
        cookies={CSRF_COOKIE_NAME: "different"},
    )
    assert response.status_code == 403


def test_post_with_forged_token_pair_returns_403() -> None:
    """Equal but incorrect (attacker-supplied) token pair must not validate."""
    record = _record()
    app = _build_app(attach_record=record)
    forged = "0" * 32
    response = TestClient(app).post(
        "/submit",
        headers={CSRF_HEADER_NAME: forged},
        cookies={CSRF_COOKIE_NAME: forged},
    )
    assert response.status_code == 403


def test_login_path_is_exempt() -> None:
    """Login/callback are exempt because they bootstrap the session."""
    record = _record()
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(_AttachSession, record=record)

    @app.post("/auth/login")
    def login() -> dict:
        return {"ok": True}

    response = TestClient(app).post("/auth/login")
    assert response.status_code == 200
