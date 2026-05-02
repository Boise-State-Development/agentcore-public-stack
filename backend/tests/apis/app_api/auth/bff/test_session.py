"""Tests for `GET /auth/session`.

The session route consumes `get_current_user_from_session`, which depends
on `request.state.bff_session` being populated upstream by
`SessionRefreshMiddleware`. Rather than spinning up the full middleware
stack, we shim a tiny `BaseHTTPMiddleware` that injects a session record
on every request — same pattern as `test_dependency_session_user.py` in
the Phase 2 suite.
"""

from __future__ import annotations

import time
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from apis.app_api.auth.bff.routes import router as bff_router
from apis.shared.auth.models import User
from apis.shared.sessions_bff.csrf import CSRFHelper
from apis.shared.sessions_bff.models import SessionRecord


def _record() -> SessionRecord:
    now = int(time.time())
    return SessionRecord(
        session_id="sess-abcd",
        user_id="user-sub",
        username="alice",
        cognito_access_token="access.tok",
        cognito_refresh_token="refresh.tok",
        id_token="id.tok",
        access_token_exp=now + 3600,
        csrf_secret="csrf-secret-xyz",
        created_at=now,
        last_seen_at=now,
        ttl=now + 28800,
    )


class _AttachSession(BaseHTTPMiddleware):
    def __init__(self, app, *, record: Optional[SessionRecord]) -> None:
        super().__init__(app)
        self._record = record

    async def dispatch(self, request, call_next):
        if self._record is not None:
            request.state.bff_session = self._record
            request.state.bff_csrf_token = CSRFHelper.derive_token(
                self._record.csrf_secret, self._record.session_id
            )
        return await call_next(request)


def _build_app(*, record: Optional[SessionRecord]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(_AttachSession, record=record)
    app.include_router(bff_router)
    return app


def test_session_returns_user_and_csrf_when_authenticated(bff_env):
    record = _record()
    fake_user = User(
        email="alice@example.com",
        user_id="user-sub",
        name="Alice",
        roles=["user", "admin"],
        picture="https://example.com/p.png",
    )
    validator = MagicMock()
    validator.validate_token.return_value = fake_user

    with patch(
        "apis.shared.auth.dependencies._get_bff_cognito_validator",
        return_value=validator,
    ), patch(
        "apis.shared.auth.dependencies._enrich_user_from_store"
    ) as enrich, patch(
        "apis.shared.auth.dependencies._get_user_sync_service",
        return_value=None,
    ):
        async def _noop(_user):
            return None

        enrich.side_effect = _noop

        app = _build_app(record=record)
        response = TestClient(app).get("/auth/session")

    assert response.status_code == 200
    body = response.json()
    expected_csrf = CSRFHelper.derive_token(record.csrf_secret, record.session_id)
    assert body == {
        "user_id": "user-sub",
        "email": "alice@example.com",
        "name": "Alice",
        "roles": ["user", "admin"],
        "picture": "https://example.com/p.png",
        "csrf_token": expected_csrf,
    }


def test_session_401_without_session(bff_env):
    """No upstream middleware ran → dep raises 401."""
    app = _build_app(record=None)
    response = TestClient(app).get("/auth/session")
    assert response.status_code == 401


def test_session_falls_back_to_re_deriving_csrf(bff_env):
    """If `request.state.bff_csrf_token` is somehow missing but the
    session record is present, the route still returns a usable CSRF
    token — defense in depth against a future middleware bug."""
    record = _record()

    class _SessionOnly(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.bff_session = record
            return await call_next(request)

    fake_user = User(email="a@a", user_id="u", name="A", roles=[])
    validator = MagicMock()
    validator.validate_token.return_value = fake_user

    with patch(
        "apis.shared.auth.dependencies._get_bff_cognito_validator",
        return_value=validator,
    ), patch(
        "apis.shared.auth.dependencies._enrich_user_from_store"
    ) as enrich, patch(
        "apis.shared.auth.dependencies._get_user_sync_service",
        return_value=None,
    ):
        async def _noop(_u):
            return None

        enrich.side_effect = _noop

        app = FastAPI()
        app.add_middleware(_SessionOnly)
        app.include_router(bff_router)

        body = TestClient(app).get("/auth/session").json()

    assert body["csrf_token"] == CSRFHelper.derive_token(
        record.csrf_secret, record.session_id
    )
