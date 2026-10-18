"""Tests for app-initiated tools/call dispatch (MCP Apps PR #5).

Mocks the boundary the way the PR #3 tests do: a fake MCP client +
`UIToolCatalog`, no live agent. Asserts the spec-MUST app-visibility gate
at the inference-api dispatch, and that a successful call publishes
synthesized tool_use/tool_result into the per-session broker.
"""

import contextlib

import pytest

from apis.inference_api.chat import app_tool_dispatch as dispatch_mod
from apis.inference_api.chat.app_tool_dispatch import (
    AppToolCallError,
    dispatch_app_tool_call,
)
from apis.shared.mcp_apps.broker import get_app_tool_event_broker
from apis.shared.tools.models import ToolUIMetadata
from agents.main_agent.integrations import mcp_apps as mcp_apps_mod


class _FakeContent:
    def __init__(self, text: str) -> None:
        self._text = text

    def model_dump(self, **_: object) -> dict:
        return {"type": "text", "text": self._text}


class _FakeResult:
    def __init__(self, text: str = "ok", is_error: bool = False) -> None:
        self.content = [_FakeContent(text)]
        self.isError = is_error


class _FakeClient:
    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self._result = result if result is not None else _FakeResult()
        self._raises = raises
        self.calls: list = []

    def call_tool_sync(self, tool_use_id, name, arguments=None):
        self.calls.append((tool_use_id, name, arguments))
        if self._raises is not None:
            raise self._raises
        return self._result


class _FakeCatalog:
    def __init__(self, meta=None, client=None) -> None:
        self._meta = meta
        self._client = client

    def get(self, _name):
        return self._meta

    def get_client(self, _name):
        return self._client


def _patch(monkeypatch, *, enabled=True, meta=None, client=None):
    monkeypatch.setattr(
        mcp_apps_mod, "is_mcp_apps_host_enabled", lambda: enabled
    )
    monkeypatch.setattr(
        mcp_apps_mod,
        "get_ui_tool_catalog",
        lambda: _FakeCatalog(meta=meta, client=client),
    )


def _ui(visibility):
    return ToolUIMetadata(resource_uri="ui://srv/w", visibility=visibility)


async def _call(session_id="disp-s1", tool_name="widget_tool"):
    return await dispatch_app_tool_call(
        agent=None,
        session_id=session_id,
        user_id="u1",
        tool_use_id="tu-1",
        tool_name=tool_name,
        arguments={"q": "x"},
    )


@pytest.mark.asyncio
async def test_rejects_when_host_flag_disabled(monkeypatch):
    _patch(monkeypatch, enabled=False)
    with pytest.raises(AppToolCallError) as ei:
        await _call()
    assert ei.value.code == 403


@pytest.mark.asyncio
async def test_rejects_unknown_tool(monkeypatch):
    # e.g. GitHub's issue_write App calling ui_get against `/mcp/x/issues`,
    # which never lists it. The message names the tool and says it isn't
    # offered, not that its visibility forbids the App.
    _patch(monkeypatch, enabled=True, meta=None, client=_FakeClient())
    with pytest.raises(AppToolCallError) as ei:
        await _call(tool_name="ui_get")
    assert ei.value.code == 403
    assert ei.value.message == (
        "Tool 'ui_get' is not offered by this conversation's MCP servers"
    )


@pytest.mark.asyncio
async def test_rejects_tool_not_app_visible(monkeypatch):
    # visibility=["model"] → callable by the model, NOT by an app.
    _patch(
        monkeypatch,
        enabled=True,
        meta=_ui(["model"]),
        client=_FakeClient(),
    )
    with pytest.raises(AppToolCallError) as ei:
        await _call()
    assert ei.value.code == 403
    assert ei.value.message == (
        "Tool 'widget_tool' is not callable from an MCP App"
    )


@pytest.mark.asyncio
async def test_rejects_when_no_live_client(monkeypatch):
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=None)
    with pytest.raises(AppToolCallError) as ei:
        await _call()
    assert ei.value.code == 409


@pytest.mark.asyncio
async def test_dispatch_failure_maps_to_502(monkeypatch):
    _patch(
        monkeypatch,
        enabled=True,
        meta=_ui(["app"]),
        client=_FakeClient(raises=RuntimeError("boom")),
    )
    with pytest.raises(AppToolCallError) as ei:
        await _call()
    assert ei.value.code == 502


@pytest.mark.asyncio
async def test_success_returns_result_and_publishes_thread_events(monkeypatch):
    client = _FakeClient(_FakeResult("hello"))
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)

    broker = get_app_tool_event_broker()
    q = broker.add_subscriber("disp-ok")
    try:
        payload = await dispatch_app_tool_call(
            agent=None,
            session_id="disp-ok",
            user_id="u1",
            tool_use_id="tu-9",
            tool_name="widget_tool",
            arguments={"q": "x"},
        )
    finally:
        events = broker.drain(q)
        broker.remove_subscriber("disp-ok", q)

    assert payload["toolUseId"] == "tu-9"
    assert payload["result"]["isError"] is False
    assert payload["result"]["content"] == [{"type": "text", "text": "hello"}]
    # The MCP client was called with a synthesized (distinct) id.
    assert client.calls[0][1] == "widget_tool"
    assert client.calls[0][0] != "tu-9"
    # Both thread events were published, tool_use before tool_result.
    types = [e["type"] for e in events]
    assert types == ["tool_use", "tool_result"]
    assert events[0]["data"]["tool_use"]["name"] == "widget_tool"
    assert events[0]["data"]["tool_use"]["origin"] == "mcp_app"
    assert events[1]["data"]["tool_result"]["status"] == "success"


@pytest.mark.asyncio
async def test_dict_shaped_result_keeps_its_content(monkeypatch):
    """A dict-shaped tool result must round-trip its content blocks.

    Strands' `MCPToolResult` extends `ToolResult`, a TypedDict — so what
    `call_tool_sync` returns is a plain dict at runtime, and the `getattr`
    lookup in `_serialize_content` finds nothing on it. That regression sent
    `content: []` back for every app-initiated tools/call, leaving embedded
    Apps with no data to render while every layer still reported 200/success.

    The other fakes in this module are objects with a `.content` attribute,
    which is why the attribute path alone looked correct.
    """
    result = {
        "toolUseId": "mcp-1",
        "status": "success",
        # Strands content blocks are untagged: `text`/`json`, no `type`.
        "content": [{"text": '{"lists": []}'}],
    }
    client = _FakeClient(result)
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)

    payload = await _call(session_id="disp-dict")

    assert payload["result"]["content"] == [{"type": "text", "text": '{"lists": []}'}]
    assert payload["result"]["isError"] is False


@pytest.mark.asyncio
async def test_app_receives_tagged_mcp_content(monkeypatch):
    """The App gets an MCP `CallToolResult`, not Strands' untagged blocks.

    GitHub's issue_write App finds its data with `c.type === "text"`; with
    Strands' `{"text": ...}` passed through, every picker it fills (labels,
    assignees, milestones) rendered "No labels available" although the
    `ui_get` call succeeded. The thread event keeps the Strands shape.
    """
    result = {
        "toolUseId": "mcp-1",
        "status": "success",
        "content": [
            {"text": '{"labels": [{"name": "bug"}]}'},
            {"json": {"count": 1}},
            {"image": {"format": "png", "source": {"bytes": b"\x89PNG"}}},
            {"type": "text", "text": "already tagged"},
        ],
        "structuredContent": {"labels": [{"name": "bug"}]},
    }
    client = _FakeClient(result)
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)

    broker = get_app_tool_event_broker()
    q = broker.add_subscriber("disp-tagged")
    try:
        payload = await _call(session_id="disp-tagged")
        events = broker.drain(q)
    finally:
        broker.remove_subscriber("disp-tagged", q)

    assert payload["result"]["content"] == [
        {"type": "text", "text": '{"labels": [{"name": "bug"}]}'},
        {"type": "text", "text": '{"count": 1}'},
        {"type": "image", "data": "iVBORw==", "mimeType": "image/png"},
        {"type": "text", "text": "already tagged"},
    ]
    assert payload["result"]["structuredContent"] == {"labels": [{"name": "bug"}]}
    thread_result = events[1]["data"]["tool_result"]
    assert thread_result["content"][0] == {"text": '{"labels": [{"name": "bug"}]}'}


@pytest.mark.asyncio
async def test_no_structured_content_key_when_the_tool_returned_none(monkeypatch):
    client = _FakeClient({"status": "success", "content": [{"text": "ok"}]})
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)

    payload = await _call(session_id="disp-nostruct")

    assert "structuredContent" not in payload["result"]


@pytest.mark.asyncio
async def test_strands_error_status_reaches_the_app_as_is_error(monkeypatch):
    """`status: "error"` without `isError` is still an error.

    That is the shape `call_tool_sync` returns when it catches an exception
    itself. Read for `isError` alone, the App got `isError: false` beside
    "Tool execution failed: ..." — found on dev with GitHub's issue_write.
    """
    client = _FakeClient(
        {
            "toolUseId": "mcp-1",
            "status": "error",
            "content": [{"text": "Tool execution failed: boom"}],
        }
    )
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)

    broker = get_app_tool_event_broker()
    q = broker.add_subscriber("disp-status")
    try:
        payload = await _call(session_id="disp-status")
        events = broker.drain(q)
    finally:
        broker.remove_subscriber("disp-status", q)

    assert payload["result"]["isError"] is True
    assert payload["result"]["content"] == [
        {"type": "text", "text": "Tool execution failed: boom"}
    ]
    assert events[1]["data"]["tool_result"]["status"] == "error"


def test_is_error_reads_every_failure_marker():
    assert dispatch_mod._is_error({"isError": True}) is True
    assert dispatch_mod._is_error({"status": "error"}) is True
    assert dispatch_mod._is_error({"status": "success"}) is False
    assert dispatch_mod._is_error(_FakeResult(is_error=True)) is True
    assert dispatch_mod._is_error(_FakeResult(is_error=False)) is False


class _SessionClient(_FakeClient):
    """Client that tracks its MCP session the way Strands' MCPClient does.

    `start()` raises if the session is already running, matching upstream, so
    a test fails loudly if the dispatch tries to revive a live session.
    """

    def __init__(self, active: bool, result=None) -> None:
        super().__init__(result)
        self.active = active
        self.starts = 0
        self.stops = 0
        self.active_during_call: bool | None = None

    def _is_session_active(self) -> bool:
        return self.active

    def start(self):
        if self.active:
            raise AssertionError("start() on an already-running session")
        self.active = True
        self.starts += 1
        return self

    def stop(self, exc_type, exc_val, exc_tb) -> None:
        self.active = False
        self.stops += 1

    def call_tool_sync(self, tool_use_id, name, arguments=None):
        self.active_during_call = self.active
        return super().call_tool_sync(tool_use_id, name, arguments)


@pytest.mark.asyncio
async def test_revives_a_torn_down_client_session(monkeypatch):
    """An app call between turns must reconnect rather than 502.

    The agent is served from cache, so Strands has already torn down its MCP
    client sessions; the catalog still holds the client. Calling straight
    through raised MCPClientInitializationError, which surfaced to the App as
    a 502 Bad Gateway.
    """
    client = _SessionClient(active=False)
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)

    payload = await _call(session_id="disp-revive")

    assert payload["result"]["isError"] is False
    assert client.starts == 1
    assert client.active_during_call is True
    # Restored to how we found it — the revival is scoped to this one call.
    assert client.stops == 1
    assert client.active is False


@pytest.mark.asyncio
async def test_leaves_a_live_client_session_alone(monkeypatch):
    """Mid-stream the session belongs to the running turn — don't touch it."""
    client = _SessionClient(active=True)
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)

    await _call(session_id="disp-live")

    assert client.starts == 0
    assert client.stops == 0
    assert client.active is True


class _FlakyMCPServer:
    """A real streamable-HTTP MCP server whose `tools/call` can 503.

    With `fail_calls` set it runs the tool and then answers HTTP 503 — what
    GitHub's MCP endpoint did on dev 2026-10-18: the issue was created, the
    POST came back 503. The MCP SDK treats any non-2xx POST as fatal for the
    session, so the in-flight call surfaces from Strands as "Connection to
    the MCP server was closed".
    """

    def __init__(self) -> None:
        import socket
        import threading

        import uvicorn
        from mcp.server.fastmcp import FastMCP

        self.writes: list = []
        self.fail_calls = True
        server = FastMCP("flaky", stateless_http=True, json_response=True)

        @server.tool()
        def issue_write(title: str) -> str:
            self.writes.append(title)
            return f"created {title}"

        inner = server.streamable_http_app()

        async def app(scope, receive, send):
            if scope["type"] != "http":
                return await inner(scope, receive, send)
            messages = []
            body = b""
            while True:
                message = await receive()
                messages.append(message)
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            replay_iter = iter(messages)

            async def replay():
                return next(replay_iter, {"type": "http.disconnect"})

            if not (self.fail_calls and b'"tools/call"' in body):
                return await inner(scope, replay, send)

            async def discard(_message):
                pass

            await inner(scope, replay, discard)  # the write happens...
            await send(  # ...and the answer is a 503
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"unavailable"})

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        port = sock.getsockname()[1]
        sock.close()
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        import time

        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("MCP test server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *_exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def client(self):
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp import MCPClient

        return MCPClient(lambda: streamablehttp_client(self.url))


@pytest.mark.asyncio
async def test_a_connection_lost_mid_call_reports_an_unknown_outcome(monkeypatch):
    """A write the server performed must not come back as a plain failure.

    The dev sequence: an agent's tool provider owns a live session, the App
    presses Create issue, GitHub creates it and answers 503. The App was told
    `isError: false` with "Tool execution failed: Connection to the MCP server
    was closed", and the shared client was left dead — the next press 502'd
    and every agent built over it got the dead session's tool list.
    """
    monkeypatch.setattr(dispatch_mod, "_schedule_ui_resource_refresh", lambda *a: None)
    with _FlakyMCPServer() as server:
        client = server.client()
        # As the agent build does: the tool provider starts the session.
        await client.load_tools()
        client.add_consumer("agent-registry")
        _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
        try:
            payload = await dispatch_app_tool_call(
                agent=None,
                session_id="disp-lost",
                user_id="u1",
                tool_use_id="tu-1",
                tool_name="issue_write",
                arguments={"title": "first"},
            )

            assert server.writes == ["first"]  # performed once, never retried
            assert payload["result"]["isError"] is True
            text = payload["result"]["content"][0]["text"]
            assert "may have completed" in text
            assert "issue_write" in text

            # The crashed session was reset, so the client reconnects: the
            # next press works...
            server.fail_calls = False
            again = await dispatch_app_tool_call(
                agent=None,
                session_id="disp-lost",
                user_id="u1",
                tool_use_id="tu-1",
                tool_name="issue_write",
                arguments={"title": "second"},
            )
            assert again["result"]["isError"] is False
            assert server.writes == ["first", "second"]

            # ...and an agent built over the client next gets a live session
            # rather than the dead one's cached tool list.
            await client.load_tools()
            result = client.call_tool_sync("t-3", "issue_write", {"title": "third"})
            assert result["status"] == "success"
        finally:
            with contextlib.suppress(Exception):
                client.stop(None, None, None)


@pytest.mark.asyncio
async def test_a_crashed_session_is_reset_before_it_is_revived(monkeypatch):
    """`start()` on a crashed client returns before any handshake.

    Its init future is still the resolved one from the dead session, so a
    plain revive "succeeded" and the call then failed on a session that was
    not running.
    """
    monkeypatch.setattr(dispatch_mod, "_schedule_ui_resource_refresh", lambda *a: None)
    with _FlakyMCPServer() as server:
        client = server.client()
        client.start()
        # Crash it outside the dispatch, the way a model-driven call would.
        client.call_tool_sync("t-1", "issue_write", {"title": "model"})
        assert dispatch_mod._session_crashed(client)

        server.fail_calls = False
        _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
        try:
            payload = await dispatch_app_tool_call(
                agent=None,
                session_id="disp-crashed",
                user_id="u1",
                tool_use_id="tu-1",
                tool_name="issue_write",
                arguments={"title": "app"},
            )
            assert payload["result"]["isError"] is False
            assert server.writes == ["model", "app"]
        finally:
            with contextlib.suppress(Exception):
                client.stop(None, None, None)


class _FakeIntegration:
    """Stands in for the process-wide `ExternalMCPIntegration` singleton."""

    def __init__(self, provider_id=None) -> None:
        self._provider_id = provider_id

    def provider_for_client(self, _client):
        return self._provider_id


class _FakeDisconnectRepo:
    def __init__(self, disconnected: bool = False) -> None:
        self._disconnected = disconnected
        self.calls: list = []

    async def is_disconnected(self, user_id, provider_id) -> bool:
        self.calls.append((user_id, provider_id))
        return self._disconnected


def _patch_oauth(
    monkeypatch,
    *,
    provider_id="google-tasks",
    resolved=None,
    disconnected=False,
):
    """Wire the OAuth collaborators `_ensure_oauth_token` reaches for.

    Each is imported lazily inside the dispatch module, so patching the
    owning module's attribute is what the call actually resolves.
    """
    from agents.main_agent.integrations import external_mcp_client
    from apis.shared.oauth import disconnect_repository, token_resolution

    monkeypatch.setattr(
        external_mcp_client,
        "get_external_mcp_integration",
        lambda: _FakeIntegration(provider_id),
    )
    repo = _FakeDisconnectRepo(disconnected)
    monkeypatch.setattr(
        disconnect_repository, "get_disconnect_repository", lambda: repo
    )

    calls: list = []

    async def _resolve(pid, uid, *, force_authentication=False):
        calls.append((pid, uid, force_authentication))
        return resolved

    monkeypatch.setattr(token_resolution, "resolve_token_or_consent_url", _resolve)
    return calls


@pytest.fixture
def token_cache():
    """The OAuth token cache is process-global — isolate each test."""
    from agents.main_agent.integrations import oauth_token_cache

    oauth_token_cache.clear_user("u1")
    yield oauth_token_cache
    oauth_token_cache.clear_user("u1")


@pytest.mark.asyncio
async def test_warms_a_cold_token_cache_from_the_vault(monkeypatch, token_cache):
    """The reported bug: an App's tool calls fail after a page reload.

    `OAuthConsentHook` warms the token cache on `BeforeToolCallEvent` — an
    event this path never raises. On any container that has not run a
    model-driven turn for this (user, provider) the cache is cold, the lazy
    token provider returns None, and the call goes out with no Authorization
    header. A server that allows an unauthenticated `tools/list` still lists
    the tool, so the App renders and then every button answers with the
    server's own "you aren't connected" text.
    """
    client = _FakeClient()
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
    calls = _patch_oauth(monkeypatch, resolved={"token": "tok-1", "url": None})

    payload = await _call(session_id="disp-oauth-cold")

    assert payload["result"]["isError"] is False
    # Resolved before the tool ran, so the request carries a Bearer token.
    assert calls == [("google-tasks", "u1", False)]
    assert token_cache.get("u1", "google-tasks") == "tok-1"


@pytest.mark.asyncio
async def test_warm_cache_skips_the_vault(monkeypatch, token_cache):
    client = _FakeClient()
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
    calls = _patch_oauth(monkeypatch, resolved={"token": "fresh", "url": None})
    token_cache.set("u1", "google-tasks", "already-warm")

    await _call(session_id="disp-oauth-warm")

    assert calls == []
    assert token_cache.get("u1", "google-tasks") == "already-warm"


@pytest.mark.asyncio
async def test_consent_required_surfaces_as_409(monkeypatch, token_cache):
    """No vaulted token means the user really hasn't connected the account.

    409, not 401: the SPA's error interceptor reads any 401 as an expired
    BFF session and redirects to login, so a 401 here would sign the user
    out over an unconnected connector.
    """
    client = _FakeClient()
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
    _patch_oauth(
        monkeypatch, resolved={"token": None, "url": "https://consent.example"}
    )

    with pytest.raises(AppToolCallError) as ei:
        await _call(session_id="disp-oauth-consent")

    assert ei.value.code == 409
    assert "google-tasks" in ei.value.message
    # Never dispatched — there was no token to dispatch with.
    assert client.calls == []


@pytest.mark.asyncio
async def test_unresolvable_provider_still_dispatches(monkeypatch, token_cache):
    """`None` means "couldn't ask AgentCore", not "user must consent".

    Prompting on it would tell a connected user to connect. Let the call go
    out instead — the server's own error is a truer report than a guess.
    """
    client = _FakeClient()
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
    _patch_oauth(monkeypatch, resolved=None)

    payload = await _call(session_id="disp-oauth-unresolved")

    assert payload["result"]["isError"] is False
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_disconnected_user_bypasses_the_cached_token(monkeypatch, token_cache):
    """A disconnect must reach an App frame that is still open on screen.

    Without this the App keeps working off the cached token for the rest of
    its TTL after the user presses Disconnect.
    """
    client = _FakeClient()
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
    _patch_oauth(
        monkeypatch,
        resolved={"token": None, "url": "https://consent.example"},
        disconnected=True,
    )
    token_cache.set("u1", "google-tasks", "stale-post-disconnect")

    with pytest.raises(AppToolCallError) as ei:
        await _call(session_id="disp-oauth-disconnected")

    assert ei.value.code == 409
    assert token_cache.get("u1", "google-tasks") is None


@pytest.mark.asyncio
async def test_forces_reauth_when_disconnected(monkeypatch, token_cache):
    client = _FakeClient()
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
    calls = _patch_oauth(
        monkeypatch,
        resolved={"token": "re-consented", "url": None},
        disconnected=True,
    )

    await _call(session_id="disp-oauth-force")

    assert calls == [("google-tasks", "u1", True)]


@pytest.mark.asyncio
async def test_auth_shaped_failure_clears_the_cached_token(monkeypatch, token_cache):
    """A rejected token must not stay cached for the rest of its TTL.

    Cleared, not retried: an app-initiated call is whatever button the user
    pressed, so re-firing a mutation off a regex match could apply the side
    effect twice.
    """
    client = _FakeClient(_FakeResult("401 Unauthorized", is_error=True))
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
    _patch_oauth(monkeypatch, resolved={"token": "revoked", "url": None})

    payload = await _call(session_id="disp-oauth-401")

    assert payload["result"]["isError"] is True
    assert token_cache.get("u1", "google-tasks") is None
    # One call only — no automatic retry of a possibly-mutating tool.
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_ordinary_tool_error_keeps_the_cached_token(monkeypatch, token_cache):
    """Only auth-shaped failures invalidate the token."""
    client = _FakeClient(_FakeResult("Task not found", is_error=True))
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
    _patch_oauth(monkeypatch, resolved={"token": "tok-1", "url": None})

    await _call(session_id="disp-oauth-plain-error")

    assert token_cache.get("u1", "google-tasks") == "tok-1"


@pytest.mark.asyncio
async def test_non_oauth_client_never_asks_agentcore(monkeypatch, token_cache):
    """A SigV4 / unauthenticated MCP server has no provider to resolve."""
    client = _FakeClient()
    _patch(monkeypatch, enabled=True, meta=_ui(["model", "app"]), client=client)
    calls = _patch_oauth(monkeypatch, provider_id=None, resolved=None)

    payload = await _call(session_id="disp-oauth-none")

    assert payload["result"]["isError"] is False
    assert calls == []

# --- opportunistic UI-resource revalidation ---------------------------------


class _FakeResourceStore:
    """Stand-in for the `UIRES#` store: one provenance row, capture writes."""

    def __init__(self, provenance=None):
        self._provenance = provenance
        self.stored: list = []

    def get_provenance(self, *, user_id, tool_use_id):
        return self._provenance

    def store(self, **kwargs):
        self.stored.append(kwargs)


@pytest.fixture(autouse=True)
def _clear_refresh_claims():
    """The once-per-process claim set is module state — reset per test."""
    dispatch_mod._refreshed_resources.clear()
    yield
    dispatch_mod._refreshed_resources.clear()


def test_claim_refresh_is_once_per_resource():
    assert dispatch_mod._claim_refresh("s1", "tu1") is True
    # A chatty App presses buttons all day; the shell is read exactly once.
    assert dispatch_mod._claim_refresh("s1", "tu1") is False
    # A different frame in the same conversation is its own resource.
    assert dispatch_mod._claim_refresh("s1", "tu2") is True


def test_claim_refresh_bounds_its_memory():
    for i in range(dispatch_mod._MAX_REFRESHED):
        dispatch_mod._claim_refresh("s", f"tu{i}")
    assert len(dispatch_mod._refreshed_resources) == dispatch_mod._MAX_REFRESHED
    dispatch_mod._claim_refresh("s", "overflow")
    assert len(dispatch_mod._refreshed_resources) == 1


def test_refresh_rereads_the_producing_tool_and_preserves_the_anchor(monkeypatch):
    store = _FakeResourceStore(
        {"toolName": "view_task_board", "producedByMessageIndex": 4}
    )
    monkeypatch.setattr(
        "apis.shared.mcp_apps.ui_resource_store.get_ui_resource_store",
        lambda: store,
    )
    monkeypatch.setattr(
        dispatch_mod, "_resolve_client", lambda agent, name: (object(), _FakeClient())
    )
    seen = {}

    def _fetch(tool_name, tool_use_id):
        seen["tool_name"] = tool_name
        return {
            "resourceUri": "ui://tasks/board",
            "html": "<html>fresh</html>",
            "mimeType": "text/html",
            "csp": {"connect-src": ["'self'"]},
            "permissions": {},
            "sandboxOrigin": "https://sandbox.example",
            "serverName": "Google Tasks",
            "icon": "",
        }

    monkeypatch.setattr(mcp_apps_mod, "fetch_ui_resource", _fetch)

    dispatch_mod._refresh_ui_resource("u1", "s1", "tu1")

    # Re-read is keyed on the tool that PRODUCED the frame, not whatever the
    # App just called — the resourceUri hangs off the producing tool.
    assert seen["tool_name"] == "view_task_board"
    assert len(store.stored) == 1
    written = store.stored[0]
    assert written["html"] == "<html>fresh</html>"
    assert written["csp"] == {"connect-src": ["'self'"]}
    assert written["tool_name"] == "view_task_board"
    # The anchor is the producing turn's; a refresh must not renumber it.
    assert written["produced_by_message_index"] == 4


def test_refresh_no_ops_without_a_stored_row(monkeypatch):
    store = _FakeResourceStore(None)
    monkeypatch.setattr(
        "apis.shared.mcp_apps.ui_resource_store.get_ui_resource_store",
        lambda: store,
    )
    dispatch_mod._refresh_ui_resource("u1", "s1", "tu1")
    assert store.stored == []


def test_refresh_keeps_the_old_copy_when_the_read_returns_nothing(monkeypatch):
    """A server that is down must not blank an App that still works."""
    store = _FakeResourceStore({"toolName": "view_task_board"})
    monkeypatch.setattr(
        "apis.shared.mcp_apps.ui_resource_store.get_ui_resource_store",
        lambda: store,
    )
    monkeypatch.setattr(
        dispatch_mod, "_resolve_client", lambda agent, name: (object(), _FakeClient())
    )
    monkeypatch.setattr(mcp_apps_mod, "fetch_ui_resource", lambda *a: None)

    dispatch_mod._refresh_ui_resource("u1", "s1", "tu1")
    assert store.stored == []
