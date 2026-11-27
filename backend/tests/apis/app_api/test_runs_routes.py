"""Route tests for the headless "Run now" surface (`/runs/*`).

Pins the two-layer gate (cookie auth → SCHEDULED_RUNS_ENABLED kill switch;
no RBAC capability gate), the create-on-enable grant flow, the background
start of ``POST /runs/now`` (202 before the turn runs), and the
``GET /runs/{runId}`` status read.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.harness.auth import HeadlessAuthError, StaticBearerAuth
from apis.shared.harness.grants import HeadlessGrant
from apis.shared.harness.models import RunResult, ToolTraceEntry
from apis.shared.tools.scoped_ids import base_tool_id

from apis.app_api.runs import routes as runs_routes

NOW = int(time.time())


class FakeRoleService:
    """Grants a fixed tool set; mirrors filter_requested_tools' contract."""

    def __init__(self, tools: Optional[list[str]] = None):
        self.tools = tools if tools is not None else ["class_search", "web_search"]

    async def filter_requested_tools(self, user, requested):
        allowed = set(self.tools)
        if "*" in allowed:
            return list(requested)
        return [t for t in requested if t in allowed or base_tool_id(t) in allowed]


def _user() -> User:
    return User(
        user_id="user-1",
        email="user@example.com",
        name="User",
        roles=["default"],
        raw_token="tok",
    )


def _grant(user_id: str = "user-1") -> HeadlessGrant:
    return HeadlessGrant(
        grant_id="hlg-abc",
        user_id=user_id,
        username="user1",
        cognito_refresh_token="rt-stored",
        status="active",
        created_at=NOW - 100,
        updated_at=NOW - 100,
        token_issued_at=NOW - 100,
        ttl=NOW + 1000,
        last_used_at=NOW - 50,
    )


class FakeGrantService:
    def __init__(self, grant: Optional[HeadlessGrant] = None):
        self.grant = grant
        self.enable_calls: list[dict] = []
        self.revoke_calls: list[str] = []

    async def get_active_grant(self, user_id: str):
        return self.grant

    async def enable(self, *, user_id, username, refresh_token, token_issued_at=None, session_id=None):
        self.enable_calls.append(
            {
                "user_id": user_id,
                "username": username,
                "refresh_token": refresh_token,
                "token_issued_at": token_issued_at,
                "session_id": session_id,
            }
        )
        self.grant = _grant(user_id)
        self.grant.cognito_refresh_token = refresh_token
        self.grant.pinned_session_id = session_id
        return self.grant

    async def revoke(self, user_id: str) -> bool:
        self.revoke_calls.append(user_id)
        revoked = self.grant is not None
        self.grant = None
        return revoked


class FakeAudit:
    """In-memory RunAuditRecorder keyed like the real table."""

    def __init__(self, *, fail_start: bool = False):
        self.fail_start = fail_start
        self.items: dict[tuple[str, str], dict] = {}
        self.ends: list[RunResult] = []

    def record_start(self, *, run_id, user_id, session_id, trigger, prompt, title=None):
        if self.fail_start:
            raise RuntimeError("dynamo down")
        self.items[(user_id, run_id)] = {
            "runId": run_id,
            "userId": user_id,
            "sessionId": session_id,
            "trigger": trigger,
            "status": "started",
            "startedAt": "2026-07-05T00:00:00Z",
            **({"title": title} if title else {}),
        }

    def record_end(self, *, result):
        self.ends.append(result)
        item = self.items.setdefault((result.user_id, result.run_id), {})
        item.update(
            status=result.status,
            errorDetail=result.error or "",
            title=result.title or "",
            finishedAt="2026-07-05T00:01:10Z",
        )

    def get(self, *, user_id, run_id):
        return self.items.get((user_id, run_id))


class FakeAuth:
    def __init__(self, error: Optional[Exception] = None):
        self.error = error
        self.minted_for: list[str] = []

    async def mint_bearer_for_user(self, user_id: str) -> str:
        self.minted_for.append(user_id)
        if self.error is not None:
            raise self.error
        return "bearer-minted"


class Harness:
    """What a test can inspect after building a client."""

    def __init__(self, grants, audit, auth):
        self.grants = grants
        self.audit = audit
        self.auth = auth
        self.run_calls: list[dict] = []
        self.spawned: list = []

    def drain(self) -> None:
        """Run every background run the route spawned, in order."""
        while self.spawned:
            asyncio.run(self.spawned.pop(0))

    def close(self) -> None:
        """Discard runs a test never drained (no "never awaited" warnings)."""
        for coro in self.spawned:
            coro.close()
        self.spawned.clear()


_harnesses: list[Harness] = []


@pytest.fixture(autouse=True)
def _close_undrained_runs():
    yield
    while _harnesses:
        _harnesses.pop().close()


class FakeSessionRecord:
    """Just the SessionRecord fields the route reads."""

    user_id = "user-1"
    session_id = "sess-live"
    username = "user1"
    cognito_refresh_token = "rt-live"
    created_at = NOW - 3600


class FakeSessionRepository:
    """The BFF session table, as the grant's token bookkeeping reads it."""

    enabled = True

    def __init__(self, records: Optional[dict] = None):
        self.records = dict(records or {})

    async def get(self, session_id: str):
        return self.records.get(session_id)


class FakeCognito:
    enabled = True

    def __init__(self):
        self.revoked: list[str] = []

    async def revoke(self, *, refresh_token: str) -> None:
        self.revoked.append(refresh_token)


def _completed_result() -> RunResult:
    return RunResult(
        run_id="run-1",
        session_id="headless-1",
        user_id="user-1",
        status="completed",
        final_message="pong",
        stop_reason="end_turn",
        title="T",
        tool_trace=[
            ToolTraceEntry(
                tool_use_id="t1",
                name="search_classes",
                input={"subject": "COMM"},
                result_preview="ok",
            )
        ],
        usage={"usage": {"totalTokens": 6}},
        started_at="2026-07-05T00:00:00Z",
        finished_at="2026-07-05T00:00:10Z",
    )


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    authed: bool = True,
    flag: Optional[str] = None,
    grants: Optional[FakeGrantService] = None,
    with_session_record: bool = False,
    run_result: Optional[RunResult] = None,
    run_error: Optional[Exception] = None,
    mint_error: Optional[Exception] = None,
    audit: Optional[FakeAudit] = None,
    role_tools: Optional[list[str]] = None,
    sessions: Optional[FakeSessionRepository] = None,
    cognito: Optional[FakeCognito] = None,
) -> tuple[TestClient, Harness]:
    monkeypatch.delenv("SKIP_AUTH", raising=False)
    if flag is None:
        monkeypatch.delenv("SCHEDULED_RUNS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", flag)

    h = Harness(grants or FakeGrantService(), audit or FakeAudit(), FakeAuth(mint_error))
    _harnesses.append(h)
    monkeypatch.setattr(runs_routes, "get_headless_grant_service", lambda: h.grants)
    monkeypatch.setattr(runs_routes, "get_run_audit_recorder", lambda: h.audit)
    monkeypatch.setattr(runs_routes, "get_headless_auth", lambda: h.auth)
    monkeypatch.setattr(runs_routes, "get_app_role_service", lambda: FakeRoleService(role_tools))
    monkeypatch.setattr(runs_routes, "spawn_run", h.spawned.append)
    h.sessions = sessions or FakeSessionRepository()
    h.cognito = cognito or FakeCognito()
    monkeypatch.setattr(runs_routes, "get_grant_token_deps", lambda: (h.sessions, h.cognito))

    async def fake_run(**kwargs):
        h.run_calls.append(kwargs)
        if run_error is not None:
            raise run_error
        result = run_result or _completed_result()
        result.run_id = kwargs["run_id"]
        result.session_id = kwargs["session_id"]
        await kwargs["governance"].on_run_end(result=result)
        return result

    monkeypatch.setattr(runs_routes, "run_agent_headless", fake_run)

    app = FastAPI()

    if with_session_record:
        @app.middleware("http")
        async def attach_session(request: Request, call_next):
            request.state.bff_session = FakeSessionRecord()
            return await call_next(request)

    app.include_router(runs_routes.router)
    if authed:
        app.dependency_overrides[get_current_user_from_session] = _user
    client = TestClient(app, raise_server_exceptions=False)
    return client, h


# ---------------------------------------------------------------------------
# scheduled_runs_enabled() helper — default ON with a kill switch
# ---------------------------------------------------------------------------


class TestScheduledRunsFlag:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, True),  # unset → default on
            ("", True),  # empty workflow var → default on
            ("true", True),
            ("false", False),
            ("FALSE", False),
            ("0", True),  # only the literal "false" disables
        ],
    )
    def test_parses_env_value(self, monkeypatch, value, expected):
        from apis.shared.feature_flags import scheduled_runs_enabled

        if value is None:
            monkeypatch.delenv("SCHEDULED_RUNS_ENABLED", raising=False)
        else:
            monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", value)
        assert scheduled_runs_enabled() is expected


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


class TestGating:
    def test_unauthenticated_request_is_401(self, monkeypatch):
        client, _ = _make_client(monkeypatch, authed=False)
        assert client.post("/runs/now", json={"prompt": "hi"}).status_code == 401

    def test_kill_switch_off_hides_the_surface_as_404(self, monkeypatch):
        client, _ = _make_client(monkeypatch, flag="false")
        assert client.post("/runs/now", json={"prompt": "hi"}).status_code == 404
        assert client.get("/runs/grant").status_code == 404
        assert client.delete("/runs/grant").status_code == 404
        assert client.get("/runs/run-0123456789ab").status_code == 404

    def test_flag_defaults_on_when_unset(self, monkeypatch):
        client, _ = _make_client(monkeypatch, with_session_record=True)
        assert client.post("/runs/now", json={"prompt": "hi"}).status_code == 202

    def test_empty_flag_value_stays_on(self, monkeypatch):
        # `${{ vars.* }}` renders "" when unset — must resolve to the default.
        client, _ = _make_client(monkeypatch, flag="", with_session_record=True)
        assert client.post("/runs/now", json={"prompt": "hi"}).status_code == 202

    def test_any_authenticated_user_is_allowed(self, monkeypatch):
        # No RBAC capability gate: an ordinary authenticated user gets 202,
        # not the old 403. (Regression for the prod "Access Denied" toast.)
        client, _ = _make_client(monkeypatch, with_session_record=True)
        assert client.post("/runs/now", json={"prompt": "hi"}).status_code == 202


# ---------------------------------------------------------------------------
# POST /runs/now
# ---------------------------------------------------------------------------


class TestRunNow:
    def test_returns_202_with_ids_before_the_turn_runs(self, monkeypatch):
        client, h = _make_client(monkeypatch, with_session_record=True)

        response = client.post("/runs/now", json={"prompt": "ping", "title": "My Briefing"})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "started"
        assert body["runId"].startswith("run-")
        assert body["sessionId"].startswith("headless-")
        # Spawned, not run: the response doesn't wait on the turn.
        assert len(h.spawned) == 1
        assert h.run_calls == []
        # The start row exists before the 202, with the caller's title.
        row = h.audit.get(user_id="user-1", run_id=body["runId"])
        assert row["status"] == "started"
        assert row["sessionId"] == body["sessionId"]
        assert row["trigger"] == "run_now"
        assert row["title"] == "My Briefing"

    def test_background_run_gets_the_request_config(self, monkeypatch):
        client, h = _make_client(monkeypatch, with_session_record=True)

        body = client.post(
            "/runs/now",
            json={
                "prompt": "ping",
                "title": "My Briefing",
                "enabledTools": ["class_search"],
                "agentType": "chat",
            },
        ).json()
        h.drain()

        (call,) = h.run_calls
        assert call["user_id"] == "user-1"
        assert call["prompt"] == "ping"
        assert call["title"] == "My Briefing"
        assert call["enabled_tools"] == ["class_search"]
        assert call["agent_type"] == "chat"
        assert call["trigger"] == "run_now"
        assert call["run_id"] == body["runId"]
        assert call["session_id"] == body["sessionId"]
        # The route already wrote the start row and minted the bearer.
        assert call["audit_started"] is True
        assert isinstance(call["auth"], StaticBearerAuth)
        assert h.auth.minted_for == ["user-1"]

    def test_enabled_tools_intersected_with_rbac_before_harness(self, monkeypatch):
        """A crafted body cannot enable a tool outside the caller's grant.

        FakeRoleService grants {class_search, web_search}; the ungranted
        ``gmail_search`` must be stripped before the harness (and thus the
        RBAC-blind tool filter) ever sees it.
        """
        client, h = _make_client(monkeypatch, with_session_record=True)

        response = client.post(
            "/runs/now",
            json={"prompt": "ping", "enabledTools": ["class_search", "gmail_search"]},
        )
        h.drain()

        assert response.status_code == 202
        (call,) = h.run_calls
        assert call["enabled_tools"] == ["class_search"]

    def test_none_enabled_tools_passes_through_as_defaults(self, monkeypatch):
        """Omitting enabled_tools stays None → harness resolves user defaults."""
        client, h = _make_client(monkeypatch, with_session_record=True)

        assert client.post("/runs/now", json={"prompt": "ping"}).status_code == 202
        h.drain()

        (call,) = h.run_calls
        assert call["enabled_tools"] is None

    def test_create_on_enable_pins_the_live_session_token(self, monkeypatch):
        client, h = _make_client(monkeypatch, with_session_record=True)

        client.post("/runs/now", json={"prompt": "ping"})

        (enable,) = h.grants.enable_calls
        assert enable["user_id"] == "user-1"
        assert enable["username"] == "user1"
        assert enable["refresh_token"] == "rt-live"
        # The session's login instant anchors the 30-day recency window.
        assert enable["token_issued_at"] == NOW - 3600

    def test_existing_grant_works_without_a_session_record(self, monkeypatch):
        client, h = _make_client(monkeypatch, grants=FakeGrantService(grant=_grant()))

        assert client.post("/runs/now", json={"prompt": "ping"}).status_code == 202
        assert h.grants.enable_calls == []  # no session to re-pin from

    def test_no_grant_and_no_session_is_409(self, monkeypatch):
        client, h = _make_client(monkeypatch)

        response = client.post("/runs/now", json={"prompt": "ping"})

        assert response.status_code == 409
        assert h.spawned == []  # never reached the harness
        assert h.audit.items == {}

    def test_mint_failure_is_409_not_401(self, monkeypatch):
        # 401 would bounce the SPA through the login redirect; the *session*
        # is fine — only the headless credential is dead. Still synchronous:
        # the mint happens before the 202.
        client, h = _make_client(
            monkeypatch,
            with_session_record=True,
            mint_error=HeadlessAuthError("cognito refused"),
        )

        assert client.post("/runs/now", json={"prompt": "ping"}).status_code == 409
        assert h.spawned == []
        assert h.audit.items == {}

    def test_audit_start_failure_fails_the_request_and_runs_nothing(self, monkeypatch):
        client, h = _make_client(
            monkeypatch, with_session_record=True, audit=FakeAudit(fail_start=True)
        )

        assert client.post("/runs/now", json={"prompt": "ping"}).status_code == 500
        assert h.spawned == []

    def test_a_run_that_raises_still_ends_its_audit_row(self, monkeypatch):
        """Otherwise the row stays ``started`` and the poll waits it out."""
        client, h = _make_client(
            monkeypatch, with_session_record=True, run_error=RuntimeError("boom")
        )

        body = client.post("/runs/now", json={"prompt": "ping", "title": "T"}).json()
        h.drain()

        row = h.audit.get(user_id="user-1", run_id=body["runId"])
        assert row["status"] == "error"
        assert row["errorDetail"] == "The run stopped unexpectedly."
        assert row["title"] == "T"
        assert "boom" not in row["errorDetail"]

    def test_empty_prompt_is_422(self, monkeypatch):
        client, _ = _make_client(monkeypatch, with_session_record=True)
        assert client.post("/runs/now", json={"prompt": ""}).status_code == 422


# ---------------------------------------------------------------------------
# GET /runs/{run_id}
# ---------------------------------------------------------------------------


class TestGetRun:
    def test_started_run_reads_as_started(self, monkeypatch):
        client, h = _make_client(monkeypatch, with_session_record=True)
        started = client.post("/runs/now", json={"prompt": "ping"}).json()

        response = client.get(f"/runs/{started['runId']}")

        assert response.status_code == 200
        assert response.json() == {
            "runId": started["runId"],
            "sessionId": started["sessionId"],
            "status": "started",
            "title": None,
            "error": None,
            "startedAt": "2026-07-05T00:00:00Z",
            "finishedAt": None,
        }

    def test_finished_run_reads_its_outcome(self, monkeypatch):
        client, h = _make_client(monkeypatch, with_session_record=True)
        started = client.post("/runs/now", json={"prompt": "ping"}).json()
        h.drain()

        body = client.get(f"/runs/{started['runId']}").json()

        assert body["status"] == "completed"
        assert body["title"] == "T"  # the run's generated title
        assert body["error"] is None
        assert body["finishedAt"] == "2026-07-05T00:01:10Z"

    def test_errored_run_reads_its_error(self, monkeypatch):
        client, h = _make_client(
            monkeypatch,
            with_session_record=True,
            run_result=RunResult(
                run_id="x", session_id="x", user_id="user-1", status="error", error="HTTP 500: nope"
            ),
        )
        started = client.post("/runs/now", json={"prompt": "ping"}).json()
        h.drain()

        body = client.get(f"/runs/{started['runId']}").json()

        assert body["status"] == "error"
        assert body["error"] == "HTTP 500: nope"

    def test_another_users_run_is_404(self, monkeypatch):
        audit = FakeAudit()
        audit.record_start(
            run_id="run-0123456789ab",
            user_id="someone-else",
            session_id="headless-x",
            trigger="run_now",
            prompt="secret",
        )
        client, _ = _make_client(monkeypatch, audit=audit)

        assert client.get("/runs/run-0123456789ab").status_code == 404

    def test_unknown_run_is_404(self, monkeypatch):
        client, _ = _make_client(monkeypatch)
        assert client.get("/runs/run-0123456789ab").status_code == 404

    def test_malformed_run_id_is_422(self, monkeypatch):
        client, _ = _make_client(monkeypatch)
        assert client.get("/runs/not-a-run").status_code == 422

    def test_grant_route_is_not_read_as_a_run_id(self, monkeypatch):
        client, _ = _make_client(monkeypatch, grants=FakeGrantService(_grant()))
        assert client.get("/runs/grant").json()["enabled"] is True

    def test_unauthenticated_request_is_401(self, monkeypatch):
        client, _ = _make_client(monkeypatch, authed=False)
        assert client.get("/runs/run-0123456789ab").status_code == 401


# ---------------------------------------------------------------------------
# Grant lifecycle routes
# ---------------------------------------------------------------------------


class TestEnableGrant:
    """POST /runs/grant — shares `_resolve_grant` with /runs/now, so this
    pins the same create-on-enable behavior via a distinct entrypoint that
    doesn't require running a prompt first."""

    def test_creates_grant_from_live_session(self, monkeypatch):
        client, h = _make_client(monkeypatch, with_session_record=True)

        response = client.post("/runs/grant")

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["grantId"] == "hlg-abc"
        assert "rt-stored" not in str(body)

        (enable,) = h.grants.enable_calls
        assert enable["user_id"] == "user-1"
        assert enable["username"] == "user1"
        assert enable["refresh_token"] == "rt-live"
        assert enable["token_issued_at"] == NOW - 3600
        assert enable["session_id"] == "sess-live"

    def test_refreshes_an_existing_grant_from_a_new_session(self, monkeypatch):
        grants = FakeGrantService(grant=_grant())
        client, h = _make_client(
            monkeypatch, grants=grants, with_session_record=True
        )

        response = client.post("/runs/grant")

        assert response.status_code == 200
        assert len(grants.enable_calls) == 1  # re-pinned, not skipped

    def test_existing_grant_without_a_session_record_is_reused(self, monkeypatch):
        grants = FakeGrantService(grant=_grant())
        client, h = _make_client(monkeypatch, grants=grants)

        response = client.post("/runs/grant")

        assert response.status_code == 200
        assert response.json()["grantId"] == "hlg-abc"
        assert grants.enable_calls == []

    def test_no_grant_and_no_session_is_409(self, monkeypatch):
        client, _ = _make_client(monkeypatch)

        assert client.post("/runs/grant").status_code == 409

    def test_kill_switch_off_hides_the_surface_as_404(self, monkeypatch):
        client, _ = _make_client(monkeypatch, flag="false")
        assert client.post("/runs/grant").status_code == 404

    def test_unauthenticated_request_is_401(self, monkeypatch):
        client, _ = _make_client(monkeypatch, authed=False)
        assert client.post("/runs/grant").status_code == 401


class TestGrantRoutes:
    def test_grant_status_when_enabled(self, monkeypatch):
        client, _ = _make_client(monkeypatch, grants=FakeGrantService(_grant()))

        body = client.get("/runs/grant").json()

        assert body["enabled"] is True
        assert body["grantId"] == "hlg-abc"
        assert body["expiresAt"] == NOW + 1000
        # The stored credential itself must never surface.
        assert "rt-stored" not in str(body)

    def test_grant_status_when_absent(self, monkeypatch):
        client, _ = _make_client(monkeypatch)
        assert client.get("/runs/grant").json() == {
            "enabled": False,
            "grantId": None,
            "createdAt": None,
            "updatedAt": None,
            "expiresAt": None,
            "lastUsedAt": None,
        }

    def test_revoke_grant(self, monkeypatch):
        client, h = _make_client(monkeypatch, grants=FakeGrantService(_grant()))

        assert client.delete("/runs/grant").json() == {"revoked": True}
        assert h.grants.revoke_calls == ["user-1"]
        assert client.delete("/runs/grant").json() == {"revoked": False}

    def test_revoke_also_revokes_a_token_no_session_holds(self, monkeypatch):
        """Logout kept the token alive for the grant; revoking the grant must end it."""
        grant = _grant()
        grant.pinned_session_id = "sess-signed-out"
        client, h = _make_client(monkeypatch, grants=FakeGrantService(grant))

        assert client.delete("/runs/grant").json() == {"revoked": True}
        assert h.cognito.revoked == ["rt-stored"]

    def test_revoke_leaves_a_token_its_live_session_still_holds(self, monkeypatch):
        grant = _grant()
        grant.pinned_session_id = "sess-live"
        live = FakeSessionRecord()
        live.cognito_refresh_token = "rt-stored"
        client, h = _make_client(
            monkeypatch, grants=FakeGrantService(grant), sessions=FakeSessionRepository({"sess-live": live})
        )

        assert client.delete("/runs/grant").json() == {"revoked": True}
        assert h.cognito.revoked == []

    def test_revoke_leaves_a_legacy_grants_token_alone(self, monkeypatch):
        """A grant pinned before the session was recorded: its session may still be live."""
        client, h = _make_client(monkeypatch, grants=FakeGrantService(_grant()))

        client.delete("/runs/grant")

        assert h.cognito.revoked == []


class TestRepinSettlesTheExpiryWarning:
    """A re-pin that moves the grant past the warning window settles its bell rows (§5.1)."""

    def test_enable_settles_with_the_callers_email_and_the_new_expiry(self, monkeypatch):
        import apis.shared.scheduled_prompts.grant_warning as grant_warning

        calls = []
        monkeypatch.setattr(grant_warning, "grant_expiry_settled", lambda email, ttl, **_: calls.append((email, ttl)) or 0)
        client, h = _make_client(monkeypatch, with_session_record=True)

        assert client.post("/runs/grant").status_code == 200
        assert calls == [(_user().email, h.grants.grant.ttl)]


class TestRepinReleasesTheOldToken:
    def test_repin_revokes_the_token_of_a_signed_out_session(self, monkeypatch):
        grant = _grant()
        grant.pinned_session_id = "sess-old"
        client, h = _make_client(monkeypatch, grants=FakeGrantService(grant), with_session_record=True)

        assert client.post("/runs/grant").status_code == 200
        assert h.grants.grant.cognito_refresh_token == "rt-live"
        assert h.cognito.revoked == ["rt-stored"]

    def test_repin_keeps_the_token_of_a_live_session(self, monkeypatch):
        grant = _grant()
        grant.pinned_session_id = "sess-old"
        old = FakeSessionRecord()
        old.cognito_refresh_token = "rt-stored"
        client, h = _make_client(
            monkeypatch,
            grants=FakeGrantService(grant),
            with_session_record=True,
            sessions=FakeSessionRepository({"sess-old": old}),
        )

        client.post("/runs/grant")

        assert h.cognito.revoked == []
