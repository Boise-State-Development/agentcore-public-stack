"""Tests for the ``/chat/stream`` transport.

No sockets: every request is served by ``httpx.MockTransport``. The assertions
concentrate on the four ways this endpoint differs from its api-converse
sibling, because those are the places a plausible implementation is wrong:

* the payload carries **one** message against a server-side ``session_id``, not
  the transcript;
* ``enabled_tools`` distinguishes absent from empty;
* the credential is a session, and there is no API-key fallback to guess at;
* nothing is ever retried.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentcore_tui.client.agent_events import Done, TextDelta, ToolResult, ToolUse
from agentcore_tui.client.agent_stream import AgentStreamClient
from agentcore_tui.client.auth import ApiKeyAuth, SessionAuth
from agentcore_tui.config import Config
from agentcore_tui.errors import (
    AuthError,
    BadRequestError,
    ConfigError,
    ConnectionFailedError,
    ModelAccessDeniedError,
    RateLimitedError,
    UpstreamError,
)

from .conftest import MODEL_ID, sse_body, sse_response

BASE_URL = "https://agent.invalid/api"
SESSION_ID = "sess-abc123"


def make_config(**overrides: Any) -> Config:
    settings: dict[str, Any] = {"base_url": BASE_URL, "model_id": MODEL_ID, "max_tokens": 4096}
    settings.update(overrides)
    return Config(**settings)


def agent_body() -> bytes:
    """A minimal but realistic one-tool turn, in the server's frame order."""
    return sse_body(
        [
            ("init_event_loop", {}),
            ("session_title", {"type": "session_title", "sessionId": SESSION_ID, "title": "Sums"}),
            ("start_event_loop", {}),
            ("message_start", {"role": "assistant"}),
            ("tool_use", {"toolUseId": "t1", "name": "calculator", "input": {"expression": "2+2"}}),
            ("tool_result", {"toolUseId": "t1", "content": [{"text": "Result: 4"}], "status": "success"}),
            ("message_start", {"role": "assistant"}),
            ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": "4"}),
            ("message_stop", {"stopReason": "end_turn"}),
            ("metadata", {"usage": {"inputTokens": 100, "outputTokens": 2}, "contextWindow": 200000}),
            ("done", {}),
        ]
    )


def build(handler: Any, *, auth: Any = None, config: Config | None = None) -> AgentStreamClient:
    return AgentStreamClient(
        config or make_config(),
        auth=auth or SessionAuth("sealed-value"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def drain(client: AgentStreamClient, **kwargs: Any) -> list[Any]:
    defaults: dict[str, Any] = {"session_id": SESSION_ID, "message": "what is 2+2"}
    defaults.update(kwargs)
    return [event async for event in client.stream(**defaults)]


class TestPayload:
    async def test_sends_one_message_and_the_session_id(self) -> None:
        """History lives in AgentCore Memory.

        Sending the transcript would replay every previous turn on top of the
        server's own copy of it.
        """
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return sse_response(agent_body())

        async with build(handler) as client:
            await drain(client)

        body = json.loads(captured[0].content)
        assert body["session_id"] == SESSION_ID
        assert body["message"] == "what is 2+2"
        assert "messages" not in body

    async def test_posts_to_chat_stream(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return sse_response(agent_body())

        async with build(handler) as client:
            await drain(client)

        assert str(captured[0].url) == f"{BASE_URL}/chat/stream"
        assert captured[0].method == "POST"

    async def test_sends_the_session_as_a_bff_header(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return sse_response(agent_body())

        async with build(handler) as client:
            await drain(client)

        assert captured[0].headers["authorization"] == "BFF sealed-value"
        assert "x-api-key" not in {k.lower() for k in captured[0].headers}

    async def test_enabled_tools_absent_means_all(self) -> None:
        """Absent and empty are different instructions to the server.

        Absent means "every tool my role grants"; `[]` means "none". Sending
        `[]` to mean "unset" would silently disable the agent's tools.
        """
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return sse_response(agent_body())

        async with build(handler) as client:
            await drain(client, enabled_tools=None)

        assert "enabled_tools" not in json.loads(captured[0].content)

    async def test_enabled_tools_empty_is_sent_as_empty(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return sse_response(agent_body())

        async with build(handler) as client:
            await drain(client, enabled_tools=[])

        assert json.loads(captured[0].content)["enabled_tools"] == []

    async def test_a_tool_selection_is_passed_through(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return sse_response(agent_body())

        async with build(handler) as client:
            await drain(client, enabled_tools=["calculator", "fetch_url_content"])

        assert json.loads(captured[0].content)["enabled_tools"] == ["calculator", "fetch_url_content"]

    async def test_declares_itself_as_a_terminal(self) -> None:
        """So the agent's interface guidance matches this client.

        The server defaults to "web" when the field is absent, which is how a
        terminal user came to be told about a gear icon and offered KaTeX.
        """
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return sse_response(agent_body())

        async with build(handler) as client:
            await drain(client)

        assert json.loads(captured[0].content)["client_surface"] == "terminal"

    async def test_the_surface_is_sent_on_every_turn(self) -> None:
        """Not just the first. The server keys its agent cache on the surface, so
        omitting it mid-conversation would swap the interface guidance."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return sse_response(agent_body())

        async with build(handler) as client:
            await drain(client)
            await drain(client, message="and again")

        assert [json.loads(r.content)["client_surface"] for r in captured] == ["terminal", "terminal"]


class TestConstruction:
    def test_requires_a_base_url(self) -> None:
        with pytest.raises(ConfigError, match="base URL"):
            AgentStreamClient(make_config(base_url=""), auth=SessionAuth("s"))

    def test_auth_is_required_rather_than_guessed(self) -> None:
        """Unlike ApiConverseClient, which can derive ApiKeyAuth from Config.

        There is exactly one credential this endpoint accepts. Deriving the
        wrong one would produce a 401 that reads as an expired session rather
        than as a programming error.
        """
        with pytest.raises(TypeError):
            AgentStreamClient(make_config())  # type: ignore[call-arg]

    async def test_an_api_key_provider_is_accepted_but_will_not_authenticate(self) -> None:
        """The type system cannot forbid it, so document what happens.

        Any AuthProvider satisfies the constructor; the server is what rejects
        the wrong credential, and it does so as a 401.
        """

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "No active BFF session"})

        async with build(handler, auth=ApiKeyAuth("k")) as client:
            with pytest.raises(AuthError):
                await drain(client)


class TestEvents:
    async def test_parses_the_turn_through_the_agent_dialect(self) -> None:
        async with build(lambda _r: sse_response(agent_body())) as client:
            events = await drain(client)

        kinds = [type(event).__name__ for event in events]
        assert "ToolUse" in kinds
        assert "ToolResult" in kinds
        assert "TextDelta" in kinds
        assert isinstance(events[-1], Done)

    async def test_tool_events_carry_their_identity(self) -> None:
        async with build(lambda _r: sse_response(agent_body())) as client:
            events = await drain(client)

        use = next(e for e in events if isinstance(e, ToolUse))
        result = next(e for e in events if isinstance(e, ToolResult))
        assert use.name == "calculator"
        assert use.tool_use_id == "t1"
        assert result.tool_use_id == "t1"
        assert not result.is_error

    async def test_text_deltas_survive_the_trip(self) -> None:
        async with build(lambda _r: sse_response(agent_body())) as client:
            events = await drain(client)
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "4"


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, AuthError),
            (403, ModelAccessDeniedError),
            (429, RateLimitedError),
            (400, BadRequestError),
            (422, BadRequestError),
            (502, UpstreamError),
            (503, UpstreamError),
        ],
    )
    async def test_status_codes_map_to_typed_errors(self, status: int, expected: type[Exception]) -> None:
        async with build(lambda _r: httpx.Response(status, json={"detail": "nope"})) as client:
            with pytest.raises(expected):
                await drain(client)

    async def test_an_error_body_is_read_before_iterating(self) -> None:
        """`aconnect_sse` does not raise_for_status, and `aiter_sse()` would fail
        on a JSON error body's content-type and mask the real cause."""
        async with build(lambda _r: httpx.Response(401, json={"detail": "session expired"})) as client:
            with pytest.raises(AuthError, match="session expired"):
                await drain(client)

    async def test_a_401_never_reports_an_api_key(self) -> None:
        """The default AuthError hint is about API keys, which is wrong advice
        for a session client."""
        async with build(lambda _r: httpx.Response(401, json={"detail": "No active BFF session"})) as client:
            with pytest.raises(AuthError) as caught:
                await drain(client)
        assert "No active BFF session" in caught.value.message

    async def test_connection_failure_names_the_host(self) -> None:
        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

        async with build(explode) as client:
            with pytest.raises(ConnectionFailedError, match=BASE_URL):
                await drain(client)

    async def test_read_timeout_reports_the_budget(self) -> None:
        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        async with build(explode, config=make_config(timeout_seconds=42.0)) as client:
            with pytest.raises(ConnectionFailedError, match="42s"):
                await drain(client)

    async def test_a_failed_turn_is_never_retried(self) -> None:
        """A reopen re-runs the turn: the prompt is already in memory and tools
        may have executed, so a second attempt double-runs them."""
        attempts: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            return httpx.Response(502, json={"detail": "upstream died"})

        async with build(handler) as client:
            with pytest.raises(UpstreamError):
                await drain(client)

        assert len(attempts) == 1

    async def test_an_in_stream_error_is_an_event_not_an_exception(self) -> None:
        """By then the server has committed a 200, so the failure has to arrive
        as data."""
        body = sse_body([("message_start", {"role": "assistant"}), ("error", {"message": "tool blew up"}), ("done", {})])
        async with build(lambda _r: sse_response(body)) as client:
            events = await drain(client)
        assert any(type(e).__name__ == "ErrorEvent" for e in events)


class TestInterrupt:
    async def test_posts_to_the_session_interrupt_endpoint(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        async with build(handler) as client:
            assert await client.interrupt(SESSION_ID) is True

        assert str(captured[0].url) == f"{BASE_URL}/sessions/{SESSION_ID}/interrupt"
        assert captured[0].method == "POST"
        assert captured[0].headers["authorization"] == "BFF sealed-value"

    async def test_reports_failure_without_raising(self) -> None:
        """It runs on the cancel path, where the turn is already being torn down
        and a second failure would replace a clean "Stopped" with a traceback."""
        async with build(lambda _r: httpx.Response(404, json={"detail": "unknown session"})) as client:
            assert await client.interrupt(SESSION_ID) is False

    async def test_a_transport_failure_is_also_not_raised(self) -> None:
        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("gone")

        async with build(explode) as client:
            assert await client.interrupt(SESSION_ID) is False


class TestLifecycle:
    async def test_does_not_close_an_injected_client(self) -> None:
        injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: sse_response(agent_body())))
        async with AgentStreamClient(make_config(), auth=SessionAuth("s"), client=injected):
            pass
        assert not injected.is_closed
        await injected.aclose()

    async def test_the_sealed_session_never_reaches_the_log(self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
        import logging

        monkeypatch.setattr(logging.getLogger("agentcore_tui"), "propagate", True)
        caplog.set_level(logging.DEBUG, logger="agentcore_tui.client.agent_stream")
        async with build(lambda _r: sse_response(agent_body()), auth=SessionAuth("SEALEDSECRET")) as client:
            await drain(client)
        assert "SEALEDSECRET" not in caplog.text
