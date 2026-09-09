"""Minimal Chrome DevTools Protocol client for the AgentCore Browser.

AgentCore's automation endpoint is a SigV4-signed WebSocket that speaks CDP.
`bedrock_agentcore.tools.browser_client.BrowserClient` handles the session
lifecycle and request signing; this module handles the protocol on top of it.

Why raw CDP instead of Playwright: `websockets` is already in the image, and
the actions a browsing agent needs — navigate, evaluate, extract, screenshot —
are a handful of CDP commands. Playwright would add its bundled Node driver to
the inference-api container for auto-waiting and a selector engine we mostly
don't use, because JS evaluated in the page does the same job. If richer
interaction (frames, file chooser, real input events) is ever needed, this
module is the seam to swap.

Nothing here is specific to the AgentCore Browser except `connect()`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger(__name__)

# CDP command timeout. Navigation uses its own, longer budget.
_COMMAND_TIMEOUT_SECONDS = 30.0

# A screenshot frame can be a few MB; the default websockets frame cap (1 MiB)
# is too small for `Page.captureScreenshot`.
_MAX_FRAME_BYTES = 16 * 1024 * 1024


class CdpError(RuntimeError):
    """A CDP command returned an error, or the connection failed."""


class CdpSession:
    """One CDP connection, attached to a single page target.

    Commands are multiplexed over the socket by request id. A background
    reader resolves futures; events are dropped (we poll instead of
    subscribing, which keeps the state machine to one path).
    """

    def __init__(self, websocket: Any) -> None:
        self._ws = websocket
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        self._page_session_id: Optional[str] = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def connect(cls, ws_url: str, headers: Dict[str, str]) -> "CdpSession":
        """Open the automation socket and attach to a page target."""
        websocket = await ws_connect(
            ws_url,
            additional_headers=headers,
            max_size=_MAX_FRAME_BYTES,
            open_timeout=30,
        )
        session = cls(websocket)
        session._reader = asyncio.create_task(session._read_loop())
        await session._attach_to_page()
        return session

    async def close(self) -> None:
        """Close the socket and cancel the reader. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001 - teardown is best effort
            logger.debug("cdp: websocket close failed", exc_info=True)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CdpError("CDP connection closed"))
        self._pending.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    # -- protocol ----------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                message_id = message.get("id")
                if message_id is None:
                    continue  # an event; we don't subscribe to any
                future = self._pending.pop(message_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced on the next command
            logger.info("cdp: read loop ended (%s)", exc)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CdpError(f"CDP connection lost: {exc}"))
            self._pending.clear()

    async def command(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session_scoped: bool = True,
        timeout: float = _COMMAND_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        """Send one CDP command and return its `result`.

        `session_scoped` targets the attached page; pass False for
        browser-level commands (`Target.*`).
        """
        if self._closed:
            raise CdpError("CDP session is closed")

        self._next_id += 1
        message_id = self._next_id
        payload: Dict[str, Any] = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        if session_scoped and self._page_session_id:
            payload["sessionId"] = self._page_session_id

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._ws.send(json.dumps(payload))
            message = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(message_id, None)
            raise CdpError(f"{method} timed out after {timeout:.0f}s") from exc
        finally:
            self._pending.pop(message_id, None)

        if "error" in message:
            detail = message["error"].get("message", "unknown error")
            raise CdpError(f"{method} failed: {detail}")
        return message.get("result", {})

    async def _attach_to_page(self) -> None:
        """Find a page target and attach, so later commands address a tab."""
        targets = await self.command("Target.getTargets", session_scoped=False)
        pages = [
            t for t in targets.get("targetInfos", []) if t.get("type") == "page"
        ]
        if not pages:
            created = await self.command(
                "Target.createTarget", {"url": "about:blank"}, session_scoped=False
            )
            target_id = created["targetId"]
        else:
            target_id = pages[0]["targetId"]

        attached = await self.command(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
            session_scoped=False,
        )
        self._page_session_id = attached["sessionId"]
        await self.command("Page.enable")
        await self.command("Runtime.enable")

    # -- page operations ---------------------------------------------------

    async def evaluate(self, expression: str, *, timeout: float = _COMMAND_TIMEOUT_SECONDS) -> Any:
        """Evaluate JS in the page and return the value by value.

        Raises `CdpError` if the expression throws — the message carries the
        page's own exception text, which is what the model needs to correct
        itself.
        """
        result = await self.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        exception = result.get("exceptionDetails")
        if exception:
            text = exception.get("exception", {}).get("description") or exception.get("text")
            raise CdpError(f"Page threw: {text}")
        return result.get("result", {}).get("value")

    async def navigate(self, url: str, *, timeout: float = 45.0) -> None:
        """Navigate and wait for the document to finish loading.

        Polls `document.readyState` rather than subscribing to lifecycle
        events — one code path, and a page that never fires `load` (long-poll
        connections, some SPAs) still returns once the DOM is usable.
        """
        result = await self.command("Page.navigate", {"url": url}, timeout=timeout)
        if result.get("errorText"):
            raise CdpError(f"Navigation failed: {result['errorText']}")

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                state = await self.evaluate("document.readyState", timeout=10.0)
            except CdpError:
                state = None  # mid-navigation context swap; retry
            if state in ("interactive", "complete"):
                return
            await asyncio.sleep(0.25)
        logger.info("cdp: navigation to %s did not settle within %ss", url, timeout)

    async def add_init_script(self, source: str) -> None:
        """Install a script that runs before any page script, on every document.

        The WebMCP shim hook: this is the CDP equivalent of Playwright's
        `add_init_script`, and it is what would let a page's
        `document.modelContext` declarations be collected before the app
        bootstraps. Unused today; see `docs/specs/webmcp-host-spa-tools.md`.
        """
        await self.command("Page.addScriptToEvaluateOnNewDocument", {"source": source})

    async def screenshot(self) -> str:
        """Capture the viewport as base64 PNG."""
        result = await self.command(
            "Page.captureScreenshot", {"format": "png"}, timeout=45.0
        )
        return result.get("data", "")
