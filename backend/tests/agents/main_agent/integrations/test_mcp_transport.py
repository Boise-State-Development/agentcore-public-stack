"""The mcp 2.x streamable-HTTP transport puts HTTP statuses back on errors.

mcp 2.x answers a non-2xx POST with "Server returned an error response" for
that one request and keeps the session. Our auth heuristics need the 401, and
the unknown-outcome rule needs the 5xx, so `mcp_transport` writes the status
onto the error. These run a real Strands `MCPClient` against a real server.
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx2
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import INTERNAL_ERROR, ErrorData, JSONRPCError, JSONRPCResponse
from strands.tools.mcp import MCPClient

from agents.main_agent.integrations.external_mcp_client import _is_auth_failure
from agents.main_agent.integrations.mcp_transport import (
    _FailedPostStatuses,
    _StatusCarryingReadStream,
)
from apis.shared.oauth.auth_failure import looks_like_auth_failure
from apis.shared.tools.mcp_session import (
    server_error_outcome_unknown,
    session_is_active,
)
from tests.fixtures.flaky_mcp_server import FlakyMCPServer


@contextlib.contextmanager
def _stopping(client: Any):
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            client.stop(None, None, None)


def test_a_5xx_answer_carries_its_status_and_keeps_the_session():
    with FlakyMCPServer() as server, _stopping(server.client()) as client:
        client.start()
        result = client.call_tool_sync("t-1", "issue_write", {"title": "a"})

        assert result["status"] == "error"
        assert "Server returned HTTP 503 Service Unavailable" in result["content"][0]["text"]
        assert server_error_outcome_unknown(result)
        assert session_is_active(client)
        assert server.writes == ["a"]


def test_the_negotiated_protocol_is_2026_07_28():
    with FlakyMCPServer() as server, _stopping(server.client()) as client:
        client.start()
        assert client._background_thread_session.protocol_version == "2026-07-28"


def test_a_401_on_connect_reads_as_an_auth_failure():
    with FlakyMCPServer() as server:
        server.down = True
        server.down_status = 401
        client = server.client()
        with pytest.raises(Exception) as caught:
            client.start()
        assert _is_auth_failure(caught.value)


def test_a_503_on_connect_is_not_an_auth_failure():
    with FlakyMCPServer() as server:
        server.down = True
        client = server.client()
        with pytest.raises(Exception) as caught:
            client.start()
        assert not _is_auth_failure(caught.value)


def test_a_401_on_a_call_reads_as_an_auth_failure():
    with FlakyMCPServer() as server, _stopping(server.client()) as client:
        client.start()
        server.down = True
        server.down_status = 401
        result = client.call_tool_sync("t-1", "issue_write", {"title": "a"})

        assert looks_like_auth_failure(result)
        assert not server_error_outcome_unknown(result)


def test_a_plain_mcp_client_never_sees_the_status():
    """The premise: without our transport the status is gone."""
    from mcp.client.streamable_http import streamable_http_client

    with FlakyMCPServer() as server:
        client = MCPClient(lambda: streamable_http_client(server.url))
        with _stopping(client):
            client.start()
            result = client.call_tool_sync("t-1", "issue_write", {"title": "a"})
        assert "Server returned an error response" in result["content"][0]["text"]
        assert not server_error_outcome_unknown(result)


_URL = "https://mcp.test/mcp"


class _Inner:
    def __init__(self, item: Any) -> None:
        self.item = item
        self.last_context = "ctx"

    async def receive(self) -> Any:
        return self.item


def _error(request_id: int, message: str) -> SessionMessage:
    return SessionMessage(
        JSONRPCError(
            jsonrpc="2.0",
            id=request_id,
            error=ErrorData(code=INTERNAL_ERROR, message=message),
        )
    )


@pytest.mark.asyncio
async def test_a_servers_own_error_message_is_kept():
    statuses = _FailedPostStatuses()
    statuses._by_id[7] = (400, "Bad Request")
    stream = _StatusCarryingReadStream(_Inner(_error(7, "Invalid params")), statuses, _URL)

    item = await stream.receive()

    assert item.message.error.message == "Invalid params"
    assert statuses.pop(7) is None  # consumed either way
    assert stream.last_context == "ctx"


@pytest.mark.asyncio
async def test_an_error_with_no_recorded_status_is_kept():
    stream = _StatusCarryingReadStream(
        _Inner(_error(7, "Server returned an error response")), _FailedPostStatuses(), _URL
    )
    item = await stream.receive()
    assert item.message.error.message == "Server returned an error response"


@pytest.mark.asyncio
async def test_a_success_passes_through_untouched():
    ok = SessionMessage(JSONRPCResponse(jsonrpc="2.0", id=7, result={}))
    statuses = _FailedPostStatuses()
    statuses._by_id[7] = (503, "Service Unavailable")
    stream = _StatusCarryingReadStream(_Inner(ok), statuses, _URL)
    assert await stream.receive() is ok


def _response(status: int, body: Any, method: str = "POST") -> httpx2.Response:
    request = httpx2.Request(method, "https://mcp.test/mcp", json=body)
    return httpx2.Response(status, request=request)


def _call(request_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call"}


@pytest.mark.asyncio
async def test_only_failed_requests_are_recorded():
    statuses = _FailedPostStatuses()
    await statuses.on_response(_response(200, _call(1)))
    await statuses.on_response(_response(503, {"jsonrpc": "2.0", "method": "notifications/x"}))
    await statuses.on_response(_response(404, None, method="GET"))
    await statuses.on_response(_response(503, _call(2)))

    assert statuses.pop(1) is None
    assert statuses.pop(2) == (503, "Service Unavailable")


@pytest.mark.asyncio
async def test_the_pending_map_is_bounded():
    """A server that answers with its own JSON-RPC body never has its entry
    read by a rewrite, so the oldest entries give way."""
    statuses = _FailedPostStatuses()
    for request_id in range(200):
        await statuses.on_response(_response(400, _call(request_id)))

    assert statuses.pop(0) is None
    assert statuses.pop(199) == (400, "Bad Request")
    assert len(statuses._by_id) == 63


# --- The legacy-handshake verdict ------------------------------------------


@pytest.fixture
def fresh_verdicts(monkeypatch):
    from agents.main_agent.integrations import mcp_transport

    monkeypatch.setattr(mcp_transport, "_legacy_until", {})
    monkeypatch.setenv("AGENTCORE_MCP_APPS_HOST_ENABLED", "true")
    return mcp_transport


def _ui_client(server):
    from agents.main_agent.integrations.mcp_apps import UICapableMCPClient
    from agents.main_agent.integrations.mcp_transport import streamable_http_transport

    return UICapableMCPClient(lambda: streamable_http_transport(server.url), server_url=server.url)


def _methods(server) -> list:
    return [body.get("method") for body in server.requests if isinstance(body, dict)]


def _connect(server) -> str:
    client = _ui_client(server)
    with _stopping(client):
        client.start()
        return client._background_thread_session.protocol_version


def test_a_legacy_server_is_probed_once_then_not_again(fresh_verdicts):
    with FlakyMCPServer(fail_calls=False) as server:
        server.discover_reply = "reject"

        assert _connect(server) == "2025-11-25"
        assert _methods(server)[:2] == ["server/discover", "initialize"]
        assert fresh_verdicts.is_known_legacy_server(server.url)

        server.requests.clear()
        assert _connect(server) == "2025-11-25"
        assert "server/discover" not in _methods(server)
        assert _methods(server)[0] == "initialize"


def test_a_5xx_probe_is_not_a_legacy_verdict(fresh_verdicts):
    """GitHub answering one probe 503 must not pin it to the old protocol."""
    with FlakyMCPServer(fail_calls=False) as server:
        server.discover_reply = "503"
        assert _connect(server) == "2025-11-25"
        assert not fresh_verdicts.is_known_legacy_server(server.url)

        server.discover_reply = None
        assert _connect(server) == "2026-07-28"


def test_a_modern_server_is_never_marked_legacy(fresh_verdicts):
    with FlakyMCPServer(fail_calls=False) as server:
        assert _connect(server) == "2026-07-28"
        assert not fresh_verdicts.is_known_legacy_server(server.url)


def test_a_verdict_expires_so_an_upgraded_server_is_probed_again(fresh_verdicts, monkeypatch):
    with FlakyMCPServer(fail_calls=False) as server:
        fresh_verdicts.remember_legacy_server(server.url)
        assert _connect(server) == "2025-11-25"  # the probe is skipped

        monkeypatch.setattr(fresh_verdicts, "_LEGACY_VERDICT_TTL_SECONDS", -1.0)
        fresh_verdicts.remember_legacy_server(server.url)
        assert not fresh_verdicts.is_known_legacy_server(server.url)
        assert _connect(server) == "2026-07-28"
