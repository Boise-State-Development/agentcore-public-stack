"""Agent-stream dialect tests.

Pure parsing and folding — no network, no Textual. The cases worth pinning down
are the ones where this dialect differs from the api-converse one:

* passthrough frames must be dropped, or every answer doubles;
* a tool-using turn produces several assistant messages and only the last is
  the answer;
* `metadata` is per-LLM-call while `metadata_summary` is the turn total;
* the same field arrives in camelCase or snake_case depending on which backend
  path emitted the frame.
"""

from __future__ import annotations

import json

import pytest

from agentcore_tui.client.agent_events import (
    PASSTHROUGH_EVENT_NAMES,
    TOOL_RESULT_PREVIEW_CHARS,
    AgentTurnAccumulator,
    Artifact,
    CitationEvent,
    Compaction,
    ContentBlockStart,
    ContentBlockStop,
    Done,
    ErrorEvent,
    IgnoredEvent,
    Metadata,
    MetadataSummary,
    MessageStart,
    MessageStop,
    OAuthRequired,
    QuotaExceeded,
    QuotaSessionNotice,
    QuotaWarning,
    Reasoning,
    SessionTitle,
    TextDelta,
    ToolApprovalRequired,
    ToolInputDelta,
    ToolProgress,
    ToolResult,
    ToolUse,
    UnknownEvent,
    parse_agent_event,
)


def ev(name: str, payload: object | None = None):
    """Parse an event from a name and a payload object."""
    return parse_agent_event(name, "" if payload is None else json.dumps(payload))


def fold(*events) -> AgentTurnAccumulator:
    acc = AgentTurnAccumulator()
    for event in events:
        acc.apply(event)
    return acc


# =====================================================================
# Passthrough frames
# =====================================================================


class TestPassthroughIsIgnored:
    """The whole reason this module is a sibling rather than an extension."""

    @pytest.mark.parametrize("name", sorted(PASSTHROUGH_EVENT_NAMES))
    def test_named_passthrough_frames_are_ignored(self, name: str) -> None:
        parsed = ev(name, {"anything": "here"})
        assert isinstance(parsed, IgnoredEvent)
        assert parsed.name == name

    @pytest.mark.parametrize("name", sorted(PASSTHROUGH_EVENT_NAMES))
    def test_passthrough_is_not_unknown(self, name: str) -> None:
        """Routing these to UnknownEvent would make a logging change double output."""
        assert not isinstance(ev(name, {}), UnknownEvent)

    def test_passthrough_text_never_reaches_the_answer(self) -> None:
        """A `message` frame restates the assistant text. Folding it doubles it."""
        acc = fold(
            MessageStart(),
            TextDelta(index=0, text="The answer is 42."),
            ev("message", {"message": {"content": [{"text": "The answer is 42."}]}}),
            MessageStop(),
            Done(),
        )
        assert acc.text == "The answer is 42."

    def test_mcp_app_ui_events_are_ignored_with_a_reason(self) -> None:
        """No terminal rendering for an HTML iframe — but not 'unimplemented'."""
        for name in ("ui_resource", "ui_tool_input_partial"):
            parsed = ev(name, {"toolUseId": "t1", "html": "<div/>"})
            assert isinstance(parsed, IgnoredEvent)
            assert parsed.reason == "no terminal rendering"


# =====================================================================
# Parsing
# =====================================================================


class TestParsingLifecycle:
    def test_message_start(self) -> None:
        assert ev("message_start", {"role": "assistant"}) == MessageStart(role="assistant")

    def test_text_delta(self) -> None:
        parsed = ev("content_block_delta", {"contentBlockIndex": 2, "text": "hi"})
        assert parsed == TextDelta(index=2, text="hi")

    def test_tool_input_delta_is_not_text(self) -> None:
        """Block type is inferred from which field is present, as the SPA does.

        Treating this as prose would leak raw JSON into the answer.
        """
        parsed = ev("content_block_delta", {"contentBlockIndex": 1, "input": '{"q": "we'})
        assert isinstance(parsed, ToolInputDelta)
        assert parsed.partial_json == '{"q": "we'

    def test_content_block_start_with_tool_use(self) -> None:
        parsed = ev(
            "content_block_start",
            {"contentBlockIndex": 1, "type": "tool_use", "toolUse": {"toolUseId": "t1", "name": "web_search"}},
        )
        assert parsed == ContentBlockStart(index=1, block_type="tool_use", tool_use_id="t1", tool_name="web_search")

    def test_content_block_start_defaults_to_text(self) -> None:
        parsed = ev("content_block_start", {"contentBlockIndex": 0})
        assert isinstance(parsed, ContentBlockStart)
        assert parsed.block_type == "text"

    def test_bare_frame_type_does_not_become_the_block_type(self) -> None:
        """A `data:`-only frame names itself in `type`, colliding with the block
        type field on this event. `toolUse` breaks the tie."""
        parsed = parse_agent_event("", json.dumps({"type": "content_block_start", "contentBlockIndex": 0}))
        assert isinstance(parsed, ContentBlockStart)
        assert parsed.block_type == "text"

    def test_message_stop_carries_stop_reason(self) -> None:
        assert ev("message_stop", {"stopReason": "max_tokens"}) == MessageStop(stop_reason="max_tokens")

    def test_content_block_stop(self) -> None:
        assert ev("content_block_stop", {"contentBlockIndex": 3}) == ContentBlockStop(index=3)

    def test_reasoning_uses_whole_chunks(self) -> None:
        """The agent dialect sends `reasoning`, not start/delta/stop."""
        assert ev("reasoning", {"reasoningText": "thinking"}) == Reasoning(text="thinking")

    def test_done_and_complete_are_both_terminal(self) -> None:
        assert isinstance(ev("done", {}), Done)
        assert isinstance(ev("complete", {}), Done)


class TestParsingTools:
    def test_tool_use_nested_shape(self) -> None:
        parsed = ev("tool_use", {"tool_use": {"tool_use_id": "t1", "name": "search", "input": '{"q":"x"}'}})
        assert parsed == ToolUse(tool_use_id="t1", name="search", arguments={"q": "x"}, partial=False)

    def test_tool_use_flat_shape(self) -> None:
        parsed = ev("tool_use", {"toolUseId": "t2", "name": "calc", "input": {"a": 1}})
        assert parsed == ToolUse(tool_use_id="t2", name="calc", arguments={"a": 1})

    def test_partial_tool_input_is_flagged_not_an_error(self) -> None:
        """Arguments stream as a JSON prefix; a later re-emit completes them."""
        parsed = ev("tool_use", {"toolUseId": "t1", "name": "s", "input": '{"q": "par'})
        assert isinstance(parsed, ToolUse)
        assert parsed.partial is True
        assert parsed.arguments == {}

    def test_tool_result_flat_shape(self) -> None:
        parsed = ev("tool_result", {"toolUseId": "t1", "result": "42"})
        assert parsed == ToolResult(tool_use_id="t1", text="42", is_error=False)

    def test_tool_result_message_shape(self) -> None:
        parsed = ev(
            "tool_result",
            {"message": {"content": [{"toolResult": {"toolUseId": "t9", "content": [{"text": "found"}], "status": "success"}}]}},
        )
        assert parsed == ToolResult(tool_use_id="t9", text="found", is_error=False)

    def test_tool_result_declared_spa_shape(self) -> None:
        parsed = ev("tool_result", {"tool_result": {"toolUseId": "t3", "content": [{"text": "ok"}]}})
        assert parsed.tool_use_id == "t3"
        assert parsed.text == "ok"

    def test_tool_result_error_status(self) -> None:
        parsed = ev("tool_result", {"toolUseId": "t1", "error": "boom", "status": "error"})
        assert parsed.is_error is True
        assert parsed.text == "boom"

    def test_tool_error_event_name_marks_error(self) -> None:
        assert ev("tool_error", {"toolUseId": "t1", "result": "nope"}).is_error is True

    def test_json_only_tool_result_is_rendered(self) -> None:
        parsed = ev("tool_result", {"toolUseId": "t1", "content": [{"json": {"k": 1}}]})
        assert '"k": 1' in parsed.text

    def test_long_tool_result_is_truncated(self) -> None:
        parsed = ev("tool_result", {"toolUseId": "t1", "result": "x" * 50_000})
        assert len(parsed.text) == TOOL_RESULT_PREVIEW_CHARS

    def test_tool_progress(self) -> None:
        parsed = ev("tool_progress", {"message": "Fetching page 2", "toolName": "browser"})
        assert parsed == ToolProgress(message="Fetching page 2", tool_name="browser", tool_use_id="")

    def test_tool_approval_required(self) -> None:
        parsed = ev(
            "tool_approval_required",
            {"interruptId": "i1", "toolUseId": "t1", "toolName": "delete_file", "message": "Allow?"},
        )
        assert isinstance(parsed, ToolApprovalRequired)
        assert parsed.tool_name == "delete_file"

    def test_oauth_required_snake_and_camel(self) -> None:
        camel = ev("oauth_required", {"providerId": "google", "authorizationUrl": "https://x/y"})
        snake = ev("oauth_required", {"provider_id": "google", "authorization_url": "https://x/y"})
        assert camel == snake == OAuthRequired(provider_id="google", authorization_url="https://x/y")


class TestParsingSideChannels:
    def test_citation(self) -> None:
        parsed = ev("citation", {"documentId": "d1", "fileName": "handbook.pdf", "text": "excerpt", "assistantId": "a1"})
        assert parsed == CitationEvent(document_id="d1", file_name="handbook.pdf", text="excerpt", assistant_id="a1")

    def test_metadata_carries_context_window(self) -> None:
        parsed = ev("metadata", {"usage": {"inputTokens": 10, "outputTokens": 5}, "contextWindow": 200_000})
        assert isinstance(parsed, Metadata)
        assert parsed.usage.total_tokens == 15
        assert parsed.context_window == 200_000

    def test_metadata_without_context_window_is_none(self) -> None:
        assert ev("metadata", {"usage": {}}).context_window is None

    def test_metadata_summary(self) -> None:
        parsed = ev("metadata_summary", {"usage": {"inputTokens": 100, "outputTokens": 50}})
        assert isinstance(parsed, MetadataSummary)
        assert parsed.usage.input_tokens == 100

    def test_compaction(self) -> None:
        parsed = ev("compaction", {"previousCheckpoint": 1, "newCheckpoint": 5, "summarizedTurns": 4, "inputTokens": 900})
        assert parsed == Compaction(summarized_turns=4, previous_checkpoint=1, new_checkpoint=5, input_tokens=900)

    def test_artifact(self) -> None:
        parsed = ev(
            "artifact",
            {"artifactId": "a1", "title": "Chart", "contentType": "text/html", "version": 2, "action": "updated"},
        )
        assert isinstance(parsed, Artifact)
        assert (parsed.artifact_id, parsed.version, parsed.action) == ("a1", 2, "updated")

    def test_session_title(self) -> None:
        assert ev("session_title", {"sessionId": "s1", "title": "Tax questions"}) == SessionTitle(session_id="s1", title="Tax questions")

    def test_quota_warning(self) -> None:
        parsed = ev("quota_warning", {"message": "75% used", "warningLevel": "high", "percentageUsed": 75.0})
        assert isinstance(parsed, QuotaWarning)
        assert parsed.percentage_used == 75.0

    def test_quota_session_notice(self) -> None:
        parsed = ev("quota_session_notice", {"message": "big chat", "sessionId": "s1", "sessionCost": 1.25})
        assert isinstance(parsed, QuotaSessionNotice)
        assert parsed.session_cost == 1.25

    def test_quota_exceeded(self) -> None:
        parsed = ev("quota_exceeded", {"message": "stop", "resetInfo": "1 Sep", "periodType": "monthly"})
        assert isinstance(parsed, QuotaExceeded)
        assert parsed.reset_info == "1 Sep"


class TestParsingErrors:
    def test_error_event(self) -> None:
        assert ev("error", {"error": "bad"}).message == "bad"

    def test_stream_error_is_the_same_type(self) -> None:
        parsed = ev("stream_error", {"message": "throttled", "code": "429", "recoverable": True})
        assert isinstance(parsed, ErrorEvent)
        assert (parsed.code, parsed.recoverable) == ("429", True)

    def test_error_without_a_message_still_says_something(self) -> None:
        assert ev("error", {}).message

    def test_malformed_json_degrades_to_an_error(self) -> None:
        parsed = parse_agent_event("content_block_delta", "{not json")
        assert isinstance(parsed, ErrorEvent)
        assert "malformed JSON" in parsed.message

    def test_empty_data_is_not_an_error(self) -> None:
        assert isinstance(parse_agent_event("done", ""), Done)

    def test_non_object_json_is_tolerated(self) -> None:
        assert isinstance(parse_agent_event("message_stop", "[1,2]"), MessageStop)

    def test_unknown_event_is_kept_not_raised(self) -> None:
        """A newer server must not break an older client."""
        parsed = ev("some_future_event", {"x": 1})
        assert parsed == UnknownEvent(name="some_future_event", payload={"x": 1})


# =====================================================================
# Folding
# =====================================================================


class TestTextSelection:
    def test_single_message_turn(self) -> None:
        acc = fold(MessageStart(), TextDelta(index=0, text="Hello "), TextDelta(index=0, text="world"), MessageStop(), Done())
        assert acc.text == "Hello world"
        assert acc.ok is True

    def test_answer_is_the_last_message_not_the_concatenation(self) -> None:
        """A tool round trip closes one message and opens another.

        Concatenating would splice the pre-tool narration onto the answer.
        """
        acc = fold(
            MessageStart(),
            TextDelta(index=0, text="Let me look that up."),
            MessageStop(),
            ToolUse(tool_use_id="t1", name="search"),
            ToolResult(tool_use_id="t1", text="42"),
            MessageStart(),
            TextDelta(index=0, text="The answer is 42."),
            MessageStop(),
            Done(),
        )
        assert acc.text == "The answer is 42."

    def test_transcript_keeps_every_message(self) -> None:
        acc = fold(
            MessageStart(),
            TextDelta(index=0, text="First."),
            MessageStop(),
            MessageStart(),
            TextDelta(index=0, text="Second."),
            MessageStop(),
            Done(),
        )
        assert acc.transcript == "First.\n\nSecond."
        assert acc.text == "Second."

    def test_unterminated_buffer_still_counts(self) -> None:
        """A stream that ends without message_stop must not lose the answer."""
        acc = fold(MessageStart(), TextDelta(index=0, text="partial answer"))
        assert acc.text == "partial answer"

    def test_done_closes_the_open_message(self) -> None:
        acc = fold(MessageStart(), TextDelta(index=0, text="answer"), Done())
        assert acc.text == "answer"
        assert acc.finished is True

    def test_whitespace_only_message_is_skipped(self) -> None:
        acc = fold(
            MessageStart(),
            TextDelta(index=0, text="real answer"),
            MessageStop(),
            MessageStart(),
            TextDelta(index=0, text="   \n "),
            MessageStop(),
            Done(),
        )
        assert acc.text == "real answer"

    def test_empty_turn_has_no_text(self) -> None:
        acc = fold(MessageStart(), MessageStop(), Done())
        assert acc.text == ""
        assert acc.ok is False


class TestToolFolding:
    def test_call_and_result_fold_into_one_record(self) -> None:
        acc = fold(
            ContentBlockStart(index=1, block_type="tool_use", tool_use_id="t1", tool_name="search"),
            ToolUse(tool_use_id="t1", name="search", arguments={"q": "x"}),
            ToolResult(tool_use_id="t1", text="found it"),
        )
        assert len(acc.tool_calls) == 1
        call = acc.tool_calls[0]
        assert (call.name, call.arguments, call.result, call.finished) == ("search", {"q": "x"}, "found it", True)

    def test_repeated_tool_use_does_not_duplicate(self) -> None:
        """Arguments are re-emitted as they stream; one record per id."""
        acc = fold(
            ToolUse(tool_use_id="t1", name="search", arguments={}, partial=True),
            ToolUse(tool_use_id="t1", name="search", arguments={}, partial=True),
            ToolUse(tool_use_id="t1", name="search", arguments={"q": "final"}),
        )
        assert len(acc.tool_calls) == 1
        assert acc.tool_calls[0].arguments == {"q": "final"}

    def test_partial_reemit_does_not_wipe_known_arguments(self) -> None:
        acc = fold(
            ToolUse(tool_use_id="t1", name="s", arguments={"q": "good"}),
            ToolUse(tool_use_id="t1", name="s", arguments={}, partial=True),
        )
        assert acc.tool_calls[0].arguments == {"q": "good"}

    def test_name_from_block_start_survives_a_nameless_reemit(self) -> None:
        acc = fold(
            ContentBlockStart(index=1, block_type="tool_use", tool_use_id="t1", tool_name="web_search"),
            ToolUse(tool_use_id="t1", name="", arguments={"q": "x"}),
        )
        assert acc.tool_calls[0].name == "web_search"

    def test_two_tools_stay_separate(self) -> None:
        acc = fold(
            ToolUse(tool_use_id="t1", name="a"),
            ToolUse(tool_use_id="t2", name="b"),
            ToolResult(tool_use_id="t2", text="second"),
            ToolResult(tool_use_id="t1", text="first"),
        )
        assert [c.name for c in acc.tool_calls] == ["a", "b"]
        assert acc.tool_calls[0].result == "first"
        assert acc.tool_calls[1].result == "second"

    def test_result_without_an_id_attaches_to_the_open_call(self) -> None:
        """Some passthrough shapes omit the id; dropping the result would leave
        the call rendering as perpetually running."""
        acc = fold(ToolUse(tool_use_id="t1", name="a"), ToolResult(tool_use_id="", text="done"))
        assert acc.tool_calls[0].result == "done"

    def test_error_result_is_marked(self) -> None:
        acc = fold(ToolUse(tool_use_id="t1", name="a"), ToolResult(tool_use_id="t1", text="boom", is_error=True))
        assert acc.tool_calls[0].is_error is True

    def test_progress_lands_on_the_call(self) -> None:
        acc = fold(ToolUse(tool_use_id="t1", name="a"), ToolProgress(message="working", tool_use_id="t1"))
        assert acc.tool_calls[0].progress == "working"

    def test_orphan_result_is_dropped_not_crashed(self) -> None:
        acc = fold(ToolResult(tool_use_id="nope", text="x"))
        assert acc.tool_calls == []

    def test_tool_input_deltas_need_no_state(self) -> None:
        """Arguments come from `tool_use` re-emits, so deltas are inert here."""
        acc = fold(ToolUse(tool_use_id="t1", name="a"), ToolInputDelta(index=1, partial_json='{"q"'))
        assert acc.tool_calls[0].arguments == {}


class TestUsageFolding:
    def test_summary_wins_over_per_call_metadata(self) -> None:
        """`metadata` fires per LLM call; only the summary is a turn total."""
        from agentcore_tui.usage import Usage

        acc = fold(
            Metadata(usage=Usage(input_tokens=10, output_tokens=5)),
            Metadata(usage=Usage(input_tokens=20, output_tokens=8)),
            MetadataSummary(usage=Usage(input_tokens=30, output_tokens=13)),
        )
        assert acc.usage == Usage(input_tokens=30, output_tokens=13)

    def test_summary_wins_regardless_of_arrival_order(self) -> None:
        from agentcore_tui.usage import Usage

        acc = fold(
            MetadataSummary(usage=Usage(input_tokens=30, output_tokens=13)),
            Metadata(usage=Usage(input_tokens=20, output_tokens=8)),
        )
        assert acc.usage.input_tokens == 30

    def test_last_call_metadata_is_the_fallback(self) -> None:
        from agentcore_tui.usage import Usage

        acc = fold(
            Metadata(usage=Usage(input_tokens=10, output_tokens=5)),
            Metadata(usage=Usage(input_tokens=20, output_tokens=8)),
        )
        assert acc.usage == Usage(input_tokens=20, output_tokens=8)

    def test_context_window_is_retained(self) -> None:
        from agentcore_tui.usage import Usage

        acc = fold(Metadata(usage=Usage(), context_window=200_000), Metadata(usage=Usage()))
        assert acc.context_window == 200_000

    def test_no_metadata_leaves_usage_none(self) -> None:
        assert fold(Done()).usage is None


class TestSideChannelFolding:
    def test_citations_accumulate_in_order(self) -> None:
        acc = fold(
            CitationEvent(document_id="d1", file_name="a.pdf"),
            CitationEvent(document_id="d2", file_name="b.pdf"),
        )
        assert [c.file_name for c in acc.citations] == ["a.pdf", "b.pdf"]

    def test_artifacts_dedupe_keeping_the_highest_version(self) -> None:
        acc = fold(
            Artifact(artifact_id="a1", title="v1", version=1),
            Artifact(artifact_id="a1", title="v2", version=2, action="updated"),
            Artifact(artifact_id="a2", title="other", version=1),
        )
        assert len(acc.artifacts) == 2
        assert acc.artifacts[0].title == "v2"

    def test_older_artifact_version_does_not_overwrite(self) -> None:
        acc = fold(
            Artifact(artifact_id="a1", title="v3", version=3),
            Artifact(artifact_id="a1", title="v1", version=1),
        )
        assert acc.artifacts[0].title == "v3"

    def test_title_is_captured_even_after_done(self) -> None:
        """`session_title` may arrive after `done`; it must not be gated on it."""
        acc = fold(Done(), SessionTitle(session_id="s1", title="Late title"))
        assert acc.title == "Late title"

    def test_placeholder_empty_title_is_ignored(self) -> None:
        acc = fold(SessionTitle(session_id="s1", title=""))
        assert acc.title is None

    def test_compaction_totals_are_summed(self) -> None:
        acc = fold(Compaction(summarized_turns=3), Compaction(summarized_turns=2))
        assert acc.summarized_turns == 5

    def test_quota_notices_collect(self) -> None:
        acc = fold(QuotaWarning(message="75%"), QuotaSessionNotice(message="big"))
        assert len(acc.quota_notices) == 2
        assert acc.blocked is False

    def test_quota_exceeded_blocks_the_turn(self) -> None:
        acc = fold(QuotaExceeded(message="stop"), Done())
        assert acc.blocked is True
        assert acc.ok is False

    def test_interrupts_are_flagged(self) -> None:
        acc = fold(OAuthRequired(provider_id="google", authorization_url="https://x"))
        assert acc.interrupted is True
        assert acc.oauth_required[0].provider_id == "google"

    def test_approval_required_is_an_interrupt(self) -> None:
        acc = fold(ToolApprovalRequired(interrupt_id="i1", tool_use_id="t1", tool_name="rm"))
        assert acc.interrupted is True

    def test_a_clean_turn_is_not_interrupted(self) -> None:
        acc = fold(MessageStart(), TextDelta(index=0, text="hi"), MessageStop(), Done())
        assert acc.interrupted is False


class TestTurnOutcome:
    def test_truncation_is_detected(self) -> None:
        acc = fold(MessageStart(), TextDelta(index=0, text="cut"), MessageStop(stop_reason="max_tokens"), Done())
        assert acc.truncated is True

    def test_error_makes_the_turn_not_ok(self) -> None:
        acc = fold(MessageStart(), TextDelta(index=0, text="partial"), ErrorEvent(message="boom"))
        assert acc.error == "boom"
        assert acc.ok is False

    def test_reasoning_accumulates(self) -> None:
        acc = fold(Reasoning(text="step 1. "), Reasoning(text="step 2."))
        assert acc.reasoning == "step 1. step 2."

    def test_events_seen_counts_by_name(self) -> None:
        acc = fold(TextDelta(index=0, text="a"), TextDelta(index=0, text="b"), Done())
        assert acc.events_seen["TextDelta"] == 2
        assert acc.events_seen["Done"] == 1

    def test_ignored_events_are_counted_under_their_wire_name(self) -> None:
        """So a log line can show what was dropped and how often."""
        acc = fold(IgnoredEvent(name="message"), IgnoredEvent(name="message"))
        assert acc.events_seen["message"] == 2


class TestRealisticToolTurn:
    """An end-to-end fold of the shape a real tool-using turn takes."""

    def test_full_turn(self) -> None:
        from agentcore_tui.usage import Usage

        acc = fold(
            MessageStart(),
            TextDelta(index=0, text="I'll search for that."),
            ContentBlockStart(index=1, block_type="tool_use", tool_use_id="t1", tool_name="web_search"),
            ToolInputDelta(index=1, partial_json='{"query": "agen'),
            ToolUse(tool_use_id="t1", name="web_search", arguments={}, partial=True),
            ToolUse(tool_use_id="t1", name="web_search", arguments={"query": "agentcore"}),
            ContentBlockStop(index=1),
            MessageStop(stop_reason="tool_use"),
            Metadata(usage=Usage(input_tokens=100, output_tokens=20)),
            ToolResult(tool_use_id="t1", text="AgentCore is a platform."),
            MessageStart(),
            TextDelta(index=0, text="AgentCore is a platform for agents."),
            MessageStop(stop_reason="end_turn"),
            Metadata(usage=Usage(input_tokens=300, output_tokens=40)),
            CitationEvent(document_id="d1", file_name="overview.pdf", text="…"),
            MetadataSummary(usage=Usage(input_tokens=400, output_tokens=60)),
            Compaction(summarized_turns=2),
            SessionTitle(session_id="s1", title="About AgentCore"),
            Done(),
        )

        assert acc.text == "AgentCore is a platform for agents."
        assert acc.ok is True
        assert acc.finished is True
        assert acc.stop_reason == "end_turn"
        assert acc.usage == Usage(input_tokens=400, output_tokens=60)
        assert acc.title == "About AgentCore"
        assert acc.summarized_turns == 2
        assert len(acc.citations) == 1

        assert len(acc.tool_calls) == 1
        call = acc.tool_calls[0]
        assert (call.name, call.arguments, call.result) == (
            "web_search",
            {"query": "agentcore"},
            "AgentCore is a platform.",
        )
