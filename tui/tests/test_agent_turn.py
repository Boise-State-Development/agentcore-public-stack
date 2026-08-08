"""Tests for :class:`~agentcore_tui.turn.AgentTurnController`.

Driven with a fake client and a ``RecordingSink``, so there is no Textual app
and no socket. The assertions are about the ways an agent turn can end that its
api-converse sibling has no concept of: paused for consent, blocked by quota,
several tool calls deep, or cancelled while the server is still generating.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from agentcore_tui.client.agent_events import (
    Artifact,
    CitationEvent,
    Compaction,
    Done,
    ErrorEvent,
    MessageStart,
    MessageStop,
    Metadata,
    OAuthRequired,
    QuotaExceeded,
    Reasoning,
    SessionTitle,
    TextDelta,
    ToolResult,
    ToolUse,
)
from agentcore_tui.conversation import ConversationStore
from agentcore_tui.errors import UpstreamError
from agentcore_tui.turn import AgentTurnController
from agentcore_tui.usage import Usage

from .conftest import RecordingSink


class FakeAgentClient:
    """Yields a scripted event list, and records what it was asked."""

    def __init__(self, events: Sequence[Any], *, raises: Exception | None = None) -> None:
        self._events = list(events)
        self._raises = raises
        self.stream_calls: list[dict[str, Any]] = []
        self.interrupts: list[str] = []
        self.interrupt_result = True

    def stream(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.stream_calls.append(kwargs)

        async def gen() -> AsyncIterator[Any]:
            if self._raises is not None:
                raise self._raises
            for event in self._events:
                yield event

        return gen()

    async def interrupt(self, session_id: str) -> bool:
        self.interrupts.append(session_id)
        return self.interrupt_result


def build(
    events: Sequence[Any], *, raises: Exception | None = None, **kwargs: Any
) -> tuple[AgentTurnController, RecordingSink, ConversationStore, FakeAgentClient]:
    store = ConversationStore()
    sink = RecordingSink()
    client = FakeAgentClient(events, raises=raises)
    controller = AgentTurnController(store, sink, client_supplier=lambda: client, **kwargs)
    return controller, sink, store, client


async def run(controller: AgentTurnController, prompt: str = "hello") -> None:
    await controller.begin(prompt)
    await controller.stream()


def answer(text: str) -> list[Any]:
    return [MessageStart(), TextDelta(index=0, text=text), MessageStop(), Done()]


class TestRequestShape:
    async def test_sends_the_prompt_alone_against_the_session(self) -> None:
        controller, _sink, store, client = build(answer("hi"))
        await run(controller, "what is 2+2")

        assert client.stream_calls == [{"session_id": store.session_id, "message": "what is 2+2", "enabled_tools": None}]

    async def test_a_tool_selection_is_passed_through(self) -> None:
        controller, _sink, _store, client = build(answer("hi"), enabled_tools=["calculator"])
        await run(controller)
        assert client.stream_calls[0]["enabled_tools"] == ["calculator"]

    async def test_the_second_turn_sends_only_the_new_prompt(self) -> None:
        """The server already has turn one. Resending it would duplicate it.

        The controller is reused across turns, so this also covers that `begin`
        resets the in-flight state rather than accumulating it.
        """
        controller, _sink, _store, client = build(answer("one"))
        await run(controller, "first")
        await run(controller, "second")

        assert [call["message"] for call in client.stream_calls] == ["first", "second"]
        # And never the transcript, on either turn.
        assert all("messages" not in call for call in client.stream_calls)


class TestHappyPath:
    async def test_streams_text_and_records_the_answer(self) -> None:
        controller, sink, store, _client = build(
            [MessageStart(), TextDelta(index=0, text="4"), MessageStop(), Metadata(usage=Usage(input_tokens=10, output_tokens=1)), Done()]
        )
        await run(controller)

        assert sink.text == "4"
        assert store.turns == 1
        assert store.messages[-1].content == "4"
        assert sink.state_labels[-1] == "Ready"

    async def test_the_answer_is_the_last_message_not_the_concatenation(self) -> None:
        """Each tool round trip closes a message and opens another, so
        concatenating splices pre-tool narration onto the answer."""
        controller, _sink, store, _client = build(
            [
                MessageStart(),
                TextDelta(index=0, text="Let me calculate that."),
                MessageStop(),
                MessageStart(),
                TextDelta(index=0, text="The answer is 4."),
                MessageStop(),
                Done(),
            ]
        )
        await run(controller)
        assert store.messages[-1].content == "The answer is 4."

    async def test_reasoning_is_buffered_separately(self) -> None:
        controller, sink, _store, _client = build([MessageStart(), Reasoning(text="thinking"), TextDelta(index=0, text="4"), MessageStop(), Done()])
        await run(controller)
        assert sink.reasoning == "thinking"
        assert sink.text == "4"

    async def test_a_title_reaches_the_sink(self) -> None:
        controller, sink, _store, _client = build([SessionTitle(session_id="s", title="Arithmetic"), *answer("4")])
        await run(controller)
        assert "Arithmetic" in sink.titles

    async def test_a_title_is_reported_once_not_twice(self) -> None:
        """It is readable both as an event and off the finished accumulator."""
        controller, sink, _store, _client = build([SessionTitle(session_id="s", title="Arithmetic"), *answer("4")])
        await run(controller)
        assert sink.titles == ["Arithmetic"]

    async def test_a_title_only_on_the_accumulator_still_reaches_the_sink(self) -> None:
        """Guards the dedupe: skipping the fallback entirely would lose titles
        that arrive without the client seeing the event."""
        controller, sink, _store, _client = build(answer("4"))
        await controller.begin("hi")
        assert controller.accumulator is not None
        controller.accumulator.title = "Set by the fold"
        await controller.stream()
        assert sink.titles == ["Set by the fold"]


class TestTools:
    async def test_a_tool_call_is_reported_as_a_record(self) -> None:
        controller, sink, _store, _client = build(
            [
                MessageStart(),
                ToolUse(tool_use_id="t1", name="calculator", arguments={"expression": "2+2"}),
                ToolResult(tool_use_id="t1", text="Result: 4"),
                *answer("4"),
            ]
        )
        await run(controller)

        assert [record.name for record in sink.tools] == ["calculator", "calculator"]
        assert sink.tools[0] is sink.tools[1], "the same mutable record, not a copy"
        assert sink.tools[-1].result == "Result: 4"
        assert sink.tools[-1].finished

    async def test_the_status_names_the_running_tool(self) -> None:
        controller, sink, _store, _client = build([MessageStart(), ToolUse(tool_use_id="t1", name="calculator"), *answer("4")])
        await run(controller)
        assert "Running calculator..." in sink.state_labels

    async def test_text_before_a_tool_is_flushed_first(self) -> None:
        """Otherwise the tool widget mounts above narration that came before it."""
        controller, sink, _store, _client = build(
            [
                MessageStart(),
                TextDelta(index=0, text="Let me check."),
                ToolUse(tool_use_id="t1", name="calculator"),
                ToolResult(tool_use_id="t1", text="4"),
                *answer("Done"),
            ]
        )
        await controller.begin("hi")
        await controller.stream()
        # The narration was flushed before the tool was announced.
        assert sink.text.startswith("Let me check.")

    async def test_two_tools_are_two_records(self) -> None:
        controller, sink, _store, _client = build(
            [
                MessageStart(),
                ToolUse(tool_use_id="t1", name="calculator"),
                ToolUse(tool_use_id="t2", name="fetch_url_content"),
                *answer("done"),
            ]
        )
        await run(controller)
        assert {record.tool_use_id for record in sink.tools} == {"t1", "t2"}


class TestTerminalStates:
    async def test_quota_block_stores_nothing_and_says_so(self) -> None:
        """The turn never ran, so there is no answer and no point complaining
        about an empty response."""
        controller, sink, store, _client = build([QuotaExceeded(message="Monthly limit reached"), Done()])
        await run(controller)

        # `turns` counts user messages, and begin() appended one — so the
        # assertion that matters is that no *answer* was stored.
        assert store.last_assistant is None
        assert sink.state_labels[-1] == "Quota exceeded"
        assert any("blocked by your usage quota" in message for message in sink.errors)

    async def test_a_paused_turn_is_not_an_error(self) -> None:
        """It is waiting for the user. Reporting a failure would say the turn
        broke when it is asking for consent."""
        controller, sink, _store, _client = build(
            [
                MessageStart(),
                TextDelta(index=0, text="I need access."),
                MessageStop(),
                OAuthRequired(provider_id="google", authorization_url="https://consent.example/authorize"),
                Done(),
            ]
        )
        await run(controller)

        assert sink.state_labels[-1] == "Paused"
        assert sink.errors == []
        assert any("paused" in message for message, _hint, _error in sink.notices)

    async def test_a_paused_turn_keeps_the_partial_answer(self) -> None:
        """Discarding it would make the transcript silently lossy."""
        controller, _sink, store, _client = build(
            [
                MessageStart(),
                TextDelta(index=0, text="Partial thought."),
                MessageStop(),
                OAuthRequired(provider_id="google", authorization_url="https://consent.example/authorize"),
                Done(),
            ]
        )
        await run(controller)
        assert store.messages[-1].content == "Partial thought."

    async def test_a_stream_error_event_fails_the_turn(self) -> None:
        controller, sink, store, _client = build([MessageStart(), ErrorEvent(message="the agent crashed"), Done()])
        await run(controller)
        assert sink.state_labels[-1] == "Failed"
        assert store.last_assistant is None

    async def test_an_http_error_is_reported_not_raised(self) -> None:
        """Letting it escape would tear down the app mid-conversation."""
        controller, sink, _store, _client = build([], raises=UpstreamError("upstream died"))
        await run(controller)
        assert sink.state_labels[-1] == "Failed"
        assert "upstream died" in sink.errors[0]

    async def test_an_unexpected_exception_is_also_contained(self) -> None:
        controller, sink, _store, _client = build([], raises=RuntimeError("bug"))
        await run(controller)
        assert sink.state_labels[-1] == "Failed"
        assert any("RuntimeError" in message for message in sink.errors)

    async def test_an_empty_answer_is_called_out(self) -> None:
        controller, sink, _store, _client = build([MessageStart(), MessageStop(), Done()])
        await run(controller)
        assert any("empty response" in message for message in sink.errors)


class TestExtras:
    async def test_citations_are_batched_into_one_notice(self) -> None:
        """One notice per turn, not one per excerpt."""
        controller, sink, _store, _client = build(
            [
                MessageStart(),
                CitationEvent(document_id="d1", file_name="syllabus.pdf"),
                CitationEvent(document_id="d2", file_name="handbook.pdf"),
                *answer("see the docs"),
            ]
        )
        await run(controller)
        citation_notices = [m for m, _h, _e in sink.notices if "excerpt" in m]
        assert len(citation_notices) == 1
        assert "2 knowledge-base excerpt" in citation_notices[0]

    async def test_an_artifact_points_at_the_web_app(self) -> None:
        controller, sink, _store, _client = build([MessageStart(), Artifact(artifact_id="a1", title="Chart", version=1), *answer("made a chart")])
        await run(controller)
        assert any("Created an artifact" in m for m, _h, _e in sink.notices)

    async def test_compaction_does_not_disturb_the_turn(self) -> None:
        controller, sink, store, _client = build([Compaction(summarized_turns=3), *answer("fine")])
        await run(controller)
        assert store.messages[-1].content == "fine"
        assert sink.state_labels[-1] == "Ready"


class TestCancel:
    async def test_cancel_interrupts_the_server(self) -> None:
        """The whole point: abandoning only the local stream leaves the server
        generating and holding the session lease."""
        controller, sink, store, client = build(answer("slow"))
        await controller.begin("hi")
        await controller.cancel()

        assert client.interrupts == [store.session_id]
        assert sink.state_labels[-1] == "Stopped"

    async def test_cancel_reports_stopped_even_if_the_interrupt_fails(self) -> None:
        """A failed interrupt must not replace a clean Stopped with a traceback."""
        controller, sink, _store, client = build(answer("slow"))
        client.interrupt_result = False
        await controller.begin("hi")
        await controller.cancel()
        assert sink.state_labels[-1] == "Stopped"

    async def test_cancel_when_idle_does_nothing(self) -> None:
        controller, sink, _store, client = build(answer("x"))
        await controller.cancel()
        assert client.interrupts == []
        assert sink.states == []

    async def test_cancel_shows_a_transitional_state(self) -> None:
        """The interrupt is a network round trip, so the UI must not look frozen."""
        controller, sink, _store, _client = build(answer("x"))
        await controller.begin("hi")
        await controller.cancel()
        assert "Stopping..." in sink.state_labels


class TestBusyFlag:
    async def test_busy_is_true_during_a_turn_and_false_after(self) -> None:
        controller, _sink, _store, _client = build(answer("x"))
        assert controller.busy is False
        await controller.begin("hi")
        assert controller.busy is True
        await controller.stream()
        assert controller.busy is False

    async def test_the_accumulator_is_released_when_idle(self) -> None:
        controller, _sink, _store, _client = build(answer("x"))
        await run(controller)
        assert controller.accumulator is None

    async def test_stream_without_begin_is_a_no_op(self) -> None:
        controller, sink, _store, client = build(answer("x"))
        await controller.stream()
        assert client.stream_calls == []
        assert sink.states == []


@pytest.mark.parametrize("prompt", ["", "   "])
async def test_an_empty_prompt_still_reaches_the_server(prompt: str) -> None:
    """The UI gates empty prompts; the controller does not second-guess it."""
    controller, _sink, _store, client = build(answer("?"))
    await run(controller, prompt)
    assert client.stream_calls[0]["message"] == prompt
