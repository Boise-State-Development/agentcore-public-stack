"""Tests for app-initiated tools/call dispatch (MCP Apps PR #5).

Mocks the boundary the way the PR #3 tests do: a fake MCP client +
`UIToolCatalog`, no live agent. Asserts the spec-MUST app-visibility gate
at the inference-api dispatch, and that a successful call publishes
synthesized tool_use/tool_result into the per-session broker.
"""

import pytest

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
    _patch(monkeypatch, enabled=True, meta=None, client=_FakeClient())
    with pytest.raises(AppToolCallError) as ei:
        await _call()
    assert ei.value.code == 403


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

    assert payload["result"]["content"] == [{"text": '{"lists": []}'}]
    assert payload["result"]["isError"] is False


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
