"""Shared fixtures.

Every test drives the client through ``httpx.MockTransport``, so the suite never
opens a socket and never needs a deployed backend.

Two things here exist for the sake of the next five endpoints rather than the
current one:

* :class:`RouteTable` dispatches on method *and* path, so a test can stub
  ``/chat/api-converse`` and ``/sessions`` in the same transport. A single
  handler callable answers every request identically, which stops working the
  moment a screen fetches something in the background.
* :class:`RecordingSink` implements :class:`~agentcore_tui.turn.TurnSink`, so
  the turn lifecycle can be exercised with no Textual app at all.
"""

from __future__ import annotations

import html
import inspect
import json
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

import httpx
import pytest
from textual.pilot import Pilot

from agentcore_tui.app import ChatApp
from agentcore_tui.client import ApiConverseClient
from agentcore_tui.config import Config
from agentcore_tui.credentials import CredentialSource
from agentcore_tui.client.agent_events import ToolCallRecord
from agentcore_tui.usage import Usage

BASE_URL = "https://example.test/api"
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_B = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

Handler = Callable[[httpx.Request], httpx.Response]


# ---------------------------------------------------------------------------
# SSE body construction
# ---------------------------------------------------------------------------


def sse_frame(event: str, data: dict[str, object] | None = None) -> str:
    """Render one SSE frame exactly as the server does."""
    return f"event: {event}\ndata: {json.dumps(data or {})}\n\n"


def sse_body(frames: Iterable[tuple[str, dict[str, object] | None]]) -> bytes:
    """Render a whole SSE response body."""
    return "".join(sse_frame(name, data) for name, data in frames).encode("utf-8")


def text_stream(chunks: Sequence[str], *, usage: dict[str, object] | None = None) -> bytes:
    """Build a well-formed assistant turn that emits ``chunks`` as text deltas."""
    frames: list[tuple[str, dict[str, object] | None]] = [
        ("message_start", {"role": "assistant"}),
        ("content_block_start", {"contentBlockIndex": 0, "type": "text"}),
    ]
    frames += [("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": chunk}) for chunk in chunks]
    frames += [
        ("content_block_stop", {"contentBlockIndex": 0}),
        ("message_stop", {"stopReason": "end_turn"}),
        ("metadata", {"usage": usage or {"inputTokens": 12, "outputTokens": 34}, "metrics": {"latencyMs": 900}}),
        ("done", {}),
    ]
    return sse_body(frames)


def sse_response(body: bytes, status_code: int = 200) -> httpx.Response:
    """An SSE-shaped response, including the content type httpx-sse requires."""
    return httpx.Response(status_code, content=body, headers={"content-type": "text/event-stream"})


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class RouteTable:
    """Dispatches mock responses on ``(method, path suffix)``.

    An unrouted request fails the test with a 599 whose body names the missing
    route, rather than silently returning something plausible.
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}
        self.requests: list[httpx.Request] = []

    def add(self, method: str, path_suffix: str, handler: Handler) -> RouteTable:
        self._routes[(method.upper(), path_suffix)] = handler
        return self

    def json(self, method: str, path_suffix: str, payload: object, *, status: int = 200) -> RouteTable:
        return self.add(method, path_suffix, lambda _: httpx.Response(status, json=payload))

    def sse(self, method: str, path_suffix: str, body: bytes, *, status: int = 200) -> RouteTable:
        return self.add(method, path_suffix, lambda _: sse_response(body, status))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for (method, suffix), handler in self._routes.items():
            if request.method == method and path.endswith(suffix):
                return handler(request)
        return httpx.Response(599, json={"detail": f"no mock route for {request.method} {path}"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def paths_called(self) -> list[str]:
        return [request.url.path for request in self.requests]


def converse_routes(body: bytes) -> RouteTable:
    """A route table answering only the api-converse endpoint."""
    return RouteTable().sse("POST", "/chat/api-converse", body)


# ---------------------------------------------------------------------------
# Turn sink double
# ---------------------------------------------------------------------------


@dataclass
class RecordingSink:
    """A :class:`~agentcore_tui.turn.TurnSink` that records instead of rendering."""

    text: str = ""
    reasoning: str = ""
    usages: list[Usage | None] = field(default_factory=list)
    states: list[tuple[str, bool, bool]] = field(default_factory=list)
    notices: list[tuple[str, str, bool]] = field(default_factory=list)
    tools: list[ToolCallRecord] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)

    async def on_text(self, chunk: str) -> None:
        self.text += chunk

    async def on_reasoning(self, chunk: str) -> None:
        self.reasoning += chunk

    async def on_usage(self, usage: Usage | None) -> None:
        self.usages.append(usage)

    async def on_state(self, state: str, *, busy: bool = False, error: bool = False) -> None:
        self.states.append((state, busy, error))

    async def on_notice(self, message: str, *, hint: str = "", error: bool = False) -> None:
        self.notices.append((message, hint, error))

    async def on_tool(self, record: ToolCallRecord) -> None:
        # Records the object, not a copy: the accumulator mutates it in place, so
        # a copy here would hide exactly the aliasing the design depends on.
        self.tools.append(record)

    async def on_title(self, title: str) -> None:
        self.titles.append(title)

    @property
    def state_labels(self) -> list[str]:
        return [state for state, _busy, _error in self.states]

    @property
    def errors(self) -> list[str]:
        return [message for message, _hint, error in self.notices if error]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_config(**overrides: object) -> Config:
    """A complete config pointing at the mock host.

    ``credential_source`` is set explicitly because the dataclass default is
    NONE — only ``resolve_config`` infers it, and a test that forgets would get
    the not-configured UI.
    """
    settings: dict[str, object] = {
        "base_url": BASE_URL,
        "api_key": "test-key",
        "model_id": MODEL_ID,
        "models": (MODEL_ID,),
        "credential_source": CredentialSource.API_KEY,
    }
    settings.update(overrides)
    return Config(**settings)  # type: ignore[arg-type]


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
def make_client(config: Config) -> Callable[[Handler], ApiConverseClient]:
    """Build an ApiConverseClient wired to a MockTransport handler."""

    def factory(handler: Handler) -> ApiConverseClient:
        transport = httpx.MockTransport(handler)
        return ApiConverseClient(config, client=httpx.AsyncClient(transport=transport))

    return factory


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


# ---------------------------------------------------------------------------
# App construction and frame inspection
# ---------------------------------------------------------------------------


def build_app(handler: Handler, *, config: Config | None = None) -> ChatApp:
    """A ChatApp whose client is backed by MockTransport."""
    resolved = config if config is not None else make_config(models=(MODEL_ID, MODEL_B))

    def factory(cfg: Config) -> ApiConverseClient:
        return ApiConverseClient(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    return ChatApp(resolved, client_factory=factory)


def ok_handler(chunks: list[str] | None = None) -> Handler:
    return lambda _: sse_response(text_stream(chunks or ["Hello", " world"]))


def error_handler(status: int, detail: str) -> Handler:
    return lambda _: httpx.Response(status, json={"detail": detail})


async def send(pilot: Pilot[None], app: ChatApp, prompt: str) -> None:
    """Type a prompt, submit it, and wait for the turn to settle.

    Waits on the turn controller rather than on ``workers.wait_for_complete()``.
    The latter waits for *every* worker, which is correct only while exactly one
    worker group exists — the first background fetch would make every test here
    order-dependent.
    """
    app.chat.composer.text = prompt
    await pilot.press("enter")
    await wait_for_turn(pilot, app)


async def wait_for_turn(pilot: Pilot[None], app: ChatApp, *, timeout: float = 10.0) -> None:
    """Block until no turn is in flight."""
    deadline = time.monotonic() + timeout
    # The submit is delivered as a message, so the turn may not have started yet.
    await pilot.pause()
    while app.chat.turn.busy and time.monotonic() < deadline:
        await pilot.pause(0.02)
    if app.chat.turn.busy:  # pragma: no cover - only on a hang
        raise AssertionError(f"turn still in flight after {timeout}s")
    await pilot.pause()


def command_titles(app: ChatApp) -> list[str]:
    return [command.title for command in app.get_system_commands(app.screen)]


async def run_command(app: ChatApp, title: str) -> None:
    """Invoke a palette command by title.

    Goes through ``get_system_commands`` so the test covers the palette wiring
    as well as the callback, instead of calling a private method directly.
    """
    for command in app.get_system_commands(app.screen):
        if command.title == title:
            result = command.callback()
            if inspect.isawaitable(result):
                await result
            return
    raise AssertionError(f"no palette command titled {title!r}")


def rendered_text(app: ChatApp) -> str:
    """Plain text of the current frame, one screen row per line.

    ``export_screenshot`` returns SVG. Three details make naive tag-stripping
    unreliable, and all three have produced misleading results:

    * The SVG embeds a ``<style>`` block, whose CSS text survives tag removal
      and can satisfy assertions that never appeared on screen.
    * Each styled run is its own ``<text>`` element, so a syntax-highlighted
      line is split into many fragments. Runs sharing a ``y`` are one row.
    * Spaces are ``&#160;``.

    Grouping by ``y`` reassembles rows, which makes substring assertions mean
    what they appear to mean. Note that ``x`` is ignored, so this cannot be used
    to verify horizontal alignment — measure ``widget.region`` for that. The SVG
    also carries window chrome bearing the app title, which is not app content.
    """
    svg = app.export_screenshot()
    svg = re.sub(r"<style.*?</style>", "", svg, flags=re.S)
    svg = re.sub(r"<defs.*?</defs>", "", svg, flags=re.S)

    rows: dict[float, list[str]] = {}
    for match in re.finditer(r"<text[^>]*y=\"([0-9.]+)\"[^>]*>(.*?)</text>", svg, flags=re.S):
        rows.setdefault(float(match.group(1)), []).append(html.unescape(match.group(2)))

    lines = ["".join(fragments).replace("\xa0", " ").rstrip() for _, fragments in sorted(rows.items())]
    return "\n".join(lines)
