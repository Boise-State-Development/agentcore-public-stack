"""Tests for the dictation ticket + WebSocket routes.

The WS gates themselves are the voice routes' ``authenticate_ticketed_websocket``
(covered in ``tests/apis/app_api/voice/test_routes.py``); here the focus is
what dictation adds: the kill switch, a user-bound ticket, and that a ticket
opens only the socket it was minted for — in both directions.
"""

from __future__ import annotations

from typing import Optional

import pytest
from botocore.credentials import ReadOnlyCredentials
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apis.app_api.dictation.routes as dictation_routes
import apis.app_api.voice.routes as voice_routes
from apis.app_api.dictation.routes import router as dictation_router
from apis.app_api.voice.routes import router as voice_router
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord
from apis.shared.voice_ticket.codec import VoiceTicketCodec
from apis.shared.voice_ticket.replay import VoiceTicketReplayStore
from apis.shared.voice_ticket.service import VoiceTicketService

USER_ID = "user-001"
ORIGIN = "http://localhost:4200"
COOKIE = {"__Host-bff_session": "sealed"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", ORIGIN)
    monkeypatch.delenv("DICTATION_ENABLED", raising=False)
    monkeypatch.delenv("DICTATION_LANGUAGES", raising=False)
    monkeypatch.delenv("DICTATION_MAX_SECONDS", raising=False)


@pytest.fixture
def ticket_service() -> VoiceTicketService:
    return VoiceTicketService(
        codec=VoiceTicketCodec(b"k" * 64),
        replay_store=VoiceTicketReplayStore(table_name=""),
        ttl_seconds=60,
    )


class _FakeRepository:
    enabled = True

    def __init__(self, record: SessionRecord) -> None:
        self._record = record

    async def get(self, session_id: str) -> Optional[SessionRecord]:
        return self._record if session_id == self._record.session_id else None


class _FakeCodec:
    def unseal(self, _value: str) -> CookiePayload:
        return CookiePayload(session_id="bff-sess-1")


@pytest.fixture
def relay_calls(ticket_service, monkeypatch) -> list[dict]:
    record = SessionRecord(
        session_id="bff-sess-1",
        user_id=USER_ID,
        username="alice",
        cognito_access_token="t",
        cognito_refresh_token="r",
        id_token=None,
        access_token_exp=2_000_000_000,
        csrf_secret="c",
        created_at=0,
        last_seen_at=0,
        ttl=2_000_000_000,
    )
    voice_routes._reset_for_tests()
    monkeypatch.setattr(voice_routes, "get_default_service", lambda: ticket_service)
    monkeypatch.setattr(dictation_routes, "get_default_service", lambda: ticket_service)
    monkeypatch.setattr(voice_routes, "get_default_codec", lambda: _FakeCodec())
    monkeypatch.setattr(voice_routes, "_get_session_repository", lambda: _FakeRepository(record))
    monkeypatch.setattr(
        dictation_routes, "_credentials", lambda: ReadOnlyCredentials("AKID", "secret", None)
    )

    calls: list[dict] = []

    async def _fake_relay(*, client_ws, upstream_url, user_id, max_seconds):
        calls.append({"upstream_url": upstream_url, "user_id": user_id, "max_seconds": max_seconds})
        await client_ws.send_json({"type": "ready"})

    async def _noop_voice_relay(*, client_ws, cognito_access_token, user_id):
        await client_ws.send_json({"type": "noop"})

    monkeypatch.setattr(dictation_routes, "relay_dictation_stream", _fake_relay)
    monkeypatch.setattr(voice_routes, "relay_voice_stream", _noop_voice_relay)
    return calls


@pytest.fixture
def client(relay_calls) -> TestClient:
    app = FastAPI()
    app.include_router(dictation_router)
    app.include_router(voice_router)

    async def _stub_user() -> User:
        return User(user_id=USER_ID, email="a@example.com", name="Alice", roles=[], raw_token="t")

    app.dependency_overrides[get_current_user_from_session] = _stub_user
    return TestClient(app, cookies=COOKIE)


# --- POST /dictation/ticket --------------------------------------------


def test_ticket_is_user_bound_dictation_ticket(client: TestClient, ticket_service) -> None:
    res = client.post("/dictation/ticket")
    assert res.status_code == 200
    claims = ticket_service._codec.verify(res.json()["ticket"])
    assert claims.user_id == USER_ID
    assert claims.purpose == "dictation"
    assert claims.session_id == ""


def test_ticket_404s_when_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DICTATION_ENABLED", "false")
    assert client.post("/dictation/ticket").status_code == 404


# --- WebSocket /dictation/stream ---------------------------------------


def test_stream_presigns_and_relays(client: TestClient, ticket_service, relay_calls, monkeypatch) -> None:
    monkeypatch.setenv("DICTATION_LANGUAGES", "en-US, es-US")
    monkeypatch.setenv("DICTATION_MAX_SECONDS", "120")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    ticket, _ = ticket_service.issue(user_id=USER_ID, session_id="", purpose="dictation")
    with client.websocket_connect(
        f"/dictation/stream?ticket={ticket}", headers={"origin": ORIGIN}
    ) as ws:
        assert ws.receive_json() == {"type": "ready"}

    (call,) = relay_calls
    assert call["user_id"] == USER_ID
    assert call["max_seconds"] == 120.0
    assert call["upstream_url"].startswith(
        "wss://transcribestreaming.us-west-2.amazonaws.com:8443/stream-transcription-websocket?"
    )
    assert "identify-language=true" in call["upstream_url"]
    assert "language-options=en-US%2Ces-US" in call["upstream_url"]


def test_stream_rejects_voice_ticket(client: TestClient, ticket_service, relay_calls) -> None:
    ticket, _ = ticket_service.issue(user_id=USER_ID, session_id="sess-A")
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/dictation/stream?ticket={ticket}", headers={"origin": ORIGIN}
        ):
            pass
    assert relay_calls == []


def test_voice_stream_rejects_dictation_ticket(client: TestClient, ticket_service) -> None:
    ticket, _ = ticket_service.issue(user_id=USER_ID, session_id="", purpose="dictation")
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/voice/stream?ticket={ticket}", headers={"origin": ORIGIN}
        ):
            pass


def test_stream_rejects_disallowed_origin(client: TestClient, ticket_service, relay_calls) -> None:
    ticket, _ = ticket_service.issue(user_id=USER_ID, session_id="", purpose="dictation")
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/dictation/stream?ticket={ticket}", headers={"origin": "https://evil.example"}
        ):
            pass
    assert relay_calls == []


def test_stream_closed_when_disabled(
    client: TestClient, ticket_service, relay_calls, monkeypatch
) -> None:
    monkeypatch.setenv("DICTATION_ENABLED", "false")
    ticket, _ = ticket_service.issue(user_id=USER_ID, session_id="", purpose="dictation")
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/dictation/stream?ticket={ticket}", headers={"origin": ORIGIN}
        ):
            pass
    assert relay_calls == []


# --- config ------------------------------------------------------------


def test_languages_default_to_english(monkeypatch) -> None:
    assert dictation_routes.dictation_languages() == ["en-US"]
    monkeypatch.setenv("DICTATION_LANGUAGES", " , ")
    assert dictation_routes.dictation_languages() == ["en-US"]


def test_max_seconds_falls_back_on_bad_values(monkeypatch) -> None:
    assert dictation_routes.dictation_max_seconds() == 300.0
    monkeypatch.setenv("DICTATION_MAX_SECONDS", "abc")
    assert dictation_routes.dictation_max_seconds() == 300.0
    monkeypatch.setenv("DICTATION_MAX_SECONDS", "-5")
    assert dictation_routes.dictation_max_seconds() == 300.0
