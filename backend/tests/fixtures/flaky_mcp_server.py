"""A real streamable-HTTP MCP server that can answer 503, for session-death tests."""

from __future__ import annotations

import socket
import threading
import time


class FlakyMCPServer:
    """A real streamable-HTTP MCP server whose `tools/call` can 503.

    With `fail_calls` set it runs the tool and then answers HTTP 503 — what
    GitHub's MCP endpoint did on dev 2026-10-18: the issue was created, the
    POST came back 503. The MCP SDK treats any non-2xx POST as fatal for the
    session, so the in-flight call surfaces from Strands as "Connection to
    the MCP server was closed".

    With `down` set it answers every request 503 without running anything —
    a server still unavailable when a client tries to reconnect.
    """

    def __init__(self) -> None:
        import uvicorn
        from mcp.server.fastmcp import FastMCP

        self.writes: list = []
        self.fail_calls = True
        self.down = False
        server = FastMCP("flaky", stateless_http=True, json_response=True)

        @server.tool()
        def issue_write(title: str) -> str:
            self.writes.append(title)
            return f"created {title}"

        inner = server.streamable_http_app()

        async def unavailable(send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"unavailable"})

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

            if self.down:
                return await unavailable(send)
            if not (self.fail_calls and b'"tools/call"' in body):
                return await inner(scope, replay, send)

            async def discard(_message):
                pass

            await inner(scope, replay, discard)  # the write happens...
            await unavailable(send)  # ...and the answer is a 503

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
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp import MCPClient

        return (cls or MCPClient)(lambda: streamablehttp_client(self.url))
