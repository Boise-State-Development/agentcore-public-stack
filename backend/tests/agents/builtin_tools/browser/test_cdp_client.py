"""Tests for `CdpSession` liveness.

The trap these exist to hold down: a CDP socket closed by the *peer* has to
read as closed locally. Handing the browser to a human disables the automation
stream and the service closes this socket with a clean 1000 ("Disconnected by
admin"); a clean close ends the reader's `async for` normally, with no
exception to catch. A session that only tracked its own `close()` therefore
reported `closed is False` forever while holding a dead socket, and
`session_pool.acquire` kept handing that corpse back rather than reconnecting
— every later browse in the conversation failed, permanently.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, List, Optional

import pytest

from agents.builtin_tools.browser.cdp_client import CdpError, CdpSession


class FakeWebSocket:
    """A websocket whose iteration ends the way a closed socket's does."""

    def __init__(self, frames: Optional[List[str]] = None, error: Any = None) -> None:
        self._frames = list(frames or [])
        self._error = error
        self.sent: List[str] = []
        self.closed = False

    def __aiter__(self) -> "FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        if self._error is not None:
            raise self._error
        raise StopAsyncIteration  # clean peer close: no exception reaches us

    async def send(self, _payload: str) -> None:
        if self.closed or not self._frames:
            raise ConnectionError("sent 1000 (OK) Disconnected by admin")

    async def close(self) -> None:
        self.closed = True


async def _drain(session: CdpSession) -> None:
    """Run the reader to completion, as the background task would."""
    await session._read_loop()


class TestPeerClose:
    @pytest.mark.asyncio
    async def test_a_clean_peer_close_marks_the_session_closed(self) -> None:
        session = CdpSession(FakeWebSocket())

        assert session.closed is False
        await _drain(session)

        # Without this the pool re-uses a dead socket for the life of the
        # conversation instead of reconnecting to the same remote browser.
        assert session.closed is True

    @pytest.mark.asyncio
    async def test_an_errored_peer_close_marks_the_session_closed(self) -> None:
        session = CdpSession(FakeWebSocket(error=ConnectionError("boom")))

        await _drain(session)

        assert session.closed is True

    @pytest.mark.asyncio
    async def test_pending_commands_fail_rather_than_hang(self) -> None:
        session = CdpSession(FakeWebSocket())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        session._pending[1] = future

        await _drain(session)

        assert future.done()
        with pytest.raises(CdpError):
            future.result()

    @pytest.mark.asyncio
    async def test_commands_refuse_a_closed_session(self) -> None:
        session = CdpSession(FakeWebSocket())
        await _drain(session)

        with pytest.raises(CdpError, match="closed"):
            await session.command("Page.navigate", {"url": "https://example.edu"})


class AnsweringWebSocket:
    """A websocket that answers every command, and remembers what was sent."""

    def __init__(self, results: Optional[dict] = None) -> None:
        self.results = results or {}
        self.sent: List[dict] = []
        self._replies: asyncio.Queue = asyncio.Queue()

    def __aiter__(self) -> "AnsweringWebSocket":
        return self

    async def __anext__(self) -> str:
        return await self._replies.get()

    async def send(self, payload: str) -> None:
        message = json.loads(payload)
        self.sent.append(message)
        result = self.results.get(message["method"], {})
        await self._replies.put(json.dumps({"id": message["id"], "result": result}))

    async def close(self) -> None:
        pass


class TestSetWindowSize:
    """Chrome on a bare X display only re-fits through `setWindowBounds`."""

    @pytest.mark.asyncio
    async def test_finds_the_page_window_then_normalizes_then_sizes(self) -> None:
        ws = AnsweringWebSocket({"Browser.getWindowForTarget": {"windowId": 7}})
        session = CdpSession(ws)
        session._target_id = "page-1"
        session._page_session_id = "s-1"
        session._reader = asyncio.create_task(session._read_loop())
        try:
            await session.set_window_size(768, 832)
        finally:
            await session.close()

        assert [(m["method"], m.get("params")) for m in ws.sent] == [
            ("Browser.getWindowForTarget", {"targetId": "page-1"}),
            (
                "Browser.setWindowBounds",
                {"windowId": 7, "bounds": {"windowState": "normal"}},
            ),
            (
                "Browser.setWindowBounds",
                {
                    "windowId": 7,
                    "bounds": {"left": 0, "top": 0, "width": 768, "height": 832},
                },
            ),
        ]
        # Browser-scoped: a page sessionId would address the tab, not the window.
        assert all("sessionId" not in m for m in ws.sent)

    @pytest.mark.asyncio
    async def test_no_window_is_an_error_not_a_silent_no_op(self) -> None:
        ws = AnsweringWebSocket({"Browser.getWindowForTarget": {}})
        session = CdpSession(ws)
        session._reader = asyncio.create_task(session._read_loop())
        try:
            with pytest.raises(CdpError, match="windowId"):
                await session.set_window_size(768, 832)
        finally:
            await session.close()
