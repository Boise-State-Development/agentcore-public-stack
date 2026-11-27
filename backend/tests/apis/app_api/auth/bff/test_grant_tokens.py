"""A headless grant's refresh token outlives a sign-out, and nothing else does.

The BFF app client has no refresh-token rotation, so a headless grant holds
the very token of the session it was pinned from. Before this, signing out of
that session revoked the token and silently stopped every scheduled run
(docs/specs/proactive-trust.md §5.2). These tests pin each path of the fix:
logout of the pinned session keeps the token, logout of any other session
revokes as before, a token the grant lets go of is revoked once no session
holds it, and only a web sign-in re-pins, and only an active grant.
"""

from __future__ import annotations

import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from apis.app_api.auth.bff import grant_tokens
from apis.app_api.auth.bff import routes as bff_routes
from apis.app_api.auth.bff.token_exchange import ExchangeResult
from apis.shared.harness.grants import HeadlessGrant
from apis.shared.sessions_bff.config import SESSION_COOKIE_NAME
from apis.shared.sessions_bff.models import CookiePayload, SessionRecord

from .conftest import make_id_token
from .test_callback import _bound_client, _patch_token_exchange, _seed_state
from .test_logout import _seed_record

NOW = int(time.time())


def _grant(*, token: str = "r.t", pinned: Optional[str] = "sess-x", status: str = "active") -> HeadlessGrant:
    return HeadlessGrant(
        grant_id="hlg-1",
        user_id="user-sub",
        username="alice",
        cognito_refresh_token=token,
        status=status,
        created_at=NOW - 100,
        updated_at=NOW - 100,
        token_issued_at=NOW - 100,
        ttl=NOW + 10_000,
        pinned_session_id=pinned,
    )


class FakeGrants:
    enabled = True

    def __init__(self, grant: Optional[HeadlessGrant] = None, *, error: Optional[Exception] = None):
        self.grant = grant
        self.error = error
        self.enable_calls: list[dict] = []

    async def get_active_grant(self, user_id: str):
        if self.error is not None:
            raise self.error
        return self.grant if self.grant is not None and self.grant.is_active else None

    async def enable(self, **kwargs):
        self.enable_calls.append(kwargs)
        self.grant = _grant(token=kwargs["refresh_token"], pinned=kwargs.get("session_id"))
        return self.grant


class FakeSessions:
    enabled = True

    def __init__(self, records: Optional[dict] = None, *, error: Optional[Exception] = None):
        self.records = dict(records or {})
        self.error = error

    async def get(self, session_id: str):
        if self.error is not None:
            raise self.error
        return self.records.get(session_id)


def _cognito() -> MagicMock:
    client = MagicMock()
    client.enabled = True
    client.revoke = AsyncMock()
    return client


def _record(session_id: str = "sess-x", token: str = "r.t") -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        user_id="user-sub",
        username="alice",
        cognito_access_token="a.t",
        cognito_refresh_token=token,
        id_token=None,
        access_token_exp=NOW + 3600,
        csrf_secret="c",
        created_at=NOW,
        last_seen_at=NOW,
        ttl=NOW + 28_800,
    )


# =====================================================================
# grant_tokens rules
# =====================================================================


class TestGrantKeepsToken:
    @pytest.mark.asyncio
    async def test_true_when_the_active_grant_holds_this_sessions_token(self):
        assert await grant_tokens.grant_keeps_token(_record(), FakeGrants(_grant()))

    @pytest.mark.asyncio
    async def test_false_for_another_sessions_token(self):
        assert not await grant_tokens.grant_keeps_token(_record(token="other"), FakeGrants(_grant()))

    @pytest.mark.asyncio
    async def test_false_without_an_active_grant(self):
        assert not await grant_tokens.grant_keeps_token(_record(), FakeGrants(None))
        assert not await grant_tokens.grant_keeps_token(_record(), FakeGrants(_grant(status="revoked")))
        assert not await grant_tokens.grant_keeps_token(_record(), FakeGrants(_grant(status="needs_reauth")))

    @pytest.mark.asyncio
    async def test_an_unreadable_grant_fails_toward_revoking(self):
        assert not await grant_tokens.grant_keeps_token(_record(), FakeGrants(error=RuntimeError("ddb down")))


class TestReleaseRefreshToken:
    @pytest.mark.asyncio
    async def test_revokes_once_the_pinned_session_is_gone(self):
        cognito = _cognito()
        revoked = await grant_tokens.release_refresh_token("r.t", "sess-x", repository=FakeSessions(), cognito=cognito)
        assert revoked
        cognito.revoke.assert_awaited_once_with(refresh_token="r.t")

    @pytest.mark.asyncio
    async def test_keeps_a_token_its_live_session_holds(self):
        cognito = _cognito()
        sessions = FakeSessions({"sess-x": _record()})
        assert not await grant_tokens.release_refresh_token("r.t", "sess-x", repository=sessions, cognito=cognito)
        cognito.revoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_revokes_when_the_session_moved_to_another_token(self):
        cognito = _cognito()
        sessions = FakeSessions({"sess-x": _record(token="newer")})
        assert await grant_tokens.release_refresh_token("r.t", "sess-x", repository=sessions, cognito=cognito)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pinned", [None, ""])
    async def test_leaves_a_legacy_grants_token_alone(self, pinned):
        cognito = _cognito()
        assert not await grant_tokens.release_refresh_token("r.t", pinned, repository=FakeSessions(), cognito=cognito)
        cognito.revoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unreadable_or_disabled_session_table_leaves_it_alone(self):
        cognito = _cognito()
        failing = FakeSessions(error=RuntimeError("ddb down"))
        assert not await grant_tokens.release_refresh_token("r.t", "sess-x", repository=failing, cognito=cognito)
        disabled = FakeSessions()
        disabled.enabled = False
        assert not await grant_tokens.release_refresh_token("r.t", "sess-x", repository=disabled, cognito=cognito)
        cognito.revoke.assert_not_awaited()


class TestPinGrant:
    @pytest.mark.asyncio
    async def test_sign_in_repins_an_active_grant_and_releases_the_old_token(self):
        grants = FakeGrants(_grant(token="old.t", pinned="sess-old"))
        cognito = _cognito()

        grant = await grant_tokens.pin_grant(
            _record("sess-new", "new.t"), grants=grants, repository=FakeSessions(), cognito=cognito, only_if_active=True
        )

        assert grant is not None and grant.pinned_session_id == "sess-new"
        (call,) = grants.enable_calls
        assert call["refresh_token"] == "new.t" and call["session_id"] == "sess-new"
        cognito.revoke.assert_awaited_once_with(refresh_token="old.t")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("grant", [None, _grant(status="revoked"), _grant(status="needs_reauth")])
    async def test_sign_in_never_creates_or_revives_a_grant(self, grant):
        grants = FakeGrants(grant)

        result = await grant_tokens.pin_grant(
            _record("sess-new", "new.t"), grants=grants, repository=FakeSessions(), cognito=_cognito(), only_if_active=True
        )

        assert result is None
        assert grants.enable_calls == []

    @pytest.mark.asyncio
    async def test_already_pinned_to_this_session_writes_nothing(self):
        grants = FakeGrants(_grant())
        await grant_tokens.pin_grant(_record(), grants=grants, repository=FakeSessions(), cognito=_cognito())
        assert grants.enable_calls == []

    @pytest.mark.asyncio
    async def test_enable_creates_a_grant_when_there_is_none(self):
        grants = FakeGrants(None)
        cognito = _cognito()
        grant = await grant_tokens.pin_grant(_record(), grants=grants, repository=FakeSessions(), cognito=cognito)
        assert grant is not None and grants.enable_calls
        cognito.revoke.assert_not_awaited()


# =====================================================================
# POST /auth/logout
# =====================================================================


@pytest.fixture
def revoke(monkeypatch):
    client = _cognito()
    monkeypatch.setattr(bff_routes, "_get_cognito_client", lambda: client)
    return client.revoke


def _use_grants(monkeypatch, grants: FakeGrants) -> FakeGrants:
    monkeypatch.setattr(bff_routes, "_get_grant_service", lambda: grants)
    return grants


class TestLogout:
    def test_logout_of_the_pinned_session_keeps_the_token_and_says_so(
        self, app, codec, repository, monkeypatch, revoke
    ):
        record = _seed_record(repository)
        _use_grants(monkeypatch, FakeGrants(_grant(token=record.cognito_refresh_token, pinned=record.session_id)))
        sealed = codec.seal(CookiePayload(session_id=record.session_id))

        response = TestClient(app).post("/auth/logout", cookies={SESSION_COOKIE_NAME: sealed})

        assert response.status_code == 200
        body = response.json()
        assert body["scheduled_runs_continue"] is True
        assert body["post_logout_url"]
        revoke.assert_not_awaited()
        # Still signed out: the row is gone and the cookies are cleared.
        assert "Item" not in repository._table.get_item(Key={"PK": f"SESSION#{record.session_id}", "SK": "META"})
        assert SESSION_COOKIE_NAME in " ".join(response.headers.get_list("set-cookie"))

    def test_logout_of_any_other_session_revokes_as_before(self, app, codec, repository, monkeypatch, revoke):
        record = _seed_record(repository)
        _use_grants(monkeypatch, FakeGrants(_grant(token="someone-elses.t", pinned="sess-other")))
        sealed = codec.seal(CookiePayload(session_id=record.session_id))

        response = TestClient(app).post("/auth/logout", cookies={SESSION_COOKIE_NAME: sealed})

        assert "scheduled_runs_continue" not in response.json()
        revoke.assert_awaited_once_with(refresh_token=record.cognito_refresh_token)

    def test_logout_with_no_grant_revokes_as_before(self, app, codec, repository, monkeypatch, revoke):
        record = _seed_record(repository)
        _use_grants(monkeypatch, FakeGrants(None))
        sealed = codec.seal(CookiePayload(session_id=record.session_id))

        TestClient(app).post("/auth/logout", cookies={SESSION_COOKIE_NAME: sealed})

        revoke.assert_awaited_once_with(refresh_token=record.cognito_refresh_token)

    def test_an_unreadable_grant_revokes_as_before(self, app, codec, repository, monkeypatch, revoke):
        record = _seed_record(repository)
        _use_grants(monkeypatch, FakeGrants(error=RuntimeError("ddb down")))
        sealed = codec.seal(CookiePayload(session_id=record.session_id))

        TestClient(app).post("/auth/logout", cookies={SESSION_COOKIE_NAME: sealed})

        revoke.assert_awaited_once()

    def test_a_header_presented_session_follows_the_same_rule(self, app, codec, repository, monkeypatch, revoke):
        record = _seed_record(repository)
        _use_grants(monkeypatch, FakeGrants(_grant(token=record.cognito_refresh_token, pinned=record.session_id)))
        sealed = codec.seal(CookiePayload(session_id=record.session_id))

        response = TestClient(app).post("/auth/logout", headers={"Authorization": f"BFF {sealed}"})

        assert response.json()["scheduled_runs_continue"] is True
        assert response.headers.get_list("set-cookie") == []
        revoke.assert_not_awaited()


# =====================================================================
# GET /auth/callback — a web sign-in re-pins an active grant
# =====================================================================


def _sign_in_on_the_web(app, monkeypatch):
    state = _seed_state()
    _patch_token_exchange(
        monkeypatch,
        ExchangeResult(
            access_token="access.tok",
            refresh_token="refresh.tok",
            id_token=make_id_token(),
            access_token_exp=int(time.time()) + 3600,
        ),
    )
    return _bound_client(app).get(f"/auth/callback?code=auth-code&state={state}")


class TestWebSignIn:
    def test_repins_an_active_grant_to_the_new_session(self, app, monkeypatch, repository, revoke):
        grants = _use_grants(monkeypatch, FakeGrants(_grant(token="old.t", pinned="sess-signed-out")))

        response = _sign_in_on_the_web(app, monkeypatch)

        assert response.status_code == 302
        (call,) = grants.enable_calls
        assert call["refresh_token"] == "refresh.tok"
        (row,) = [i for i in repository._table.scan()["Items"] if i["PK"].startswith("SESSION#")]
        assert call["session_id"] == row["session_id"]
        revoke.assert_awaited_once_with(refresh_token="old.t")

    def test_never_creates_a_grant(self, app, monkeypatch):
        grants = _use_grants(monkeypatch, FakeGrants(None))

        assert _sign_in_on_the_web(app, monkeypatch).status_code == 302
        assert grants.enable_calls == []

    def test_a_revoked_grant_stays_revoked(self, app, monkeypatch):
        grants = _use_grants(monkeypatch, FakeGrants(_grant(status="revoked")))

        _sign_in_on_the_web(app, monkeypatch)

        assert grants.enable_calls == []

    def test_a_failing_repin_never_fails_the_sign_in(self, app, monkeypatch):
        _use_grants(monkeypatch, FakeGrants(error=RuntimeError("ddb down")))

        response = _sign_in_on_the_web(app, monkeypatch)

        assert response.status_code == 302
        assert SESSION_COOKIE_NAME in " ".join(response.headers.get_list("set-cookie"))


class TestWebSignInSettlesTheExpiryWarning:
    def test_a_repin_settles_with_the_sign_ins_email(self, app, monkeypatch, revoke):
        import apis.shared.scheduled_prompts.grant_warning as grant_warning

        calls = []
        monkeypatch.setattr(grant_warning, "grant_expiry_settled", lambda email, ttl, **_: calls.append((email, ttl)) or 0)
        grants = _use_grants(monkeypatch, FakeGrants(_grant(token="old.t", pinned="sess-signed-out")))

        _sign_in_on_the_web(app, monkeypatch)

        assert len(calls) == 1
        email, ttl = calls[0]
        assert email and ttl == grants.grant.ttl

    def test_no_active_grant_settles_nothing(self, app, monkeypatch):
        import apis.shared.scheduled_prompts.grant_warning as grant_warning

        calls = []
        monkeypatch.setattr(grant_warning, "grant_expiry_settled", lambda email, ttl, **_: calls.append(email) or 0)
        _use_grants(monkeypatch, FakeGrants(None))

        _sign_in_on_the_web(app, monkeypatch)

        assert calls == []
