"""Tests for `get_current_user_or_session` (Phase 6 dual-auth dep).

The dependency resolves the current user from either a BFF session
cookie OR a Bearer token. It exists only for the Phase 6 → 7 transition
window: routes the SPA hits during normal use are migrated to this dep
so that a SPA still on the Bearer rollback bundle and a SPA on the new
cookie bundle both work against the same backend.

Resolution order: cookie first, Bearer second. The two underlying paths
delegate to `get_current_user_from_session` and `get_current_user`
respectively, both of which have their own coverage — these tests pin
down only the COMPOSITION (which path gets picked, what 401 looks like
when neither is available, that the cookie path wins when both are
present).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from apis.shared.auth.dependencies import get_current_user_or_session
from apis.shared.auth.models import User
from apis.shared.sessions_bff.models import SessionRecord


def _record() -> SessionRecord:
    now = int(time.time())
    return SessionRecord(
        session_id="sess-001",
        user_id="cookie-user",
        username="alice",
        cognito_access_token="cookie.access.token",
        cognito_refresh_token="cookie.refresh.token",
        id_token="cookie.id.token",
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
    async def me(user: User = Depends(get_current_user_or_session)):
        return {"user_id": user.user_id, "via": "cookie" if user.user_id == "cookie-user" else "bearer"}

    return app


def _noop_enrich(_user):
    return None


def test_uses_cookie_when_session_attached() -> None:
    """Cookie path: bff_session is on request.state, no Bearer needed."""
    fake_validator = MagicMock()
    fake_validator.validate_token.return_value = User(
        email="alice@example.com",
        user_id="cookie-user",
        name="Alice",
        roles=["user"],
    )
    with patch(
        "apis.shared.auth.dependencies._get_bff_cognito_validator",
        return_value=fake_validator,
    ), patch(
        "apis.shared.auth.dependencies._enrich_user_from_store",
        side_effect=_noop_enrich,
    ), patch(
        "apis.shared.auth.dependencies._get_user_sync_service",
        return_value=None,
    ):
        app = _build_app(record=_record())
        response = TestClient(app).get("/me")
        assert response.status_code == 200
        assert response.json() == {"user_id": "cookie-user", "via": "cookie"}
        fake_validator.validate_token.assert_called_once_with("cookie.access.token")


def test_falls_back_to_bearer_when_no_session() -> None:
    """No session attached → Bearer path runs against the SPA validator."""
    fake_validator = MagicMock()
    fake_validator.validate_token.return_value = User(
        email="bob@example.com",
        user_id="bearer-user",
        name="Bob",
        roles=["user"],
    )
    with patch(
        "apis.shared.auth.dependencies._get_cognito_validator",
        return_value=fake_validator,
    ), patch(
        "apis.shared.auth.dependencies._enrich_user_from_store",
        side_effect=_noop_enrich,
    ), patch(
        "apis.shared.auth.dependencies._get_user_sync_service",
        return_value=None,
    ):
        app = _build_app(record=None)
        response = TestClient(app).get(
            "/me", headers={"Authorization": "Bearer the-bearer-token"}
        )
        assert response.status_code == 200
        assert response.json() == {"user_id": "bearer-user", "via": "bearer"}
        fake_validator.validate_token.assert_called_once_with("the-bearer-token")


def test_cookie_takes_precedence_when_both_present() -> None:
    """Both Bearer and cookie present → cookie wins. This is the SPA's
    rollback bundle scenario: the user has a stale localStorage Bearer
    token AND a fresh BFF session cookie. The cookie is the post-cutover
    truth source, so it should authoritatively decide."""
    cookie_validator = MagicMock()
    cookie_validator.validate_token.return_value = User(
        email="alice@example.com",
        user_id="cookie-user",
        name="Alice",
        roles=["user"],
    )
    bearer_validator = MagicMock()  # Should never be called

    with patch(
        "apis.shared.auth.dependencies._get_bff_cognito_validator",
        return_value=cookie_validator,
    ), patch(
        "apis.shared.auth.dependencies._get_cognito_validator",
        return_value=bearer_validator,
    ), patch(
        "apis.shared.auth.dependencies._enrich_user_from_store",
        side_effect=_noop_enrich,
    ), patch(
        "apis.shared.auth.dependencies._get_user_sync_service",
        return_value=None,
    ):
        app = _build_app(record=_record())
        response = TestClient(app).get(
            "/me", headers={"Authorization": "Bearer stale-bearer-token"}
        )
        assert response.status_code == 200
        assert response.json() == {"user_id": "cookie-user", "via": "cookie"}
        cookie_validator.validate_token.assert_called_once_with("cookie.access.token")
        bearer_validator.validate_token.assert_not_called()


def test_returns_401_when_neither_cookie_nor_bearer_present() -> None:
    """Anonymous request: no session on state, no Authorization header."""
    app = _build_app(record=None)
    response = TestClient(app).get("/me")
    assert response.status_code == 401
    assert response.json()["detail"].startswith("Authentication required")
    # Carries the WWW-Authenticate header so the SPA bootstrap path can
    # distinguish "session expired" from "service down".
    assert response.headers.get("www-authenticate") == "Bearer"
