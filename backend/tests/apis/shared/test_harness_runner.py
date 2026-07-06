"""Tests for ``run_agent_headless`` — governance ordering, outcomes, delivery.

The runner is exercised end-to-end against an ``httpx.MockTransport``
standing in for the AgentCore Runtime data plane (via the
``_build_http_client`` seam), with a recording governance floor and spied
delivery functions. The key invariant pinned here is the F6a fail-closed
rule: **no audit record → no run** (no bearer mint, no HTTP).
"""

from __future__ import annotations

import httpx
import pytest

import apis.shared.sessions.metadata as sessions_metadata
from apis.shared.harness import runner as runner_module
from apis.shared.harness.auth import HeadlessAuthError, StaticBearerAuth
from apis.shared.harness.governance import GovernanceFloor
from apis.shared.harness.runner import build_invocations_url, run_agent_headless


class RecordingAudit:
    """Stands in for RunAuditRecorder; optionally fails the start write."""

    def __init__(self, *, fail_start: bool = False):
        self.fail_start = fail_start
        self.starts: list[dict] = []
        self.ends: list = []

    def record_start(self, **kwargs):
        if self.fail_start:
            raise RuntimeError("dynamo down")
        self.starts.append(kwargs)

    def record_end(self, *, result):
        self.ends.append(result)


class SpyBearerAuth(StaticBearerAuth):
    def __init__(self, token: str = "bearer-1"):
        super().__init__(token)
        self.minted_for: list[str] = []

    async def mint_bearer_for_user(self, user_id: str) -> str:
        self.minted_for.append(user_id)
        return await super().mint_bearer_for_user(user_id)


class FailingBearerAuth:
    async def mint_bearer_for_user(self, user_id: str) -> str:
        raise HeadlessAuthError("no grant")


def _sse(*events: tuple[str, str]) -> bytes:
    return "".join(f"event: {name}\ndata: {data}\n\n" for name, data in events).encode()


_HAPPY_STREAM = _sse(
    ("message_start", '{"role": "assistant"}'),
    ("content_block_delta", '{"contentBlockIndex": 0, "type": "text", "text": "pong"}'),
    ("message_stop", '{"stopReason": "end_turn"}'),
    ("metadata", '{"usage": {"inputTokens": 5, "outputTokens": 1, "totalTokens": 6}}'),
    ("done", "{}"),
)


@pytest.fixture
def delivery_spy(monkeypatch):
    """Spy the runner's delivery calls (imported lazily from sessions.metadata)."""
    calls = {"ensure": [], "title": []}

    async def fake_ensure(session_id, user_id):
        calls["ensure"].append((session_id, user_id))
        return True

    async def fake_title(session_id, user_id, title):
        calls["title"].append((session_id, user_id, title))

    monkeypatch.setattr(sessions_metadata, "ensure_session_metadata_exists", fake_ensure)
    monkeypatch.setattr(sessions_metadata, "update_session_title", fake_title)
    return calls


def _mock_http(monkeypatch, handler):
    def factory(timeout_seconds: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(runner_module, "_build_http_client", factory)


# ---------------------------------------------------------------------------
# F6a — audit fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_start_failure_fails_closed_before_any_spend(monkeypatch):
    """No audit record → no run: neither the bearer mint nor HTTP happens."""
    auth = SpyBearerAuth()

    def no_http(timeout_seconds):  # any HTTP attempt is a test failure
        raise AssertionError("HTTP client built despite failed audit")

    monkeypatch.setattr(runner_module, "_build_http_client", no_http)

    with pytest.raises(RuntimeError, match="dynamo down"):
        await run_agent_headless(
            user_id="user-1",
            prompt="hi",
            auth=auth,
            governance=GovernanceFloor(audit=RecordingAudit(fail_start=True)),
        )

    assert auth.minted_for == []


@pytest.mark.asyncio
async def test_auth_failure_still_writes_the_end_audit_record(monkeypatch, delivery_spy):
    audit = RecordingAudit()

    with pytest.raises(HeadlessAuthError):
        await run_agent_headless(
            user_id="user-1",
            prompt="hi",
            auth=FailingBearerAuth(),
            governance=GovernanceFloor(audit=audit),
        )

    assert len(audit.starts) == 1
    (end,) = audit.ends
    assert end.status == "error"
    assert "auth" in (end.error or "")


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_completes_and_delivers(monkeypatch, delivery_spy):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_HAPPY_STREAM,
        )

    _mock_http(monkeypatch, handler)
    audit = RecordingAudit()

    result = await run_agent_headless(
        user_id="user-1",
        prompt="ping",
        auth=SpyBearerAuth("bearer-xyz"),
        title="My Briefing",
        trigger="run_now",
        invocations_base_url="http://localhost:8001",
        governance=GovernanceFloor(audit=audit),
    )

    assert result.status == "completed"
    assert result.final_message == "pong"
    assert result.title == "My Briefing"
    assert result.usage["usage"]["totalTokens"] == 6
    assert seen["auth"] == "Bearer bearer-xyz"
    assert seen["url"] == "http://localhost:8001/invocations"

    # Audit trail: start before, end after, same run id.
    assert audit.starts[0]["run_id"] == result.run_id
    assert audit.starts[0]["trigger"] == "run_now"
    assert audit.ends[0].status == "completed"

    # Delivery: idempotent session ensure + explicit title override.
    assert delivery_spy["ensure"] == [(result.session_id, "user-1")]
    assert delivery_spy["title"] == [(result.session_id, "user-1", "My Briefing")]


@pytest.mark.asyncio
async def test_http_error_from_the_gateway_is_an_error_result(monkeypatch, delivery_spy):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "OAuth authorization failed"})

    _mock_http(monkeypatch, handler)

    result = await run_agent_headless(
        user_id="user-1",
        prompt="ping",
        auth=StaticBearerAuth("t"),
        invocations_base_url="http://localhost:8001",
        governance=GovernanceFloor(audit=RecordingAudit()),
    )

    assert result.status == "error"
    assert "HTTP 403" in (result.error or "")


@pytest.mark.asyncio
async def test_stream_without_done_event_is_an_error(monkeypatch, delivery_spy):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(("message_start", '{"role": "assistant"}')),
        )

    _mock_http(monkeypatch, handler)

    result = await run_agent_headless(
        user_id="user-1",
        prompt="ping",
        auth=StaticBearerAuth("t"),
        invocations_base_url="http://localhost:8001",
        governance=GovernanceFloor(audit=RecordingAudit()),
    )

    assert result.status == "error"
    assert "without a done event" in (result.error or "")


@pytest.mark.asyncio
async def test_timeout_surfaces_as_timeout_status(monkeypatch, delivery_spy):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    _mock_http(monkeypatch, handler)

    result = await run_agent_headless(
        user_id="user-1",
        prompt="ping",
        auth=StaticBearerAuth("t"),
        invocations_base_url="http://localhost:8001",
        timeout_seconds=1.0,
        governance=GovernanceFloor(audit=RecordingAudit()),
    )

    assert result.status == "timeout"


# ---------------------------------------------------------------------------
# build_invocations_url — the single shared resolver (chat proxy imports it)
# ---------------------------------------------------------------------------


def test_build_invocations_url_encodes_the_runtime_arn():
    base = (
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/my-runtime"
    )
    url = build_invocations_url(base)
    assert url == (
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
        "arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A123456789012%3Aruntime%2F"
        "my-runtime/invocations?qualifier=DEFAULT"
    )


def test_build_invocations_url_local_passthrough():
    assert (
        build_invocations_url("http://localhost:8001")
        == "http://localhost:8001/invocations"
    )


def test_chat_proxy_uses_the_shared_resolver():
    from apis.app_api.chat import proxy_routes

    assert proxy_routes._build_invocations_url is build_invocations_url
