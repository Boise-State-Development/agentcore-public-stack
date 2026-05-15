"""Tests for the app-api voice routes — ticket POST and WS auth gating.

Full end-to-end WebSocket relay tests would need an upstream WS mock and
are out of scope here; the unit-level codec/replay/service tests cover the
ticket primitive separately. These tests focus on the route's auth gates:
origin allowlist, missing/invalid ticket, replay rejection, and ticket↔
session user-id binding.
"""

from __future__ import annotations

from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apis.app_api.voice.routes as voice_routes
from apis.app_api.voice.routes import router as voice_router
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord
from apis.shared.voice_ticket.codec import VoiceTicketCodec
from apis.shared.voice_ticket.replay import VoiceTicketReplayStore
from apis.shared.voice_ticket.service import VoiceTicketService


SIGNING_KEY = b"k" * 64
USER_ID = "user-001"
SESSION_ID = "sess-AAA"
COGNITO_TOKEN = "cognito-access-token"
ORIGIN = "http://localhost:4200"


@pytest.fixture(autouse=True)
def cors_origins(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", ORIGIN)


@pytest.fixture
def voice_service() -> VoiceTicketService:
    return VoiceTicketService(
        codec=VoiceTicketCodec(SIGNING_KEY),
        replay_store=VoiceTicketReplayStore(table_name=""),
        ttl_seconds=60,
    )


@pytest.fixture
def session_record() -> SessionRecord:
    return SessionRecord(
        session_id="bff-sess-1",
        user_id=USER_ID,
        username="alice",
        cognito_access_token=COGNITO_TOKEN,
        cognito_refresh_token="r",
        id_token=None,
        access_token_exp=2_000_000_000,
        csrf_secret="csrf-secret",
        created_at=0,
        last_seen_at=0,
        ttl=2_000_000_000,
    )


class _FakeRepository:
    def __init__(self, record: Optional[SessionRecord]) -> None:
        self._record = record
        self.enabled = True

    async def get(self, session_id: str) -> Optional[SessionRecord]:
        if self._record and self._record.session_id == session_id:
            return self._record
        return None


class _FakeCodec:
    """Stand-in for CookieCodec — unseal returns a fixed session_id without KMS."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def unseal(self, _value: str) -> CookiePayload:
        return CookiePayload(session_id=self._session_id)


@pytest.fixture
def app(voice_service, session_record, monkeypatch) -> FastAPI:
    voice_routes._reset_for_tests()
    monkeypatch.setattr(voice_routes, "get_default_service", lambda: voice_service)
    monkeypatch.setattr(
        voice_routes, "get_default_codec", lambda: _FakeCodec(session_record.session_id)
    )
    monkeypatch.setattr(
        voice_routes, "_get_session_repository", lambda: _FakeRepository(session_record)
    )

    fastapi_app = FastAPI()
    fastapi_app.include_router(voice_router)

    async def _stub_user() -> User:
        return User(
            user_id=USER_ID,
            email="alice@example.com",
            name="Alice",
            roles=[],
            raw_token="t",
        )

    fastapi_app.dependency_overrides[get_current_user_from_session] = _stub_user

    # Stub out the upstream relay so the WS handler returns immediately
    # after the auth checks pass — we're verifying gates, not the proxy.
    async def _noop_relay(*, client_ws, cognito_access_token, user_id):
        await client_ws.send_json({"type": "noop"})

    monkeypatch.setattr(voice_routes, "relay_voice_stream", _noop_relay)
    return fastapi_app


# --- POST /voice/ticket -----------------------------------------------


def test_post_ticket_returns_signed_ticket(app: FastAPI, voice_service) -> None:
    client = TestClient(app)
    res = client.post("/voice/ticket", json={"session_id": "sess-A"})
    assert res.status_code == 200
    body = res.json()
    assert "ticket" in body and body["ticket"]
    assert body["expires_in"] == 60

    # Round-trip: the ticket must verify against the same signing key.
    claims = voice_service._codec.verify(body["ticket"])
    assert claims.user_id == USER_ID
    assert claims.session_id == "sess-A"


def test_post_ticket_validates_session_id_present(app: FastAPI) -> None:
    client = TestClient(app)
    res = client.post("/voice/ticket", json={})
    assert res.status_code == 422


# --- WebSocket /voice/stream ------------------------------------------


def test_ws_rejects_missing_ticket(app: FastAPI) -> None:
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect("/voice/stream", headers={"origin": ORIGIN}):
            pass


def test_ws_rejects_disallowed_origin(app: FastAPI, voice_service) -> None:
    ticket, _ = voice_service.issue(user_id=USER_ID, session_id="sess-A")
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/voice/stream?ticket={ticket}", headers={"origin": "https://evil.com"}
        ):
            pass


def test_ws_rejects_replayed_ticket(app: FastAPI, voice_service, session_record) -> None:
    ticket, _ = voice_service.issue(user_id=USER_ID, session_id="sess-A")
    cookie = {"__Host-bff_session": "sealed"}
    client = TestClient(app, cookies=cookie)

    # First use should pass the gates and reach the noop relay.
    with client.websocket_connect(
        f"/voice/stream?ticket={ticket}", headers={"origin": ORIGIN}
    ) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "noop"

    # Second use of the same ticket must be rejected before accept.
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/voice/stream?ticket={ticket}", headers={"origin": ORIGIN}
        ):
            pass


def test_ws_rejects_ticket_for_different_user(
    app: FastAPI, voice_service, session_record
) -> None:
    # Ticket bound to a user_id that doesn't match the BFF session row.
    ticket, _ = voice_service.issue(user_id="other-user", session_id="sess-A")
    cookie = {"__Host-bff_session": "sealed"}
    client = TestClient(app, cookies=cookie)
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/voice/stream?ticket={ticket}", headers={"origin": ORIGIN}
        ):
            pass
