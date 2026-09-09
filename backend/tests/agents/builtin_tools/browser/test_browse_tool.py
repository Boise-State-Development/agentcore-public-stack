"""Tests for the AgentCore Browser tool.

No AWS: the CDP layer is exercised against a fake websocket that speaks the
protocol, and the tool layer against a fake CDP session. What these protect is
the framing (ids, sessionId routing, error surfacing) and the output budgets —
the two things a live smoke test is least likely to catch, because a happy-path
browse looks fine right up until a page returns 400KB of text.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from agents.builtin_tools.browser import browse_tool
from agents.builtin_tools.browser.browse_tool import browse_web
from agents.builtin_tools.browser.cdp_client import CdpError, CdpSession


def _call(tool, **kwargs):
    """Reach the underlying callable inside the Strands tool wrapper."""
    inner = getattr(tool, "_tool_func", None) or getattr(tool, "func", None) or tool
    if hasattr(inner, "__wrapped__"):
        inner = inner.__wrapped__
    return inner(**kwargs)


# --------------------------------------------------------------------------
# CDP framing
# --------------------------------------------------------------------------


class FakeWebSocket:
    """Answers CDP commands from a scripted table."""

    def __init__(self, responses: Dict[str, Any], *, page_targets: bool = True) -> None:
        self._responses = responses
        self._page_targets = page_targets
        self.sent: List[dict] = []
        self._outbox: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        method = message["method"]
        if method == "Target.getTargets":
            infos = [{"targetId": "T1", "type": "page"}] if self._page_targets else []
            result = {"targetInfos": infos}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "S1"}
        elif method in self._responses:
            entry = self._responses[method]
            if isinstance(entry, Exception):
                await self._outbox.put(
                    {"id": message["id"], "error": {"message": str(entry)}}
                )
                return
            result = entry
        else:
            result = {}
        await self._outbox.put({"id": message["id"], "result": result})

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._outbox.get()
        return json.dumps(item)

    async def close(self) -> None:
        self.closed = True


async def _session(ws: FakeWebSocket) -> CdpSession:
    session = CdpSession(ws)
    session._reader = asyncio.create_task(session._read_loop())
    await session._attach_to_page()
    return session


@pytest.mark.asyncio
async def test_attaches_to_page_and_scopes_later_commands() -> None:
    ws = FakeWebSocket({"Runtime.evaluate": {"result": {"value": "hello"}}})
    session = await _session(ws)

    value = await session.evaluate("1+1")

    assert value == "hello"
    target_cmds = [m for m in ws.sent if m["method"].startswith("Target.")]
    assert [m["method"] for m in target_cmds] == [
        "Target.getTargets",
        "Target.attachToTarget",
    ]
    # Target.* must be browser-scoped, page commands session-scoped.
    assert all("sessionId" not in m for m in target_cmds)
    evaluate = [m for m in ws.sent if m["method"] == "Runtime.evaluate"][0]
    assert evaluate["sessionId"] == "S1"
    await session.close()


@pytest.mark.asyncio
async def test_creates_a_target_when_none_exists() -> None:
    ws = FakeWebSocket({"Target.createTarget": {"targetId": "NEW"}}, page_targets=False)
    session = await _session(ws)

    assert any(m["method"] == "Target.createTarget" for m in ws.sent)
    await session.close()


@pytest.mark.asyncio
async def test_page_exception_surfaces_as_cdp_error() -> None:
    ws = FakeWebSocket(
        {
            "Runtime.evaluate": {
                "exceptionDetails": {
                    "exception": {"description": "ReferenceError: nope is not defined"}
                }
            }
        }
    )
    session = await _session(ws)

    with pytest.raises(CdpError, match="ReferenceError"):
        await session.evaluate("nope()")
    await session.close()


@pytest.mark.asyncio
async def test_protocol_error_surfaces_with_method_name() -> None:
    ws = FakeWebSocket({"Page.navigate": RuntimeError("Cannot navigate to invalid URL")})
    session = await _session(ws)

    with pytest.raises(CdpError, match="Page.navigate failed"):
        await session.command("Page.navigate", {"url": "::"})
    await session.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_fails_pending_commands() -> None:
    ws = FakeWebSocket({})
    session = await _session(ws)

    await session.close()
    await session.close()  # must not raise

    assert session.closed
    with pytest.raises(CdpError, match="closed"):
        await session.command("Runtime.evaluate")


# --------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/admin",
        "http://127.0.0.1/",
        "ftp://example.com/x",
    ],
)
def test_unsafe_urls_are_refused(url: str) -> None:
    assert browse_tool._validate_url(url) is not None


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com/a?b=c"])
def test_public_urls_are_allowed(url: str) -> None:
    assert browse_tool._validate_url(url) is None


# --------------------------------------------------------------------------
# Tool dispatch and output budgets
# --------------------------------------------------------------------------


@dataclass
class FakeCdp:
    values: Dict[str, Any] = field(default_factory=dict)
    navigated: List[str] = field(default_factory=list)
    closed: bool = False
    screenshot_data: str = ""

    async def navigate(self, url: str, **_: Any) -> None:
        self.navigated.append(url)

    async def evaluate(self, expression: str, **_: Any) -> Any:
        for needle, value in self.values.items():
            if needle in expression:
                return value
        return None

    async def screenshot(self) -> str:
        return self.screenshot_data


@dataclass
class FakeLive:
    cdp: FakeCdp
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class FakeState:
    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}

    def get(self, key: str) -> Any:
        return json.loads(json.dumps(self._data.get(key))) if key in self._data else None

    def set(self, key: str, value: Any) -> None:
        self._data[key] = json.loads(json.dumps(value))


class FakeAgent:
    def __init__(self) -> None:
        self.state = FakeState()


class FakeContext:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent


@pytest.fixture
def patched_pool(monkeypatch):
    """Replace the session pool so no AWS call is attempted."""
    cdp = FakeCdp()
    live = FakeLive(cdp=cdp)

    async def _acquire(_agent):
        return live

    monkeypatch.setattr(browse_tool.session_pool, "acquire", _acquire)
    return live


@pytest.mark.asyncio
async def test_navigate_returns_title_and_text(patched_pool) -> None:
    patched_pool.cdp.values = {
        "document.title": "Example Domain",
        "location.href": "https://example.com/",
        "querySelector('main')": "Body copy here.",
    }

    result = await _call(
        browse_web,
        action="navigate",
        url="https://example.com",
        tool_context=FakeContext(FakeAgent()),
    )

    assert result["status"] == "success"
    text = result["content"][0]["text"]
    assert "Example Domain" in text
    assert "Body copy here." in text
    assert patched_pool.cdp.navigated == ["https://example.com"]


@pytest.mark.asyncio
async def test_navigate_refuses_metadata_url_without_touching_the_pool(monkeypatch) -> None:
    called = False

    async def _acquire(_agent):
        nonlocal called
        called = True
        raise AssertionError("pool must not be touched")

    monkeypatch.setattr(browse_tool.session_pool, "acquire", _acquire)

    result = await _call(
        browse_web,
        action="navigate",
        url="http://169.254.169.254/latest/meta-data/",
        tool_context=FakeContext(FakeAgent()),
    )

    assert result["status"] == "error"
    assert not called


@pytest.mark.asyncio
async def test_page_text_is_truncated_to_the_budget(patched_pool, monkeypatch) -> None:
    monkeypatch.setattr(browse_tool, "MAX_TEXT_CHARS", 100)
    patched_pool.cdp.values = {"querySelector('main')": "x" * 5000}

    result = await _call(
        browse_web, action="extract_text", tool_context=FakeContext(FakeAgent())
    )

    text = result["content"][0]["text"]
    assert len(text) < 400
    assert "truncated" in text


@pytest.mark.asyncio
async def test_click_reports_a_missing_selector_as_error(patched_pool) -> None:
    patched_pool.cdp.values = {"querySelector": False}

    result = await _call(
        browse_web,
        action="click",
        selector="#nope",
        tool_context=FakeContext(FakeAgent()),
    )

    assert result["status"] == "error"
    assert "#nope" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_type_requires_selector_and_text(patched_pool) -> None:
    result = await _call(
        browse_web, action="type", selector="#q", tool_context=FakeContext(FakeAgent())
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_evaluate_renders_structured_results(patched_pool) -> None:
    patched_pool.cdp.values = {"headings": ["One", "Two"]}

    result = await _call(
        browse_web,
        action="evaluate",
        script="headings",
        tool_context=FakeContext(FakeAgent()),
    )

    assert result["status"] == "success"
    assert "One" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_screenshot_returns_image_bytes(patched_pool) -> None:
    import base64

    patched_pool.cdp.screenshot_data = base64.b64encode(b"PNGDATA").decode()

    result = await _call(
        browse_web, action="screenshot", tool_context=FakeContext(FakeAgent())
    )

    assert result["content"][1]["image"]["source"]["bytes"] == b"PNGDATA"


@pytest.mark.asyncio
async def test_unknown_action_lists_valid_actions(patched_pool) -> None:
    result = await _call(
        browse_web, action="teleport", tool_context=FakeContext(FakeAgent())
    )

    assert result["status"] == "error"
    assert "navigate" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_kill_switch_short_circuits(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_TOOL_ENABLED", "false")

    async def _acquire(_agent):
        raise AssertionError("pool must not be touched when disabled")

    monkeypatch.setattr(browse_tool.session_pool, "acquire", _acquire)

    result = await _call(
        browse_web,
        action="navigate",
        url="https://example.com",
        tool_context=FakeContext(FakeAgent()),
    )

    assert result["status"] == "error"
    assert "disabled" in result["content"][0]["text"]
