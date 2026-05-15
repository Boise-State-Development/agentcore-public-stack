"""Tests for `get_current_user_from_session`.

The dependency reads `request.state.bff_session` (populated by
SessionRefreshMiddleware), revalidates the stored Cognito access token via
the existing CognitoJWTValidator, and returns a `User`. We mock the
validator so we don't need real JWKS — that's covered by existing
CognitoJWTValidator tests.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.sessions_bff.models import SessionRecord


def _record() -> SessionRecord:
    now = int(time.time())
    return SessionRecord(
        session_id="sess-001",
        user_id="user-sub",
        username="alice",
        cognito_access_token="access.token",
        cognito_refresh_token="refresh.token",
        id_token="id.token",
        access_token_exp=now + 3600,
        csrf_secret="csrf-secret",
        created_at=now,
        last_seen_at=now,
        ttl=now + 28800,
    )


class _AttachSession(BaseHTTPMiddleware):
    def __init__(self, app, record: SessionRecord | None) -> None:
        super().__init__(app)
        self._record = record

    async def dispatch(self, request, call_next):
        if self._record is not None:
            request.state.bff_session = self._record
        return await call_next(request)


def _build_app(*, record: SessionRecord | None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(_AttachSession, record=record)

    @app.get("/me")
    async def me(user: User = Depends(get_current_user_from_session)):
        return {"user_id": user.user_id, "email": user.email}

    return app


def test_returns_user_when_session_attached() -> None:
    fake_validator = MagicMock()
    fake_validator.validate_token.return_value = User(
        email="alice@example.com",
        user_id="user-sub",
        name="Alice",
        roles=["user"],
    )
    with patch(
        "apis.shared.auth.dependencies._get_bff_cognito_validator",
        return_value=fake_validator,
    ), patch(
        "apis.shared.auth.dependencies._enrich_user_from_store"
    ) as enrich, patch(
        "apis.shared.auth.dependencies._get_user_sync_service",
        return_value=None,
    ):
        # Make _enrich_user_from_store an awaitable no-op
        async def _noop(_user):
            return None

        enrich.side_effect = _noop

        app = _build_app(record=_record())
        response = TestClient(app).get("/me")
        assert response.status_code == 200
        assert response.json() == {
            "user_id": "user-sub",
            "email": "alice@example.com",
        }
        fake_validator.validate_token.assert_called_once_with("access.token")


def test_returns_401_when_no_session_on_state() -> None:
    """No upstream middleware ran (or the cookie was missing) → 401."""
    app = _build_app(record=None)
    response = TestClient(app).get("/me")
    assert response.status_code == 401


def test_returns_401_when_token_validation_fails() -> None:
    from fastapi import HTTPException, status

    fake_validator = MagicMock()
    fake_validator.validate_token.side_effect = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="bad sig"
    )
    with patch(
        "apis.shared.auth.dependencies._get_bff_cognito_validator",
        return_value=fake_validator,
    ):
        app = _build_app(record=_record())
        response = TestClient(app).get("/me")
        assert response.status_code == 401


def test_returns_500_when_validator_unconfigured() -> None:
    with patch(
        "apis.shared.auth.dependencies._get_bff_cognito_validator",
        return_value=None,
    ):
        app = _build_app(record=_record())
        response = TestClient(app).get("/me")
        assert response.status_code == 500
