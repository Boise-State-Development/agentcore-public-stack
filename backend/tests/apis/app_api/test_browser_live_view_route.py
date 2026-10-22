"""`POST /sessions/{id}/browser/live-view` — mint a Live View URL (spec D2).

The route exists because a Live View URL is SigV4 *query*-signed and lives at
most 300 seconds: it cannot be minted once and reused, so the client sends only
a conversation id and app-api signs a fresh one per call.

What these hold down:

* **The client never names the browser session.** It sends a conversation id;
  the browser session is resolved server-side from the metadata row. A route
  that accepted a browser session id would let any signed-in user stream any
  browser in the account.
* **Ownership is the metadata read**, which is user-scoped, so another user's
  conversation is indistinguishable from a missing one.
* **404 and 409 mean different things** — "no viewer here" vs "the session
  ended" — and the SPA says different things for each.
* **The signed URL never reaches a log line.**

Auth is the cookie-aware `get_current_user_from_session`, per the SPA-facing
route rule in CLAUDE.md.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.sessions import routes as session_routes
from apis.app_api.sessions.services import browser_live_view
from apis.app_api.sessions.services.browser_live_view import LiveViewUnavailable
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User

SIGNED_URL = (
    "https://bedrock-agentcore.us-west-2.amazonaws.com/browser-streams/b-1/"
    "sessions/bs-1/live?X-Amz-Signature=deadbeefdeadbeef&X-Amz-Expires=300"
)

REF = {
    "browserSessionId": "bs-1",
    "browserId": "browser-abc",
    "viewport": {"width": 1280, "height": 800},
    "controlState": "user",
}


def _user() -> User:
    return User(
        user_id="user-1", email="u@example.com", name="U",
        roles=["default"], raw_token="tok",
    )


def _client(monkeypatch, *, metadata=..., mint=None) -> TestClient:
    monkeypatch.delenv("BROWSER_TAKEOVER_ENABLED", raising=False)
    if metadata is ...:
        metadata = SimpleNamespace(browser_session=dict(REF))
    monkeypatch.setattr(
        session_routes, "get_session_metadata", AsyncMock(return_value=metadata)
    )
    monkeypatch.setattr(
        browser_live_view,
        "mint_live_view",
        mint
        or AsyncMock(
            return_value={
                "url": SIGNED_URL,
                "expiresAt": "2026-09-18T12:05:00+00:00",
                "viewport": {"width": 1280, "height": 800},
                "controlState": "user",
            }
        ),
    )
    app = FastAPI()
    app.include_router(session_routes.router)
    app.dependency_overrides[get_current_user_from_session] = _user
    return TestClient(app)


class TestMinting:
    def test_returns_a_fresh_url_with_its_expiry_and_viewport(self, monkeypatch):
        client = _client(monkeypatch)

        resp = client.post("/sessions/s1/browser/live-view")

        assert resp.status_code == 200
        body = resp.json()
        assert body["url"] == SIGNED_URL
        assert body["expiresAt"] == "2026-09-18T12:05:00+00:00"
        # Carried, not re-declared in the viewer: DCV crops on a mismatch.
        assert body["viewport"] == {"width": 1280, "height": 800}
        assert body["controlState"] == "user"

    def test_the_client_sends_only_a_conversation_id(self, monkeypatch):
        mint = AsyncMock(
            return_value={
                "url": SIGNED_URL,
                "expiresAt": "t",
                "viewport": {"width": 1280, "height": 800},
                "controlState": "user",
            }
        )
        client = _client(monkeypatch, mint=mint)

        client.post("/sessions/s1/browser/live-view")

        # The browser session came from the metadata row, not the request. A
        # route that took it from the client would let any signed-in user
        # stream any browser session in the account.
        assert mint.await_args.args[0]["browserSessionId"] == "bs-1"

    def test_the_lookup_is_scoped_to_the_calling_user(self, monkeypatch):
        client = _client(monkeypatch)

        client.post("/sessions/s1/browser/live-view")

        lookup = session_routes.get_session_metadata.await_args
        assert lookup.args == ("s1", "user-1")


class TestRefusals:
    def test_unknown_or_other_users_conversation_is_404(self, monkeypatch):
        client = _client(monkeypatch, metadata=None)

        assert client.post("/sessions/s1/browser/live-view").status_code == 404

    def test_a_conversation_with_no_browser_session_is_404(self, monkeypatch):
        client = _client(monkeypatch, metadata=SimpleNamespace(browser_session=None))

        resp = client.post("/sessions/s1/browser/live-view")

        assert resp.status_code == 404
        assert "no browser session" in resp.json()["detail"]

    def test_an_ended_session_is_409_not_404(self, monkeypatch):
        # Distinct on purpose: the SPA says "the session ended", not "no
        # viewer here". Collapsing them would make a timed-out takeover look
        # like a missing feature.
        mint = AsyncMock(
            side_effect=LiveViewUnavailable("The browser session is gone.", code=409)
        )
        client = _client(monkeypatch, mint=mint)

        resp = client.post("/sessions/s1/browser/live-view")

        assert resp.status_code == 409
        assert "gone" in resp.json()["detail"]

    def test_404_while_the_kill_switch_is_off(self, monkeypatch):
        client = _client(monkeypatch)
        monkeypatch.setenv("BROWSER_TAKEOVER_ENABLED", "false")

        assert client.post("/sessions/s1/browser/live-view").status_code == 404

    def test_the_kill_switch_short_circuits_before_any_lookup(self, monkeypatch):
        client = _client(monkeypatch)
        monkeypatch.setenv("BROWSER_TAKEOVER_ENABLED", "false")
        session_routes.get_session_metadata.reset_mock()

        client.post("/sessions/s1/browser/live-view")

        session_routes.get_session_metadata.assert_not_awaited()

    def test_a_signing_failure_does_not_leak_its_message(self, monkeypatch):
        # The exception text from a partially-built signed URL can contain the
        # URL itself.
        mint = AsyncMock(side_effect=RuntimeError(f"failed building {SIGNED_URL}"))
        client = _client(monkeypatch, mint=mint)

        resp = client.post("/sessions/s1/browser/live-view")

        assert resp.status_code == 500
        assert "X-Amz-Signature" not in resp.text

    def test_it_is_a_post_so_the_signature_stays_out_of_history(self, monkeypatch):
        client = _client(monkeypatch)

        assert client.get("/sessions/s1/browser/live-view").status_code == 405


class TestLogging:
    def test_the_signed_url_never_reaches_the_logs(self, monkeypatch, caplog):
        client = _client(monkeypatch)

        with caplog.at_level(logging.DEBUG):
            client.post("/sessions/s1/browser/live-view")

        assert "X-Amz-Signature" not in caplog.text
        assert SIGNED_URL not in caplog.text

    def test_the_unavailable_log_describes_the_session_without_a_url(
        self, monkeypatch, caplog
    ):
        mint = AsyncMock(side_effect=LiveViewUnavailable("gone", code=409))
        client = _client(monkeypatch, mint=mint)

        with caplog.at_level(logging.INFO):
            client.post("/sessions/s1/browser/live-view")

        assert "bs-1" in caplog.text
        assert "X-Amz-Signature" not in caplog.text


class TestMintLiveView:
    """The service, against a fake `BrowserClient`."""

    @pytest.fixture
    def fake_client(self, monkeypatch):
        calls = SimpleNamespace(expires=None, identifier=None, session_id=None)

        class FakeBrowserClient:
            def __init__(self, **_):
                self.identifier = None
                self.session_id = None

            def generate_live_view_url(self, expires):
                calls.expires = expires
                calls.identifier = self.identifier
                calls.session_id = self.session_id
                return SIGNED_URL

        monkeypatch.setattr(
            "bedrock_agentcore.tools.browser_client.BrowserClient", FakeBrowserClient
        )
        return calls

    @pytest.mark.asyncio
    async def test_signs_against_the_referenced_session(self, fake_client):
        minted = await browser_live_view.mint_live_view(dict(REF))

        assert minted["url"] == SIGNED_URL
        assert fake_client.identifier == "browser-abc"
        assert fake_client.session_id == "bs-1"

    @pytest.mark.asyncio
    async def test_never_asks_for_more_than_the_services_cap(self, fake_client):
        # `generate_live_view_url` RAISES above 300 rather than clamping, so
        # an over-large env value must be clamped here or every mint fails.
        await browser_live_view.mint_live_view(dict(REF))

        assert fake_client.expires <= browser_live_view.MAX_EXPIRY_SECONDS

    @pytest.mark.asyncio
    async def test_a_missing_reference_is_404_not_a_crash(self, fake_client):
        with pytest.raises(LiveViewUnavailable) as exc:
            await browser_live_view.mint_live_view({"viewport": {"width": 1, "height": 1}})

        assert exc.value.code == 404

    @pytest.mark.asyncio
    async def test_a_signing_refusal_reads_as_409(self, monkeypatch):
        class Boom:
            def __init__(self, **_):
                self.identifier = None
                self.session_id = None

            def generate_live_view_url(self, expires):
                raise RuntimeError("ResourceNotFoundException")

        monkeypatch.setattr(
            "bedrock_agentcore.tools.browser_client.BrowserClient", Boom
        )

        with pytest.raises(LiveViewUnavailable) as exc:
            await browser_live_view.mint_live_view(dict(REF))

        assert exc.value.code == 409

    @pytest.mark.asyncio
    async def test_an_unusable_viewport_falls_back_rather_than_failing(
        self, fake_client
    ):
        minted = await browser_live_view.mint_live_view(
            {**REF, "viewport": {"width": "wide"}}
        )

        assert minted["viewport"] == browser_live_view.DEFAULT_VIEWPORT

    @pytest.mark.asyncio
    async def test_expiry_is_measured_from_the_request_not_the_read(self, fake_client):
        from datetime import datetime, timezone

        before = datetime.now(timezone.utc)
        minted = await browser_live_view.mint_live_view(dict(REF))
        expires = datetime.fromisoformat(minted["expiresAt"])

        window = (expires - before).total_seconds()
        assert 0 < window <= browser_live_view.LIVE_VIEW_EXPIRY_SECONDS + 1
