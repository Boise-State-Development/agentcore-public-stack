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


# ---------------------------------------------------------------------------
# Prepare: a warm that also builds the turn's agent (SESSION_PREWARM_PREPARE_ENABLED)
# ---------------------------------------------------------------------------

_SELECTION = {
    "model_id": "us.anthropic.claude-haiku-4-5",
    "temperature": 0.2,
    "caching_enabled": True,
    "enabled_tools": ["web_search"],
    "provider": "bedrock",
    "max_tokens": 2048,
    "inference_params": {"top_p": 0.9},
    "rag_assistant_id": "ast-1",
    "agent_type": "chat",
    "enabled_skills": ["skill-a"],
    "selected_prompt_id": "p1",
    "incognito": False,
    "agent_mention": False,
    "system_prompt": "be brief",
}


@pytest.fixture()
def prepare_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_PREWARM_PREPARE_ENABLED", "true")


@pytest.fixture()
def prepare_upstream(monkeypatch: pytest.MonkeyPatch) -> List[httpx.Request]:
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"warmed": True, "prepared": True, "reason": None, "ms": 40})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(proxy_routes, "_build_prewarm_client", lambda: httpx.AsyncClient(transport=transport))
    return seen


def test_flag_off_drops_prepare_and_forwards_the_plain_warm(monkeypatch, upstream) -> None:
    monkeypatch.delenv("SESSION_PREWARM_PREPARE_ENABLED", raising=False)
    resp = TestClient(_app(_user())).post(
        "/chat/prewarm", json={"session_id": "conv-1", "prepare": True, **_SELECTION}
    )
    assert resp.json() == {"status": "accepted"}
    _drain()
    assert json.loads(upstream[0].content) == {"session_id": "conv-1", "warm": True}


def test_prepare_absent_or_false_is_a_plain_warm_even_with_the_flag_on(prepare_on, upstream) -> None:
    client = TestClient(_app(_user()))
    client.post("/chat/prewarm", json={"session_id": "a", "prepare": False, **_SELECTION})
    client.post("/chat/prewarm", json={"session_id": "b", **_SELECTION})
    _drain()
    assert [json.loads(r.content) for r in upstream] == [
        {"session_id": "a", "warm": True},
        {"session_id": "b", "warm": True},
    ]


def test_prepare_needs_session_prewarm_on(monkeypatch, prepare_on, upstream) -> None:
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", "")
    resp = TestClient(_app(_user())).post("/chat/prewarm", json={"session_id": "s", "prepare": True})
    assert resp.status_code == 404


def test_prepare_forwards_only_the_allowlist(prepare_on, prepare_upstream) -> None:
    forbidden = {
        "message": "hello",
        "files": [{"filename": "a.txt"}],
        "file_upload_ids": ["u1"],
        "folder_attachment_ids": ["f1"],
        "interrupt_responses": [{"interruptId": "i", "response": {}}],
        "continue_truncated": True,
        "client_tools": [{"name": "x"}],
        "run_mode": "watch",
        "handoff_context": {"x": 1},
    }
    resp = TestClient(_app(_user())).post(
        "/chat/prewarm",
        json={"session_id": "conv-1", "prepare": True, **_SELECTION, **forbidden},
        headers={"OAuth2CallbackUrl": "https://app.example/oauth-complete"},
    )
    assert resp.json() == {"status": "accepted", "prepareArm": "treated"}
    _drain()
    assert len(prepare_upstream) == 1
    sent = json.loads(prepare_upstream[0].content)
    assert sent == {"session_id": "conv-1", "warm": True, "prepare": True, **_SELECTION}
    assert not set(forbidden) & set(sent)
    # The same header set a chat turn carries.
    assert prepare_upstream[0].headers["OAuth2CallbackUrl"] == "https://app.example/oauth-complete"
    assert prepare_upstream[0].headers["Authorization"] == "Bearer access.token.value"
    assert prepare_upstream[0].headers[RUNTIME_SESSION_ID_HEADER] == runtime_session_id_for("conv-1", "user-sub")


def test_prepare_omits_null_selection_fields(prepare_on, prepare_upstream) -> None:
    TestClient(_app(_user())).post(
        "/chat/prewarm", json={"session_id": "c", "prepare": True, "model_id": "m", "temperature": None}
    )
    _drain()
    assert json.loads(prepare_upstream[0].content) == {
        "session_id": "c",
        "warm": True,
        "prepare": True,
        "model_id": "m",
    }


def test_a_plain_warm_does_not_carry_the_callback_header(prepare_upstream) -> None:
    TestClient(_app(_user())).post(
        "/chat/prewarm", json={"session_id": "c"}, headers={"OAuth2CallbackUrl": "https://x/y"}
    )
    _drain()
    assert "OAuth2CallbackUrl" not in prepare_upstream[0].headers


def test_warm_then_prepare_is_forwarded(prepare_on, prepare_upstream) -> None:
    client = TestClient(_app(_user()))
    assert client.post("/chat/prewarm", json={"session_id": "c"}).json() == {"status": "accepted"}
    assert client.post("/chat/prewarm", json={"session_id": "c", "prepare": True}).json() == {"status": "accepted", "prepareArm": "treated"}
    _drain()
    assert [json.loads(r.content).get("prepare") for r in prepare_upstream] == [None, True]


def test_prepare_then_prepare_is_recently_warmed(prepare_on, prepare_upstream) -> None:
    client = TestClient(_app(_user()))
    assert client.post("/chat/prewarm", json={"session_id": "c", "prepare": True}).json() == {"status": "accepted", "prepareArm": "treated"}
    again = client.post("/chat/prewarm", json={"session_id": "c", "prepare": True}).json()
    assert again == {"status": "recently_warmed", "prepareArm": "treated"}
    _drain()
    assert len(prepare_upstream) == 1


def test_prepare_then_warm_is_recently_warmed(prepare_on, prepare_upstream) -> None:
    client = TestClient(_app(_user()))
    assert client.post("/chat/prewarm", json={"session_id": "c", "prepare": True}).json() == {"status": "accepted", "prepareArm": "treated"}
    assert client.post("/chat/prewarm", json={"session_id": "c"}).json() == {"status": "recently_warmed"}


def test_ledger_stages_stay_per_user_and_conversation_and_share_the_rate_limit() -> None:
    ledger = proxy_routes._PrewarmLedger()
    assert ledger.admit("u", "s", 10.0) == "accepted"
    assert ledger.admit("u", "s", 11.0, prepare=True) == "accepted"
    assert ledger.admit("u", "s", 12.0, prepare=True) == "recently_warmed"
    assert ledger.admit("u", "s", 12.0) == "recently_warmed"
    assert ledger.admit("other", "s", 12.0, prepare=True) == "accepted"
    assert ledger.admit("u", "t", 12.0, prepare=True) == "accepted"
    window = proxy_routes._PREWARM_REWARM_AFTER_SECONDS
    assert ledger.admit("u", "s", 11.0 + window + 1, prepare=True) == "accepted"
    # Prepares count against the same per-user rate limit.
    busy = proxy_routes._PrewarmLedger()
    for i in range(proxy_routes._PREWARM_MAX_PER_USER_PER_MINUTE):
        assert busy.admit("b", f"s{i}", 5.0, prepare=bool(i % 2)) == "accepted"
    assert busy.admit("b", "x", 5.0, prepare=True) == "rate_limited"


def test_prepare_log_line_appends_the_outcome(prepare_on, prepare_upstream, caplog) -> None:
    caplog.set_level("INFO", logger=proxy_routes.logger.name)
    TestClient(_app(_user())).post("/chat/prewarm", json={"session_id": "c", "prepare": True, "model_id": "secret-m"})
    _drain()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("prewarm forwarded")]
    assert len(lines) == 1
    assert lines[0].endswith(" prepare=true prepared=true reason=-")
    assert " in " in lines[0] and "secret-m" not in lines[0] and "user-sub" not in lines[0]


def test_prepare_log_line_reads_a_refusal_and_survives_non_json(prepare_on, monkeypatch, caplog) -> None:
    caplog.set_level("INFO", logger=proxy_routes.logger.name)
    answers = iter(
        [
            httpx.Response(200, json={"warmed": True, "prepared": False, "reason": "no_model"}),
            httpx.Response(502, text="bad gateway"),
        ]
    )
    transport = httpx.MockTransport(lambda request: next(answers))
    monkeypatch.setattr(proxy_routes, "_build_prewarm_client", lambda: httpx.AsyncClient(transport=transport))
    client = TestClient(_app(_user()))
    client.post("/chat/prewarm", json={"session_id": "a", "prepare": True})
    _drain()
    client.post("/chat/prewarm", json={"session_id": "b", "prepare": True})
    _drain()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("prewarm forwarded")]
    assert lines[0].endswith(" prepare=true prepared=false reason=no_model")
    assert lines[1].endswith(" prepare=true prepared=unknown reason=-")


def test_the_plain_warm_log_line_is_unchanged(upstream, caplog) -> None:
    caplog.set_level("INFO", logger=proxy_routes.logger.name)
    TestClient(_app(_user())).post("/chat/prewarm", json={"session_id": "c"})
    _drain()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("prewarm forwarded")]
    assert len(lines) == 1 and "prepare" not in lines[0]
    assert lines[0].startswith("prewarm forwarded -> 200 in ") and lines[0].endswith(" ms")


def test_the_prepare_timeout_is_between_30_and_60_seconds() -> None:
    assert 30.0 <= proxy_routes._PREWARM_TIMEOUT_SECONDS <= 60.0


# ---------------------------------------------------------------------------
# Randomized holdout (SESSION_PREWARM_PREPARE_HOLDOUT_PERCENT)
# ---------------------------------------------------------------------------


@pytest.fixture()
def all_holdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_PREWARM_PREPARE_HOLDOUT_PERCENT", "100")


def test_holdout_forwards_a_plain_warm_that_names_its_arm(prepare_on, all_holdout, prepare_upstream) -> None:
    resp = TestClient(_app(_user())).post(
        "/chat/prewarm",
        json={"session_id": "conv-1", "prepare": True, **_SELECTION},
        headers={"OAuth2CallbackUrl": "https://app.example/oauth-complete"},
    )
    assert resp.json() == {"status": "accepted", "prepareArm": "holdout"}
    _drain()
    (request,) = prepare_upstream
    assert json.loads(request.content) == {"session_id": "conv-1", "warm": True, "prepare_arm": "holdout"}
    assert "OAuth2CallbackUrl" not in request.headers


def test_treated_reports_its_arm_in_every_outcome(prepare_on, prepare_upstream) -> None:
    client = TestClient(_app(_user()))
    first = client.post("/chat/prewarm", json={"session_id": "c", "prepare": True}).json()
    again = client.post("/chat/prewarm", json={"session_id": "c", "prepare": True}).json()
    assert first == {"status": "accepted", "prepareArm": "treated"}
    assert again == {"status": "recently_warmed", "prepareArm": "treated"}
    cap = proxy_routes._PREWARM_MAX_PER_USER_PER_MINUTE
    for i in range(cap):
        client.post("/chat/prewarm", json={"session_id": f"x{i}", "prepare": True})
    limited = client.post("/chat/prewarm", json={"session_id": "over", "prepare": True}).json()
    assert limited == {"status": "rate_limited", "prepareArm": "treated"}


def test_holdout_repeats_dedupe_on_the_prepare_stage(prepare_on, all_holdout, prepare_upstream) -> None:
    client = TestClient(_app(_user()))
    assert client.post("/chat/prewarm", json={"session_id": "c", "prepare": True}).json()["status"] == "accepted"
    again = client.post("/chat/prewarm", json={"session_id": "c", "prepare": True}).json()
    assert again == {"status": "recently_warmed", "prepareArm": "holdout"}
    # A plain warm earlier does not suppress a holdout's prepare-stage warm.
    other = TestClient(_app(_user("bob")))
    assert other.post("/chat/prewarm", json={"session_id": "d"}).json() == {"status": "accepted"}
    assert other.post("/chat/prewarm", json={"session_id": "d", "prepare": True}).json()["status"] == "accepted"
    _drain()
    assert len(prepare_upstream) == 3


def test_no_arm_without_a_prepare_or_with_the_flag_off(monkeypatch, all_holdout, upstream) -> None:
    client = TestClient(_app(_user()))
    monkeypatch.setenv("SESSION_PREWARM_PREPARE_ENABLED", "true")
    assert client.post("/chat/prewarm", json={"session_id": "a"}).json() == {"status": "accepted"}
    assert client.post("/chat/prewarm", json={"session_id": "b", "prepare": False}).json() == {"status": "accepted"}
    monkeypatch.delenv("SESSION_PREWARM_PREPARE_ENABLED")
    assert client.post("/chat/prewarm", json={"session_id": "c", "prepare": True}).json() == {"status": "accepted"}
    _drain()
    assert all("prepare_arm" not in json.loads(r.content) for r in upstream)


def test_the_arm_comes_from_the_conversation_id_not_the_affinity_id(prepare_on, monkeypatch, prepare_upstream) -> None:
    seen = []
    real = proxy_routes.prepare_arm

    def spy(session_id, percent):
        seen.append(session_id)
        return real(session_id, percent)

    monkeypatch.setattr(proxy_routes, "prepare_arm", spy)
    TestClient(_app(_user())).post(
        "/chat/prewarm", json={"session_id": "child", "affinity_session_id": "root", "prepare": True}
    )
    assert seen == ["child"]


def test_holdout_log_line_appends_the_arm(prepare_on, all_holdout, prepare_upstream, caplog) -> None:
    caplog.set_level("INFO", logger=proxy_routes.logger.name)
    TestClient(_app(_user())).post("/chat/prewarm", json={"session_id": "c", "prepare": True})
    _drain()
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("prewarm forwarded")]
    assert line.startswith("prewarm forwarded -> 200 in ") and line.endswith(" ms prepare=holdout")
