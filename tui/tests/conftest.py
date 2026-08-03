"""Shared fixtures.

Every test drives the client through ``httpx.MockTransport``, so the suite never
opens a socket and never needs a deployed backend.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence

import httpx
import pytest

from agentcore_tui.client import ApiConverseClient
from agentcore_tui.config import Config

BASE_URL = "https://example.test/api"
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


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


@pytest.fixture
def config() -> Config:
    """A complete config pointing at the mock host."""
    return Config(base_url=BASE_URL, api_key="test-key", model_id=MODEL_ID, models=(MODEL_ID,))


@pytest.fixture
def make_client(config: Config) -> Callable[[Callable[[httpx.Request], httpx.Response]], ApiConverseClient]:
    """Build an ApiConverseClient wired to a MockTransport handler."""

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> ApiConverseClient:
        transport = httpx.MockTransport(handler)
        return ApiConverseClient(config, client=httpx.AsyncClient(transport=transport))

    return factory
