"""Tests for the api-converse client: request shape, streaming, error mapping."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from agentcore_tui.client import ApiConverseClient
from agentcore_tui.conversation import Message
from agentcore_tui.client.events import ConverseEvent, Done, ErrorEvent, MessageStop, TextDelta, TurnAccumulator
from agentcore_tui.config import Config
from agentcore_tui.credentials import CredentialSource
from agentcore_tui.errors import (
    AuthError,
    BadRequestError,
    ConfigError,
    ConnectionFailedError,
    ModelAccessDeniedError,
    RateLimitedError,
    UpstreamError,
)

from .conftest import BASE_URL, MODEL_ID, make_config, sse_body, sse_response, text_stream

ClientFactory = Callable[[Callable[[httpx.Request], httpx.Response]], ApiConverseClient]

HELLO = [Message(role="user", content="hello")]


async def drain(client: ApiConverseClient, messages: list[Message] | None = None) -> list[ConverseEvent]:
    """Collect every event from one streamed turn."""
    return [event async for event in client.stream(messages or HELLO)]


class TestConstruction:
    def test_requires_base_url(self) -> None:
        with pytest.raises(ConfigError, match="base URL"):
            ApiConverseClient(make_config(base_url=""))

    def test_requires_api_key(self) -> None:
        with pytest.raises(ConfigError, match="API key"):
            ApiConverseClient(make_config(api_key=None, credential_source=CredentialSource.NONE))

    async def test_does_not_close_an_injected_client(self, config: Config) -> None:
        """Callers that supply a client keep ownership of its lifecycle."""
        injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
        client = ApiConverseClient(config, client=injected)
        await client.aclose()
        assert injected.is_closed is False
        await injected.aclose()

    async def test_closes_a_client_it_created(self, config: Config) -> None:
        client = ApiConverseClient(config)
        await client.aclose()
        assert client._client.is_closed is True


class TestRequestShape:
    async def test_posts_expected_url_headers_and_body(self, make_client: ClientFactory) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["api_key"] = request.headers.get("X-API-Key")
            seen["accept"] = request.headers.get("Accept")
            seen["user_agent"] = request.headers.get("User-Agent")
            seen["body"] = json.loads(request.content)
            return sse_response(text_stream(["ok"]))

        await drain(make_client(handler))

        assert seen["url"] == f"{BASE_URL}/chat/api-converse"
        assert seen["api_key"] == "test-key"
        assert seen["accept"] == "text/event-stream"
        assert str(seen["user_agent"]).startswith("agentcore-tui/")
        body = seen["body"]
        assert isinstance(body, dict)
        assert body["model_id"] == MODEL_ID
        assert body["stream"] is True
        assert body["messages"] == [{"role": "user", "content": "hello"}]

    async def test_sends_full_history_for_multi_turn(self, make_client: ClientFactory) -> None:
        captured: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.extend(json.loads(request.content)["messages"])
            return sse_response(text_stream(["ok"]))

        history = [
            Message(role="user", content="first"),
            Message(role="assistant", content="reply"),
            Message(role="user", content="second"),
        ]
        await drain(make_client(handler), history)

        assert [message["role"] for message in captured] == ["user", "assistant", "user"]
        assert captured[-1]["content"] == "second"

    async def test_optional_inference_params_are_omitted_when_unset(self, make_client: ClientFactory) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return sse_response(text_stream(["ok"]))

        await drain(make_client(handler))
        assert "temperature" not in captured
        assert "top_p" not in captured
        assert "system_prompt" not in captured

    async def test_configured_inference_params_are_sent(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return sse_response(text_stream(["ok"]))

        config = make_config(
            temperature=0.25,
            top_p=0.9,
            max_tokens=256,
            system_prompt="be terse",
        )
        client = ApiConverseClient(config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await drain(client)

        assert captured["temperature"] == 0.25
        assert captured["top_p"] == 0.9
        assert captured["max_tokens"] == 256
        assert captured["system_prompt"] == "be terse"

    async def test_model_override_applies_to_one_call_only(self, make_client: ClientFactory) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["model_id"])
            return sse_response(text_stream(["ok"]))

        client = make_client(handler)
        override = "us.anthropic.claude-opus-4-7-20260115-v1:0"
        [event async for event in client.stream(HELLO, model_id=override)]
        await drain(client)

        assert seen == [override, MODEL_ID]


class TestStreaming:
    async def test_yields_parsed_events_in_order(self, make_client: ClientFactory) -> None:
        client = make_client(lambda _: sse_response(text_stream(["Hel", "lo"])))
        events = await drain(client)

        assert [event for event in events if isinstance(event, TextDelta)] == [
            TextDelta(index=0, text="Hel"),
            TextDelta(index=0, text="lo"),
        ]
        assert isinstance(events[-1], Done)

    async def test_accumulates_into_a_complete_turn(self, make_client: ClientFactory) -> None:
        client = make_client(lambda _: sse_response(text_stream(["Hello", " there"], usage={"inputTokens": 7, "outputTokens": 3})))
        accumulator = TurnAccumulator()
        for event in await drain(client):
            accumulator.apply(event)

        assert accumulator.text == "Hello there"
        assert accumulator.usage is not None
        assert accumulator.usage.input_tokens == 7
        assert accumulator.finished
        assert accumulator.ok

    async def test_reasoning_stream_is_captured_separately(self, make_client: ClientFactory) -> None:
        body = sse_body(
            [
                ("message_start", {"role": "assistant"}),
                ("reasoning_start", {"contentBlockIndex": 0}),
                ("reasoning_delta", {"contentBlockIndex": 0, "text": "let me think"}),
                ("reasoning_stop", {"contentBlockIndex": 0}),
                ("content_block_delta", {"contentBlockIndex": 1, "type": "text", "text": "42"}),
                ("message_stop", {"stopReason": "end_turn"}),
                ("done", {}),
            ]
        )
        accumulator = TurnAccumulator()
        for event in await drain(make_client(lambda _: sse_response(body))):
            accumulator.apply(event)

        assert accumulator.reasoning == "let me think"
        assert accumulator.text == "42"

    async def test_mid_stream_error_arrives_as_event_not_exception(self, make_client: ClientFactory) -> None:
        """The server has already sent 200, so the failure must ride the stream."""
        body = sse_body([("error", {"error": "Model invocation failed"}), ("done", {})])
        events = await drain(make_client(lambda _: sse_response(body)))

        assert ErrorEvent(message="Model invocation failed") in events
        assert isinstance(events[-1], Done)

    async def test_stream_split_across_chunks_is_reassembled(self, config: Config) -> None:
        """SSE frames can be split at arbitrary byte boundaries by the network."""
        raw = text_stream(["alpha", "beta"])
        midpoint = len(raw) // 2

        async def chunks() -> AsyncIterator[bytes]:
            # Split mid-frame so the line decoder has to buffer across chunks.
            yield raw[:midpoint]
            yield raw[midpoint:]

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=chunks(), headers={"content-type": "text/event-stream"})

        client = ApiConverseClient(config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        accumulator = TurnAccumulator()
        for event in await drain(client):
            accumulator.apply(event)

        assert accumulator.text == "alphabeta"

    async def test_max_tokens_stop_reason_is_surfaced(self, make_client: ClientFactory) -> None:
        body = sse_body(
            [
                ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": "cut"}),
                ("message_stop", {"stopReason": "max_tokens"}),
                ("done", {}),
            ]
        )
        events = await drain(make_client(lambda _: sse_response(body)))
        assert MessageStop(stop_reason="max_tokens") in events


class TestErrorMapping:
    @staticmethod
    def _json_error(status: int, detail: str, headers: dict[str, str] | None = None) -> Callable[[httpx.Request], httpx.Response]:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"detail": detail}, headers=headers or {})

        return handler

    async def test_401_raises_auth_error_with_renewal_hint(self, make_client: ClientFactory) -> None:
        client = make_client(self._json_error(401, "Invalid or expired API key"))
        with pytest.raises(AuthError) as caught:
            await drain(client)
        assert caught.value.status_code == 401
        assert "login" in caught.value.hint

    async def test_403_names_the_denied_model(self, make_client: ClientFactory) -> None:
        client = make_client(self._json_error(403, f"Access denied to model: {MODEL_ID}"))
        with pytest.raises(ModelAccessDeniedError) as caught:
            await drain(client)
        assert caught.value.model_id == MODEL_ID
        assert MODEL_ID in str(caught.value)

    async def test_429_captures_retry_after(self, make_client: ClientFactory) -> None:
        client = make_client(self._json_error(429, "Rate limit exceeded.", {"Retry-After": "60"}))
        with pytest.raises(RateLimitedError) as caught:
            await drain(client)
        assert caught.value.retry_after == 60
        assert "60s" in caught.value.hint

    async def test_429_without_retry_after_still_maps(self, make_client: ClientFactory) -> None:
        client = make_client(self._json_error(429, "Quota exceeded"))
        with pytest.raises(RateLimitedError) as caught:
            await drain(client)
        assert caught.value.retry_after is None

    async def test_400_maps_to_bad_request(self, make_client: ClientFactory) -> None:
        client = make_client(self._json_error(400, "messages array must not be empty"))
        with pytest.raises(BadRequestError):
            await drain(client)

    async def test_502_maps_to_upstream_error(self, make_client: ClientFactory) -> None:
        client = make_client(self._json_error(502, "Model invocation failed due to a service error."))
        with pytest.raises(UpstreamError):
            await drain(client)

    async def test_unparseable_error_body_still_raises(self, make_client: ClientFactory) -> None:
        client = make_client(lambda _: httpx.Response(500, content=b"<html>gateway</html>"))
        with pytest.raises(UpstreamError) as caught:
            await drain(client)
        assert "500" in str(caught.value)

    async def test_connection_failure_names_the_base_url(self, make_client: ClientFactory) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(ConnectionFailedError) as caught:
            await drain(make_client(handler))
        assert BASE_URL in str(caught.value)

    async def test_read_timeout_reports_the_budget(self, make_client: ClientFactory) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        with pytest.raises(ConnectionFailedError) as caught:
            await drain(make_client(handler))
        assert "no data" in str(caught.value)


class TestNonStreaming:
    async def test_complete_returns_parsed_turn(self, make_client: ClientFactory) -> None:
        payload = {
            "role": "assistant",
            "content": "the answer",
            "model_id": MODEL_ID,
            "usage": {"inputTokens": 5, "outputTokens": 2},
            "stop_reason": "end_turn",
            "reasoning": "some thinking",
        }
        client = make_client(lambda _: httpx.Response(200, json=payload))
        turn = await client.complete(HELLO)

        assert turn.text == "the answer"
        assert turn.reasoning == "some thinking"
        assert turn.stop_reason == "end_turn"
        assert turn.usage is not None
        assert turn.usage.total_tokens == 7

    async def test_complete_sets_stream_false(self, make_client: ClientFactory) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"content": "hi", "model_id": MODEL_ID})

        await make_client(handler).complete(HELLO)
        assert captured["stream"] is False

    async def test_complete_maps_errors_too(self, make_client: ClientFactory) -> None:
        client = make_client(lambda _: httpx.Response(401, json={"detail": "nope"}))
        with pytest.raises(AuthError):
            await client.complete(HELLO)

    async def test_complete_rejects_non_json_body(self, make_client: ClientFactory) -> None:
        client = make_client(lambda _: httpx.Response(200, content=b"not json"))
        with pytest.raises(UpstreamError):
            await client.complete(HELLO)
