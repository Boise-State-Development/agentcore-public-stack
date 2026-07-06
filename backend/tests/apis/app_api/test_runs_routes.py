"""Route tests for the headless "Run now" surface (`/runs/*`).

Pins the three-layer gate (cookie auth → SCHEDULED_RUNS_ENABLED kill switch
→ `scheduled-runs` RBAC capability), the create-on-enable grant flow, and
the RunResult → camelCase response mapping.
"""

from __future__ import annotations

import time
from typing import Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.harness.auth import HeadlessAuthError
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

    async def enable(self, *, user_id, username, refresh_token, token_issued_at=None):
        self.enable_calls.append(
            {
                "user_id": user_id,
                "username": username,
                "refresh_token": refresh_token,
                "token_issued_at": token_issued_at,
            }
        )
        self.grant = _grant(user_id)
        return self.grant

    async def revoke(self, user_id: str) -> bool:
        self.revoke_calls.append(user_id)
        revoked = self.grant is not None
        self.grant = None
        return revoked


class FakeSessionRecord:
    """Just the SessionRecord fields the route reads."""

    username = "user1"
    cognito_refresh_token = "rt-live"
    created_at = NOW - 3600


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
    capability: bool = True,
    flag: Optional[str] = None,
    grants: Optional[FakeGrantService] = None,
    with_session_record: bool = False,
    run_result: Optional[RunResult] = None,
    run_error: Optional[Exception] = None,
    role_tools: Optional[list[str]] = None,
) -> tuple[TestClient, FakeGrantService, list[dict]]:
    monkeypatch.delenv("SKIP_AUTH", raising=False)
    if flag is None:
        monkeypatch.delenv("SCHEDULED_RUNS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("SCHEDULED_RUNS_ENABLED", flag)

    async def fake_capability(user, capability_id):
        assert capability_id == "scheduled-runs"
        return capability

    monkeypatch.setattr(runs_routes, "user_has_capability", fake_capability)

    grants = grants or FakeGrantService()
    monkeypatch.setattr(runs_routes, "get_headless_grant_service", lambda: grants)
    monkeypatch.setattr(runs_routes, "get_app_role_service", lambda: FakeRoleService(role_tools))

    run_calls: list[dict] = []

    async def fake_run(**kwargs):
        run_calls.append(kwargs)
        if run_error is not None:
            raise run_error
        return run_result or _completed_result()

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
    return client, grants, run_calls


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
        client, _, _ = _make_client(monkeypatch, authed=False)
        assert client.post("/runs/now", json={"prompt": "hi"}).status_code == 401

    def test_kill_switch_off_hides_the_surface_as_404(self, monkeypatch):
        client, _, _ = _make_client(monkeypatch, flag="false")
        assert client.post("/runs/now", json={"prompt": "hi"}).status_code == 404
        assert client.get("/runs/grant").status_code == 404
        assert client.delete("/runs/grant").status_code == 404

    def test_flag_defaults_on_when_unset(self, monkeypatch):
        client, _, _ = _make_client(monkeypatch, with_session_record=True)
        assert client.post("/runs/now", json={"prompt": "hi"}).status_code == 200

    def test_empty_flag_value_stays_on(self, monkeypatch):
        # `${{ vars.* }}` renders "" when unset — must resolve to the default.
        client, _, _ = _make_client(monkeypatch, flag="", with_session_record=True)
        assert client.post("/runs/now", json={"prompt": "hi"}).status_code == 200

    def test_missing_capability_is_403(self, monkeypatch):
        client, _, _ = _make_client(monkeypatch, capability=False)
        response = client.post("/runs/now", json={"prompt": "hi"})
        assert response.status_code == 403
        assert client.get("/runs/grant").status_code == 403


# ---------------------------------------------------------------------------
# POST /runs/now
# ---------------------------------------------------------------------------


class TestRunNow:
    def test_happy_path_maps_run_result_to_camel_case(self, monkeypatch):
        client, grants, run_calls = _make_client(
            monkeypatch, with_session_record=True
        )

        response = client.post(
            "/runs/now",
            json={
                "prompt": "ping",
                "title": "My Briefing",
                "enabledTools": ["class_search"],
                "agentType": "chat",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["runId"] == "run-1"
        assert body["sessionId"] == "headless-1"
        assert body["status"] == "completed"
        assert body["finalMessage"] == "pong"
        assert body["stopReason"] == "end_turn"
        assert body["toolTrace"] == [
            {
                "toolUseId": "t1",
                "name": "search_classes",
                "input": {"subject": "COMM"},
                "resultPreview": "ok",
                "isError": False,
            }
        ]
        assert body["usage"]["usage"]["totalTokens"] == 6

        (call,) = run_calls
        assert call["user_id"] == "user-1"
        assert call["prompt"] == "ping"
        assert call["title"] == "My Briefing"
        assert call["enabled_tools"] == ["class_search"]
        assert call["agent_type"] == "chat"
        assert call["trigger"] == "run_now"

    def test_enabled_tools_intersected_with_rbac_before_harness(self, monkeypatch):
        """A crafted body cannot enable a tool outside the caller's grant.

        FakeRoleService grants {class_search, web_search}; the ungranted
        ``gmail_search`` must be stripped before the harness (and thus the
        RBAC-blind tool filter) ever sees it.
        """
        client, _, run_calls = _make_client(monkeypatch, with_session_record=True)

        response = client.post(
            "/runs/now",
            json={"prompt": "ping", "enabledTools": ["class_search", "gmail_search"]},
        )

        assert response.status_code == 200
        (call,) = run_calls
        assert call["enabled_tools"] == ["class_search"]

    def test_none_enabled_tools_passes_through_as_defaults(self, monkeypatch):
        """Omitting enabled_tools stays None → harness resolves user defaults."""
        client, _, run_calls = _make_client(monkeypatch, with_session_record=True)

        assert client.post("/runs/now", json={"prompt": "ping"}).status_code == 200

        (call,) = run_calls
        assert call["enabled_tools"] is None

    def test_create_on_enable_pins_the_live_session_token(self, monkeypatch):
        client, grants, _ = _make_client(monkeypatch, with_session_record=True)

        client.post("/runs/now", json={"prompt": "ping"})

        (enable,) = grants.enable_calls
        assert enable["user_id"] == "user-1"
        assert enable["username"] == "user1"
        assert enable["refresh_token"] == "rt-live"
        # The session's login instant anchors the 30-day recency window.
        assert enable["token_issued_at"] == NOW - 3600

    def test_existing_grant_works_without_a_session_record(self, monkeypatch):
        grants = FakeGrantService(grant=_grant())
        client, grants, _ = _make_client(monkeypatch, grants=grants)

        assert client.post("/runs/now", json={"prompt": "ping"}).status_code == 200
        assert grants.enable_calls == []  # no session to re-pin from

    def test_no_grant_and_no_session_is_409(self, monkeypatch):
        client, _, run_calls = _make_client(monkeypatch)

        response = client.post("/runs/now", json={"prompt": "ping"})

        assert response.status_code == 409
        assert run_calls == []  # never reached the harness

    def test_mint_failure_is_409_not_401(self, monkeypatch):
        # 401 would bounce the SPA through the login redirect; the *session*
        # is fine — only the headless credential is dead.
        client, _, _ = _make_client(
            monkeypatch,
            with_session_record=True,
            run_error=HeadlessAuthError("cognito refused"),
        )

        assert client.post("/runs/now", json={"prompt": "ping"}).status_code == 409

    def test_empty_prompt_is_422(self, monkeypatch):
        client, _, _ = _make_client(monkeypatch, with_session_record=True)
        assert client.post("/runs/now", json={"prompt": ""}).status_code == 422


# ---------------------------------------------------------------------------
# Grant lifecycle routes
# ---------------------------------------------------------------------------


class TestEnableGrant:
    """POST /runs/grant — shares `_resolve_grant` with /runs/now, so this
    pins the same create-on-enable behavior via a distinct entrypoint that
    doesn't require running a prompt first."""

    def test_creates_grant_from_live_session(self, monkeypatch):
        client, grants, _ = _make_client(monkeypatch, with_session_record=True)

        response = client.post("/runs/grant")

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["grantId"] == "hlg-abc"
        assert "rt-stored" not in str(body)

        (enable,) = grants.enable_calls
        assert enable["user_id"] == "user-1"
        assert enable["username"] == "user1"
        assert enable["refresh_token"] == "rt-live"
        assert enable["token_issued_at"] == NOW - 3600

    def test_refreshes_an_existing_grant_from_a_new_session(self, monkeypatch):
        grants = FakeGrantService(grant=_grant())
        client, grants, _ = _make_client(
            monkeypatch, grants=grants, with_session_record=True
        )

        response = client.post("/runs/grant")

        assert response.status_code == 200
        assert len(grants.enable_calls) == 1  # re-pinned, not skipped

    def test_existing_grant_without_a_session_record_is_reused(self, monkeypatch):
        grants = FakeGrantService(grant=_grant())
        client, grants, _ = _make_client(monkeypatch, grants=grants)

        response = client.post("/runs/grant")

        assert response.status_code == 200
        assert response.json()["grantId"] == "hlg-abc"
        assert grants.enable_calls == []

    def test_no_grant_and_no_session_is_409(self, monkeypatch):
        client, _, _ = _make_client(monkeypatch)

        assert client.post("/runs/grant").status_code == 409

    def test_kill_switch_off_hides_the_surface_as_404(self, monkeypatch):
        client, _, _ = _make_client(monkeypatch, flag="false")
        assert client.post("/runs/grant").status_code == 404

    def test_missing_capability_is_403(self, monkeypatch):
        client, _, _ = _make_client(monkeypatch, capability=False)
        assert client.post("/runs/grant").status_code == 403

    def test_unauthenticated_request_is_401(self, monkeypatch):
        client, _, _ = _make_client(monkeypatch, authed=False)
        assert client.post("/runs/grant").status_code == 401


class TestGrantRoutes:
    def test_grant_status_when_enabled(self, monkeypatch):
        client, _, _ = _make_client(monkeypatch, grants=FakeGrantService(_grant()))

        body = client.get("/runs/grant").json()

        assert body["enabled"] is True
        assert body["grantId"] == "hlg-abc"
        assert body["expiresAt"] == NOW + 1000
        # The stored credential itself must never surface.
        assert "rt-stored" not in str(body)

    def test_grant_status_when_absent(self, monkeypatch):
        client, _, _ = _make_client(monkeypatch)
        assert client.get("/runs/grant").json() == {
            "enabled": False,
            "grantId": None,
            "createdAt": None,
            "updatedAt": None,
            "expiresAt": None,
            "lastUsedAt": None,
        }

    def test_revoke_grant(self, monkeypatch):
        client, grants, _ = _make_client(monkeypatch, grants=FakeGrantService(_grant()))

        assert client.delete("/runs/grant").json() == {"revoked": True}
        assert grants.revoke_calls == ["user-1"]
        assert client.delete("/runs/grant").json() == {"revoked": False}
