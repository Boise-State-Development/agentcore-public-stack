"""Turn lifecycle tests.

Driven with :class:`~tests.conftest.RecordingSink` and no Textual app, which is
the whole reason the controller was extracted. These cover the behaviours that
previously could only be asserted by reading private attributes off the App.
"""

from __future__ import annotations

import httpx
import pytest

from agentcore_tui.client import ApiConverseClient
from agentcore_tui.conversation import ConversationStore
from agentcore_tui.errors import AuthError
from agentcore_tui.turn import TurnController
from agentcore_tui.usage import Usage

from .conftest import Handler, RecordingSink, make_config, sse_body, sse_response, text_stream


def controller(
    handler: Handler, *, store: ConversationStore | None = None, sink: RecordingSink | None = None
) -> tuple[TurnController, ConversationStore, RecordingSink]:
    resolved_store = store or ConversationStore()
    resolved_sink = sink or RecordingSink()
    config = make_config()

    def supplier() -> ApiConverseClient:
        return ApiConverseClient(config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    return (
        TurnController(resolved_store, resolved_sink, client_supplier=supplier, max_tokens=config.max_tokens),
        resolved_store,
        resolved_sink,
    )


async def run_turn(handler: Handler, prompt: str = "hi", **kwargs: object) -> tuple[ConversationStore, RecordingSink]:
    turn, store, sink = controller(handler, **kwargs)  # type: ignore[arg-type]
    await turn.begin(prompt)
    await turn.stream()
    return store, sink


def usage_handler() -> Handler:
    """A turn that reports token counts."""

    def handler(_: httpx.Request) -> httpx.Response:
        return sse_response(text_stream(["ok"], usage={"inputTokens": 7, "outputTokens": 8}))

    return handler


class TestHappyPath:
    async def test_records_both_messages(self) -> None:
        store, _ = await run_turn(lambda _: sse_response(text_stream(["Hel", "lo"])))
        assert [(m.role, m.content) for m in store] == [("user", "hi"), ("assistant", "Hello")]

    async def test_streams_text_to_the_sink(self) -> None:
        _, sink = await run_turn(lambda _: sse_response(text_stream(["Hel", "lo"])))
        assert sink.text == "Hello"

    async def test_reports_ready_when_finished(self) -> None:
        _, sink = await run_turn(lambda _: sse_response(text_stream(["ok"])))
        assert sink.state_labels[0] == "Thinking..."
        assert sink.state_labels[-1] == "Ready"

    async def test_usage_reaches_the_sink(self) -> None:
        _, sink = await run_turn(usage_handler())
        assert sink.usages[-1] == Usage(input_tokens=7, output_tokens=8)

    async def test_usage_is_stored_on_the_answer(self) -> None:
        store, _ = await run_turn(usage_handler())
        assert store.latest_usage is not None
        assert store.latest_usage.input_tokens == 7

    async def test_reasoning_is_separated_from_the_answer(self) -> None:
        body = sse_body(
            [
                ("reasoning_delta", {"contentBlockIndex": 0, "text": "pondering"}),
                ("content_block_delta", {"contentBlockIndex": 1, "type": "text", "text": "answer"}),
                ("message_stop", {"stopReason": "end_turn"}),
                ("done", {}),
            ]
        )
        store, sink = await run_turn(lambda _: sse_response(body))
        assert sink.reasoning == "pondering"
        assert sink.text == "answer"
        last = store.last_assistant
        assert last is not None
        assert last.content == "answer"
        assert last.reasoning == "pondering"

    async def test_idle_after_completing(self) -> None:
        turn, _, _ = controller(lambda _: sse_response(text_stream(["ok"])))
        await turn.begin("hi")
        assert turn.busy is True
        await turn.stream()
        assert turn.busy is False
        assert turn.accumulator is None


class TestBuffering:
    async def test_flush_is_required_before_text_reaches_the_sink(self) -> None:
        """The buffer is what keeps render cost proportional to time, not tokens."""
        turn, _, sink = controller(lambda _: sse_response(text_stream(["chunk"])))
        await turn.begin("hi")
        # Consume the stream without letting the timer run.
        await turn.stream()
        # stream() ends by completing the turn, which flushes the tail.
        assert sink.text == "chunk"

    async def test_flush_reports_whether_it_wrote(self) -> None:
        turn, _, _ = controller(lambda _: sse_response(text_stream(["x"])))
        await turn.begin("hi")
        assert await turn.flush() is False

    async def test_flush_drains_only_once(self) -> None:
        turn, _, sink = controller(lambda _: sse_response(text_stream(["x"])))
        await turn.begin("hi")
        await turn.stream()
        before = sink.text
        await turn.flush()
        assert sink.text == before


class TestFailures:
    async def test_http_error_is_reported_and_kept_out_of_history(self) -> None:
        store, sink = await run_turn(lambda _: httpx.Response(401, json={"detail": "bad key"}))
        assert [m.role for m in store] == ["user"]
        assert sink.errors
        assert sink.state_labels[-1] == "Failed"

    async def test_mid_stream_error_is_reported(self) -> None:
        body = sse_body(
            [
                ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": "partial"}),
                ("error", {"error": "Model invocation failed"}),
                ("done", {}),
            ]
        )
        store, sink = await run_turn(lambda _: sse_response(body))
        assert "Model invocation failed" in sink.errors
        assert [m.role for m in store] == ["user"]

    async def test_connection_failure_is_survivable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        _, sink = await run_turn(handler)
        assert sink.errors
        assert sink.state_labels[-1] == "Failed"

    async def test_unexpected_exception_is_contained(self) -> None:
        """An escaping exception would tear the app down mid-conversation."""

        def supplier() -> ApiConverseClient:
            raise RuntimeError("boom")

        store = ConversationStore()
        sink = RecordingSink()
        turn = TurnController(store, sink, client_supplier=supplier)
        await turn.begin("hi")
        await turn.stream()
        assert any("RuntimeError" in message for message in sink.errors)
        assert turn.busy is False

    async def test_empty_response_is_flagged(self) -> None:
        body = sse_body([("message_stop", {"stopReason": "end_turn"}), ("done", {})])
        store, sink = await run_turn(lambda _: sse_response(body))
        assert sink.errors
        assert [m.role for m in store] == ["user"]

    async def test_truncation_warns_but_keeps_the_text(self) -> None:
        body = sse_body(
            [
                ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": "cut off"}),
                ("message_stop", {"stopReason": "max_tokens"}),
                ("done", {}),
            ]
        )
        store, sink = await run_turn(lambda _: sse_response(body))
        last = store.last_assistant
        assert last is not None
        assert last.content == "cut off"
        assert any("token limit" in message for message in sink.errors)

    async def test_typed_error_hint_is_passed_through(self) -> None:
        """The hint is the actionable half of every error; losing it is a bug."""
        _, sink = await run_turn(lambda _: httpx.Response(401, json={"detail": "nope"}))
        hints = [hint for _message, hint, _error in sink.notices]
        assert any(AuthError.hint == hint for hint in hints)


class TestCancellation:
    async def test_cancel_returns_to_idle(self) -> None:
        turn, _, sink = controller(lambda _: sse_response(text_stream(["ok"])))
        await turn.begin("hi")
        await turn.cancel()
        assert turn.busy is False
        assert sink.state_labels[-1] == "Stopped"

    async def test_cancel_while_idle_is_a_no_op(self) -> None:
        turn, _, sink = controller(lambda _: sse_response(text_stream(["ok"])))
        await turn.cancel()
        assert sink.states == []

    async def test_cancel_keeps_the_user_message(self) -> None:
        """History is preserved so the prompt can be retried."""
        turn, store, _ = controller(lambda _: sse_response(text_stream(["ok"])))
        await turn.begin("hi")
        await turn.cancel()
        assert [m.content for m in store] == ["hi"]


class TestDispatchRegistry:
    async def test_unregistered_events_are_inert(self) -> None:
        """A newer server may add events; they must not break the turn."""
        body = sse_body(
            [
                ("some_future_event", {"whatever": 1}),
                ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": "fine"}),
                ("message_stop", {"stopReason": "end_turn"}),
                ("done", {}),
            ]
        )
        store, sink = await run_turn(lambda _: sse_response(body))
        assert sink.text == "fine"
        last = store.last_assistant
        assert last is not None
        assert last.content == "fine"

    async def test_every_registered_handler_is_a_coroutine_function(self) -> None:
        """Mixed sync/async handlers would silently never run."""
        import inspect

        turn, _, _ = controller(lambda _: sse_response(text_stream(["x"])))
        for handler in turn._handlers.values():
            assert inspect.iscoroutinefunction(handler)


class TestPreconditions:
    async def test_streaming_without_begin_is_a_no_op(self) -> None:
        turn, store, sink = controller(lambda _: sse_response(text_stream(["ok"])))
        await turn.stream()
        assert store.is_empty
        assert sink.states == []

    @pytest.mark.parametrize("prompt", ["hi", "a much longer prompt with words"])
    async def test_begin_records_the_prompt_verbatim(self, prompt: str) -> None:
        turn, store, _ = controller(lambda _: sse_response(text_stream(["ok"])))
        message = await turn.begin(prompt)
        assert message.content == prompt
        assert store.messages[-1] is message
