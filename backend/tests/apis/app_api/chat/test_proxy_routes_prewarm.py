"""Session prewarm (docs/specs/agentcore-runtime-v2.md §5a).

`POST /chat/prewarm` sends a no-op `warm` invocation to the conversation's
pinned Runtime session so its microVM starts before the first send. Pinned
here: the flag gates it (404 off), it answers 202 at once and forwards in the
background with the same affinity header a real turn carries, it dedupes and
rate-limits per user, and an upstream failure never surfaces.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import List, Optional

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.chat import proxy_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.harness.runner import RUNTIME_SESSION_ID_HEADER, runtime_session_id_for


def _user(user_id: str = "user-sub", raw_token: str = "access.token.value") -> User:
    user = User(email="alice@example.com", user_id=user_id, name="Alice", roles=["user"])
    user.raw_token = raw_token
    return user


def _app(user: Optional[User] = None) -> FastAPI:
    app = FastAPI()
    app.include_router(proxy_routes.router)
    if user is not None:
        app.dependency_overrides[get_current_user_from_session] = lambda: user
    return app


@pytest.fixture(autouse=True)
def _clean_ledger(monkeypatch: pytest.MonkeyPatch):
    proxy_routes._prewarm_ledger.reset()
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", "true")
    monkeypatch.delenv("RUNTIME_SESSION_AFFINITY_ENABLED", raising=False)
    yield
    proxy_routes._prewarm_ledger.reset()


@pytest.fixture()
def upstream(monkeypatch: pytest.MonkeyPatch) -> List[httpx.Request]:
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"warmed": True})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(proxy_routes, "_build_prewarm_client", lambda: httpx.AsyncClient(transport=transport))
    return seen


def _drain() -> None:
    """Let the background forward finish (TestClient runs the app on its own loop)."""
    deadline = time.monotonic() + 2
    while proxy_routes._prewarm_tasks and time.monotonic() < deadline:
        time.sleep(0.01)


def test_requires_a_session_cookie() -> None:
    assert TestClient(_app()).post("/chat/prewarm", json={"session_id": "s1"}).status_code == 401


def test_404_while_the_flag_is_off(monkeypatch: pytest.MonkeyPatch, upstream) -> None:
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", "")
    resp = TestClient(_app(_user())).post("/chat/prewarm", json={"session_id": "s1"})
    assert resp.status_code == 404
    _drain()
    assert upstream == []


def test_forwards_a_warm_invocation_pinned_like_the_first_turn(upstream) -> None:
    resp = TestClient(_app(_user())).post("/chat/prewarm", json={"session_id": "conv-123"})
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    _drain()
    assert len(upstream) == 1
    request = upstream[0]
    assert request.url.path.endswith("/invocations")
    assert json.loads(request.content) == {"session_id": "conv-123", "warm": True}
    assert request.headers["Authorization"] == "Bearer access.token.value"
    # The exact runtime session the chat proxy pins the first real turn to.
    assert request.headers[RUNTIME_SESSION_ID_HEADER] == runtime_session_id_for("conv-123", "user-sub")


def test_the_same_conversation_is_warmed_once_per_window(upstream) -> None:
    client = TestClient(_app(_user()))
    assert client.post("/chat/prewarm", json={"session_id": "conv-1"}).json() == {"status": "accepted"}
    assert client.post("/chat/prewarm", json={"session_id": "conv-1"}).json() == {"status": "recently_warmed"}
    _drain()
    assert len(upstream) == 1


def test_rewarm_is_allowed_after_the_window() -> None:
    ledger = proxy_routes._PrewarmLedger()
    window = proxy_routes._PREWARM_REWARM_AFTER_SECONDS
    assert ledger.admit("u", "s", 1000.0) == "accepted"
    assert ledger.admit("u", "s", 1000.0 + window - 1) == "recently_warmed"
    assert ledger.admit("u", "s", 1000.0 + window + 1) == "accepted"


def test_the_rewarm_window_stays_inside_the_runtime_idle_timeout() -> None:
    # The Runtime reaps a session after 900 s idle; re-warming later than that
    # would let an open conversation go cold between warms.
    assert 0 < proxy_routes._PREWARM_REWARM_AFTER_SECONDS < 900


def test_per_user_rate_limit_does_not_touch_other_users() -> None:
    ledger = proxy_routes._PrewarmLedger()
    cap = proxy_routes._PREWARM_MAX_PER_USER_PER_MINUTE
    for i in range(cap):
        assert ledger.admit("busy", f"s{i}", 10.0) == "accepted"
    assert ledger.admit("busy", "one-more", 10.0) == "rate_limited"
    assert ledger.admit("other", "s0", 10.0) == "accepted"
    # The minute rolls over.
    assert ledger.admit("busy", "one-more", 71.0) == "accepted"


def test_two_users_on_one_session_id_are_warmed_separately(upstream) -> None:
    TestClient(_app(_user("alice"))).post("/chat/prewarm", json={"session_id": "shared"})
    TestClient(_app(_user("bob"))).post("/chat/prewarm", json={"session_id": "shared"})
    _drain()
    pins = {r.headers[RUNTIME_SESSION_ID_HEADER] for r in upstream}
    assert pins == {runtime_session_id_for("shared", "alice"), runtime_session_id_for("shared", "bob")}


def test_skips_when_runtime_session_affinity_is_off(monkeypatch: pytest.MonkeyPatch, upstream) -> None:
    # Without the affinity header a warm call would start a throwaway microVM.
    monkeypatch.setattr(proxy_routes, "runtime_session_affinity_enabled", lambda: False)
    resp = TestClient(_app(_user())).post("/chat/prewarm", json={"session_id": "conv-1"})
    assert resp.status_code == 202 and resp.json() == {"status": "skipped"}
    _drain()
    assert upstream == []


@pytest.mark.parametrize("bad", ["", "has space", "../etc", "x" * 129, "semi;colon"])
def test_rejects_a_malformed_session_id(bad: str, upstream) -> None:
    resp = TestClient(_app(_user())).post("/chat/prewarm", json={"session_id": bad})
    assert resp.status_code == 422
    assert upstream == []


def test_an_upstream_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    transport = httpx.MockTransport(boom)
    monkeypatch.setattr(proxy_routes, "_build_prewarm_client", lambda: httpx.AsyncClient(transport=transport))
    resp = TestClient(_app(_user())).post("/chat/prewarm", json={"session_id": "conv-1"})
    assert resp.status_code == 202
    _drain()
    assert any("prewarm failed" in r.getMessage() for r in caplog.records)


def test_forward_never_raises() -> None:
    async def run() -> None:
        original = proxy_routes._build_prewarm_client

        def broken():
            raise RuntimeError("client construction failed")

        proxy_routes._build_prewarm_client = broken
        try:
            await proxy_routes._forward_prewarm("s", "u", "t")
        finally:
            proxy_routes._build_prewarm_client = original

    asyncio.run(run())
