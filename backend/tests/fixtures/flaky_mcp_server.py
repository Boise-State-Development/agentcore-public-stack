"""A real streamable-HTTP MCP server that can 503 or drop a call, for session tests."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time


class FlakyMCPServer:
    """A real streamable-HTTP MCP server whose `tools/call` can 503 or drop.

    With `fail_calls` set it runs the tool and then answers HTTP 503, which is
    what GitHub's MCP endpoint did on dev 2026-10-18: the issue was created,
    the POST came back 503. mcp 2.x answers that one request with an error and
    keeps the session.

    With `drop_calls` set it runs the tool and then breaks off the response
    mid-body. The transport error ends the whole session (it escapes the
    transport's task group), so the in-flight call surfaces from Strands as
    "Connection to the MCP server was closed". This is the session-death case
    that 1.x also produced for a 503.

    With `down` set it answers every request with `down_status` (503) without
    running anything — a server still unavailable when a client tries to
    reconnect, or one refusing the caller (401). `down_message` makes that
    answer the server's own JSON-RPC error instead of plain text.

    Every JSON request body it receives is kept in `requests`, so a test can
    read what the client put on the wire. `server_kwargs` go to `MCPServer`
    (`title`, `icons`: the identity a modern server stamps on discover), and
    `setup(server)` may register more tools before the app is built.
    """

    def __init__(
        self, fail_calls: bool = True, setup=None, json_response: bool = True, **server_kwargs
    ) -> None:
        import uvicorn
        from mcp.server.mcpserver import MCPServer

        self.writes: list = []
        self.requests: list = []
        self.fail_calls = fail_calls
        self.drop_calls = False
        self.down = False
        self.down_status = 503
        # While `down`, answer with this JSON-RPC error message instead of a
        # plain-text body.
        self.down_message = None
        # How `server/discover` is answered: None (the modern server answers
        # it), "reject" (a legacy server's 400 JSON-RPC error, as
        # mcp.excalidraw.com sends), or "503".
        self.discover_reply = None
        server = MCPServer(server_kwargs.pop("name", "flaky"), **server_kwargs)

        @server.tool()
        def issue_write(title: str) -> str:
            self.writes.append(title)
            return f"created {title}"

        if setup is not None:
            setup(server)

        # `json_response=False` answers each request as a server-sent event, as
        # GitHub's server does, so the transport's SSE event-size cap applies.
        inner = server.streamable_http_app(stateless_http=True, json_response=json_response)

        async def unavailable(send, request_id=None):
            if self.down_message is None:
                content_type, payload = b"text/plain", b"unavailable"
            else:
                # The server's own JSON-RPC error on the failed status, as
                # mcp.zoom.us answers a missing token with a 401.
                content_type = b"application/json"
                payload = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32003, "message": self.down_message},
                    }
                ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": self.down_status,
                    "headers": [(b"content-type", content_type)],
                }
            )
            await send({"type": "http.response.body", "body": payload})

        async def unavailable_503(send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"unavailable"})

        async def legacy_rejection(send):
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32000, "message": "Bad Request: Unsupported protocol version"},
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})

        async def broken_off(send):
            # Promise a body, then end short: the client's read fails with a
            # protocol error rather than an HTTP status.
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", b"4096"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"jsonrpc"'})

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
            parsed = None
            with contextlib.suppress(ValueError):
                parsed = json.loads(body)
                self.requests.append(parsed)

            async def replay():
                # After the body, defer to the real channel: an SSE response
                # watches it for a disconnect, and a made-up one ends the stream.
                replayed = next(replay_iter, None)
                return replayed if replayed is not None else await receive()

            if self.down:
                request_id = parsed.get("id") if isinstance(parsed, dict) else None
                return await unavailable(send, request_id)
            if self.discover_reply and b'"server/discover"' in body:
                if self.discover_reply == "503":
                    return await unavailable_503(send)
                return await legacy_rejection(send)
            failing = self.fail_calls or self.drop_calls
            if not (failing and b'"tools/call"' in body):
                return await inner(scope, replay, send)

            async def discard(_message):
                pass

            await inner(scope, replay, discard)  # the write happens...
            if self.drop_calls:
                await broken_off(send)  # ...and the connection breaks off
            else:
                await unavailable(send)  # ...or the answer is a 503

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.url = f"http://127.0.0.1:{port}/mcp"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
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

    def client(self, cls=None):
        from strands.tools.mcp import MCPClient

        from agents.main_agent.integrations.mcp_transport import (
            streamable_http_transport,
        )

        return (cls or MCPClient)(lambda: streamable_http_transport(self.url))
