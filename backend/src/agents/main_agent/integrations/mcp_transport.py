"""The streamable-HTTP transport our MCP clients connect over (mcp 2.x).

mcp 2.x replaced 1.x's ``streamablehttp_client(url, headers=..., auth=...)``
with ``streamable_http_client(url, http_client=...)``, which takes a
pre-built ``httpx2.AsyncClient``. :func:`streamable_http_transport` keeps the
1.x call shape for our call sites (`external_mcp_client`,
`gateway_mcp_client`), so a Strands ``MCPClient`` gets a transport callable
exactly as before.

It also puts the HTTP status back on the errors the SDK reports. 2.x answers a
non-2xx POST with a JSON-RPC error for that one request and keeps the session
(1.x tore the whole session down instead). Unless the body is itself a
JSON-RPC error, that error reads only "Server returned an error response",
whatever the status was. Two things downstream read the status:

* a 401/403 is how we tell a consent or token gap from an outage
  (`external_mcp_client._is_auth_failure` on the pre-flight,
  `apis.shared.oauth.auth_failure.looks_like_auth_failure` after a call);
* a 5xx answer to a ``tools/call`` means the server may have performed the
  call anyway (GitHub created the issue, then answered 503), so the result has
  to say the outcome is unknown (`apis.shared.tools.mcp_session`).

So a response hook records the status of each failed POST by JSON-RPC id. The
read stream handed to the session then rewrites the SDK's stand-in message to
:func:`~apis.shared.tools.mcp_session.http_error_message` for that id. The
error code is left alone. The hook runs before the transport writes the error
to the stream, so the status is always there when the error is read. A server
that sends its own JSON-RPC error body keeps its own message.

Last, it remembers which servers speak only the legacy handshake. mcp 2.x
probes `server/discover` on every connect and falls back to `initialize` when
the server rejects it, so a pre-2026-07-28 server costs one extra round trip
per connect: about 125 ms against `mcp.excalidraw.com` (201 → 325 ms p50,
2026-10-17), paid on every agent build that misses the cache. The read stream
carries its URL (`server_url`), and `mcp_apps._UIExtensionClientSession`
records a legacy verdict here (`remember_legacy_server`) and skips the probe
while the verdict is fresh (`is_known_legacy_server`). A verdict lasts
`_LEGACY_VERDICT_TTL_SECONDS`, so a server that upgrades is probed again
within the hour. A wrong verdict costs nothing but the newer protocol: such a
server still speaks the legacy one.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import replace
from types import TracebackType
from typing import Any, AsyncIterator, Optional

import httpx2
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCError

from apis.shared.tools.mcp_session import http_error_message

logger = logging.getLogger(__name__)

# The message `StreamableHTTPTransport._handle_post_request` (mcp 2.1.1) puts
# on a non-2xx POST whose body is not a JSON-RPC error.
_SDK_HTTP_ERROR_MESSAGE = "Server returned an error response"

# Failed POSTs whose error has not been read yet. A server that answers with
# its own JSON-RPC error body never has its entry consumed by a rewrite, so the
# map is bounded rather than trusted to drain.
_MAX_PENDING_STATUSES = 64


# How long a legacy-handshake verdict skips the `server/discover` probe.
_LEGACY_VERDICT_TTL_SECONDS = 3600.0

_legacy_until: dict[str, float] = {}
_legacy_lock = threading.Lock()


def is_known_legacy_server(url: Optional[str]) -> bool:
    """Whether `url` recently negotiated the legacy handshake."""
    if not url:
        return False
    with _legacy_lock:
        until = _legacy_until.get(url)
        if until is None:
            return False
        if until <= time.monotonic():
            del _legacy_until[url]
            return False
        return True


def remember_legacy_server(url: Optional[str]) -> None:
    """Record that `url` rejected `server/discover` and took `initialize`."""
    if not url:
        return
    with _legacy_lock:
        _legacy_until[url] = time.monotonic() + _LEGACY_VERDICT_TTL_SECONDS


def forget_legacy_server(url: Optional[str]) -> None:
    """Record that `url` answered `server/discover` (it is not legacy)."""
    if not url:
        return
    with _legacy_lock:
        _legacy_until.pop(url, None)


class _FailedPostStatuses:
    """HTTP status of each failed JSON-RPC POST, by request id, until read."""

    def __init__(self) -> None:
        self._by_id: "OrderedDict[Any, tuple[int, str]]" = OrderedDict()

    async def on_response(self, response: httpx2.Response) -> None:
        if response.status_code < 400 or response.request.method != "POST":
            return
        try:
            body = json.loads(response.request.content or b"null")
        except (ValueError, httpx2.RequestNotRead):
            return
        request_id = body.get("id") if isinstance(body, dict) else None
        if request_id is None:
            return  # A notification: nothing waits on an answer.
        self._by_id[request_id] = (response.status_code, response.reason_phrase)
        while len(self._by_id) > _MAX_PENDING_STATUSES:
            self._by_id.popitem(last=False)

    def pop(self, request_id: Any) -> Optional[tuple[int, str]]:
        return self._by_id.pop(request_id, None)


class _StatusCarryingReadStream:
    """The transport's read stream, with HTTP statuses written onto its errors."""

    def __init__(
        self, inner: Any, statuses: _FailedPostStatuses, server_url: str
    ) -> None:
        self._inner = inner
        self._statuses = statuses
        # Read by `mcp_apps._UIExtensionClientSession`, which is handed this
        # stream, for the legacy-handshake verdict.
        self.server_url = server_url

    @property
    def last_context(self) -> Any:
        # JSONRPCDispatcher reads the sender's context off the stream.
        return getattr(self._inner, "last_context", None)

    def _rewrite(self, item: Any) -> Any:
        if not isinstance(item, SessionMessage):
            return item
        message = item.message
        if not isinstance(message, JSONRPCError):
            return item
        recorded = self._statuses.pop(message.id)
        if recorded is None or message.error.message != _SDK_HTTP_ERROR_MESSAGE:
            return item
        status, reason = recorded
        error = message.error.model_copy(
            update={"message": http_error_message(status, reason)}
        )
        return replace(item, message=message.model_copy(update={"error": error}))

    async def receive(self) -> Any:
        return self._rewrite(await self._inner.receive())

    def __aiter__(self) -> "_StatusCarryingReadStream":
        return self

    async def __anext__(self) -> Any:
        return self._rewrite(await self._inner.__anext__())

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> "_StatusCarryingReadStream":
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> Optional[bool]:
        return await self._inner.__aexit__(exc_type, exc_val, exc_tb)


@asynccontextmanager
async def streamable_http_transport(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    auth: Optional[httpx2.Auth] = None,
) -> AsyncIterator[tuple[Any, Any]]:
    """Open a streamable-HTTP MCP transport; yields ``(read_stream, write_stream)``.

    ``auth`` must be an ``httpx2.Auth``: httpx2 rejects an ``httpx.Auth`` when
    the client is built. Our auth handlers (`oauth_auth`, `gateway_auth`)
    already are.
    """
    statuses = _FailedPostStatuses()
    # `streamable_http_client` closes only a client it created itself, so this
    # one's lifetime is bound to the transport's here.
    async with create_mcp_http_client(headers=headers, auth=auth) as http_client:
        http_client.event_hooks["response"].append(statuses.on_response)
        async with streamable_http_client(url, http_client=http_client) as (
            read_stream,
            write_stream,
        ):
            yield _StatusCarryingReadStream(read_stream, statuses, url), write_stream
