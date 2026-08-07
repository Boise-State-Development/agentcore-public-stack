"""Agent-content widget tests.

Mounted into the real chat transcript through Textual's ``run_test`` pilot, so
these also prove the new ``app.tcss`` rules parse — a stylesheet error raises on
mount — and that the widgets size to content rather than to the viewport.

That last point is the one worth the machinery. Textual containers default to
``height: 1fr``; a single ``1fr`` widget in the transcript makes the whole thing
unscrollable and silently clips every answer past the first screenful. Asserting
on rendered text alone would not catch it.
"""

from __future__ import annotations

from agentcore_tui.client.agent_events import (
    Artifact,
    CitationEvent,
    OAuthRequired,
    QuotaExceeded,
    QuotaSessionNotice,
    QuotaWarning,
    ToolApprovalRequired,
    ToolCallRecord,
)
from agentcore_tui.widgets import (
    ArtifactCard,
    Citations,
    CompactionNotice,
    InterruptNotice,
    QuotaNotice,
    ToolCall,
    quota_notice_for,
)

from .conftest import build_app, ok_handler, rendered_text


async def render(widget) -> str:
    """Rendered screen text with ``widget`` mounted in the transcript."""
    app = build_app(ok_handler())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.chat.transcript.mount(widget)
        await pilot.pause()
        return rendered_text(app)


# =====================================================================
# ToolCall
# =====================================================================


class TestToolCall:
    async def test_running_call_shows_the_name(self) -> None:
        record = ToolCallRecord(tool_use_id="t1", name="web_search")
        text = await render(ToolCall(record))
        assert "web_search" in text

    async def test_running_call_is_not_marked_finished(self) -> None:
        record = ToolCallRecord(tool_use_id="t1", name="web_search")
        widget = ToolCall(record)
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.chat.transcript.mount(widget)
            await pilot.pause()
            assert not widget.has_class("-finished")
            assert not widget.has_class("-error")

    async def test_progress_appears_in_the_header(self) -> None:
        record = ToolCallRecord(tool_use_id="t1", name="browser", progress="Fetching page 2")
        text = await render(ToolCall(record))
        assert "Fetching page 2" in text

    async def test_refresh_reflects_a_completed_record(self) -> None:
        """The widget shares the accumulator's record, so an update is in place."""
        record = ToolCallRecord(tool_use_id="t1", name="web_search")
        widget = ToolCall(record)
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.chat.transcript.mount(widget)
            await pilot.pause()

            record.result = "found it"
            widget.refresh_from_record()
            await pilot.pause()

            assert widget.has_class("-finished")
            assert not widget.has_class("-error")

    async def test_refresh_marks_a_failed_record(self) -> None:
        record = ToolCallRecord(tool_use_id="t1", name="web_search")
        widget = ToolCall(record)
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.chat.transcript.mount(widget)
            await pilot.pause()

            record.result = "boom"
            record.is_error = True
            widget.refresh_from_record()
            await pilot.pause()

            assert widget.has_class("-error")
            assert "failed" in rendered_text(app)

    async def test_arguments_are_rendered_as_json(self) -> None:
        record = ToolCallRecord(tool_use_id="t1", name="s", arguments={"query": "agentcore"})
        widget = ToolCall(record)
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.chat.transcript.mount(widget)
            await pilot.pause()
            assert '"query"' in widget._args_text()
            assert "agentcore" in widget._args_text()

    async def test_no_arguments_says_so(self) -> None:
        widget = ToolCall(ToolCallRecord(tool_use_id="t1", name="ping"))
        assert widget._args_text() == "Arguments: none"

    async def test_unserialisable_arguments_do_not_raise(self) -> None:
        """Arguments come straight off the wire; a repr beats an exception."""
        widget = ToolCall(ToolCallRecord(tool_use_id="t1", name="s", arguments={"when": object()}))
        assert "Arguments:" in widget._args_text()

    async def test_huge_arguments_are_summarised(self) -> None:
        record = ToolCallRecord(tool_use_id="t1", name="s", arguments={"doc": "x" * 5000})
        widget = ToolCall(record)
        rendered = widget._args_text()
        assert "characters total" in rendered
        assert len(rendered) < 2000

    async def test_long_result_is_truncated(self) -> None:
        record = ToolCallRecord(tool_use_id="t1", name="s", result="y" * 9000)
        widget = ToolCall(record)
        assert "characters total" in widget._result_text()

    async def test_empty_result_is_distinguished_from_running(self) -> None:
        running = ToolCall(ToolCallRecord(tool_use_id="t1", name="s"))
        empty = ToolCall(ToolCallRecord(tool_use_id="t2", name="s", result=""))
        assert "still running" in running._result_text()
        assert "empty" in empty._result_text()

    async def test_missing_name_falls_back(self) -> None:
        text = await render(ToolCall(ToolCallRecord(tool_use_id="t1", name="")))
        assert "tool" in text


# =====================================================================
# Citations
# =====================================================================


class TestCitations:
    async def test_counts_sources(self) -> None:
        text = await render(
            Citations(
                [
                    CitationEvent(document_id="d1", file_name="handbook.pdf", text="a"),
                    CitationEvent(document_id="d2", file_name="policy.pdf", text="b"),
                ]
            )
        )
        assert "2 sources" in text

    async def test_single_source_is_singular(self) -> None:
        text = await render(Citations([CitationEvent(document_id="d1", file_name="one.pdf")]))
        assert "1 source" in text
        assert "1 sources" not in text

    async def test_lists_file_names(self) -> None:
        text = await render(Citations([CitationEvent(document_id="d1", file_name="handbook.pdf")]))
        assert "handbook.pdf" in text

    async def test_repeated_document_is_listed_once(self) -> None:
        """Several excerpts from one document is the common case."""
        widget = Citations(
            [
                CitationEvent(document_id="d1", file_name="handbook.pdf", text="first"),
                CitationEvent(document_id="d1", file_name="handbook.pdf", text="second"),
            ]
        )
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.chat.transcript.mount(widget)
            await pilot.pause()
            sources = widget.query(".citation-source")
            assert len(sources) == 1

    async def test_falls_back_to_document_id(self) -> None:
        text = await render(Citations([CitationEvent(document_id="doc-42", file_name="")]))
        assert "doc-42" in text

    async def test_citations_without_excerpts_have_no_detail_pane(self) -> None:
        widget = Citations([CitationEvent(document_id="d1", file_name="a.pdf", text="")])
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.chat.transcript.mount(widget)
            await pilot.pause()
            assert not widget.query(".citations-detail")


# =====================================================================
# Quota
# =====================================================================


class TestQuotaNotice:
    async def test_warning_uses_the_server_message(self) -> None:
        text = await render(QuotaNotice(QuotaWarning(message="You have used 75% of your quota.")))
        assert "75% of your quota" in text

    async def test_warning_shows_the_numbers(self) -> None:
        text = await render(QuotaNotice(QuotaWarning(message="Heads up", current_usage=7.5, quota_limit=10.0, remaining=2.5)))
        assert "$7.50" in text
        assert "$10.00" in text

    async def test_exceeded_is_an_error(self) -> None:
        widget = QuotaNotice(QuotaExceeded(message="Limit reached", reset_info="1 September"))
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.chat.transcript.mount(widget)
            await pilot.pause()
            assert widget.has_class("-error")
            assert "1 September" in rendered_text(app)

    async def test_warning_is_not_an_error(self) -> None:
        widget = QuotaNotice(QuotaWarning(message="Heads up"))
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.chat.transcript.mount(widget)
            await pilot.pause()
            assert not widget.has_class("-error")

    async def test_session_notice_names_the_conversation_share(self) -> None:
        text = await render(
            QuotaNotice(
                QuotaSessionNotice(
                    message="This chat is expensive",
                    session_cost=2.5,
                    quota_limit=10.0,
                    session_percentage_of_limit=25.0,
                )
            )
        )
        assert "$2.50" in text
        assert "25%" in text

    async def test_title_falls_back_when_the_server_sends_no_message(self) -> None:
        text = await render(QuotaNotice(QuotaWarning(message="")))
        assert "quota" in text.lower()

    def test_helper_builds_for_quota_events_only(self) -> None:
        assert isinstance(quota_notice_for(QuotaWarning(message="x")), QuotaNotice)
        assert isinstance(quota_notice_for(QuotaExceeded(message="x")), QuotaNotice)
        assert isinstance(quota_notice_for(QuotaSessionNotice(message="x")), QuotaNotice)
        assert quota_notice_for(Artifact(artifact_id="a", title="t")) is None


# =====================================================================
# Compaction
# =====================================================================


class TestCompactionNotice:
    async def test_reports_the_turn_count(self) -> None:
        text = await render(CompactionNotice(4))
        assert "4 turns" in text

    async def test_single_turn_is_singular(self) -> None:
        text = await render(CompactionNotice(1))
        assert "1 turn" in text
        assert "1 turns" not in text

    async def test_explains_the_consequence(self) -> None:
        """It changes behaviour, so say what changed."""
        text = await render(CompactionNotice(3))
        assert "summary" in text.lower()


# =====================================================================
# Artifacts
# =====================================================================


class TestArtifactCard:
    async def test_shows_title_and_type(self) -> None:
        text = await render(ArtifactCard(Artifact(artifact_id="a1", title="Quarterly chart", content_type="text/html")))
        assert "Quarterly chart" in text
        assert "text/html" in text

    async def test_created_and_updated_read_differently(self) -> None:
        created = await render(ArtifactCard(Artifact(artifact_id="a1", title="T", action="created")))
        updated = await render(ArtifactCard(Artifact(artifact_id="a2", title="T", action="updated")))
        assert "Created artifact" in created
        assert "Updated artifact" in updated

    async def test_shows_the_version(self) -> None:
        text = await render(ArtifactCard(Artifact(artifact_id="a1", title="T", version=3)))
        assert "v3" in text

    async def test_says_where_to_view_it(self) -> None:
        """A terminal cannot render the HTML; do not pretend otherwise."""
        text = await render(ArtifactCard(Artifact(artifact_id="a1", title="T")))
        assert "web app" in text

    async def test_untitled_artifact_still_renders(self) -> None:
        text = await render(ArtifactCard(Artifact(artifact_id="a1", title="")))
        assert "Untitled" in text


# =====================================================================
# Interrupts
# =====================================================================


class TestInterruptNotice:
    async def test_oauth_shows_the_url_and_the_provider(self) -> None:
        text = await render(InterruptNotice(OAuthRequired(provider_id="google", authorization_url="https://accounts.example/auth")))
        assert "google" in text
        assert "https://accounts.example/auth" in text

    async def test_oauth_says_what_to_do(self) -> None:
        text = await render(InterruptNotice(OAuthRequired(provider_id="google", authorization_url="https://x/y")))
        assert "Authorization needed" in text

    async def test_oauth_without_a_provider_still_reads(self) -> None:
        text = await render(InterruptNotice(OAuthRequired(provider_id="", authorization_url="https://x/y")))
        assert "external service" in text

    async def test_approval_names_the_tool(self) -> None:
        text = await render(InterruptNotice(ToolApprovalRequired(interrupt_id="i1", tool_use_id="t1", tool_name="delete_file", message="")))
        assert "delete_file" in text

    async def test_approval_is_honest_about_the_limitation(self) -> None:
        """Approving is not implemented in the terminal; say so rather than hang."""
        text = await render(InterruptNotice(ToolApprovalRequired(interrupt_id="i1", tool_use_id="t1", tool_name="rm")))
        assert "web app" in text

    async def test_approval_prefers_the_server_message(self) -> None:
        text = await render(
            InterruptNotice(ToolApprovalRequired(interrupt_id="i1", tool_use_id="t1", tool_name="rm", message="Allow deleting build/?"))
        )
        assert "Allow deleting build/?" in text


# =====================================================================
# The transcript must stay scrollable
# =====================================================================


class TestTranscriptGrowth:
    """The regression that matters: a `1fr` widget makes the transcript unscrollable.

    ``max_scroll_y == 0`` while content exceeds the viewport is exactly the
    symptom that shipped once, presenting as answers truncating mid-sentence.
    """

    async def test_each_widget_sizes_to_content(self) -> None:
        widgets = [
            ToolCall(ToolCallRecord(tool_use_id="t1", name="search", arguments={"q": "x"}, result="ok")),
            Citations([CitationEvent(document_id="d1", file_name="a.pdf", text="excerpt")]),
            QuotaNotice(QuotaWarning(message="Heads up", current_usage=1.0, quota_limit=2.0)),
            CompactionNotice(3),
            ArtifactCard(Artifact(artifact_id="a1", title="Chart", content_type="text/html")),
            InterruptNotice(OAuthRequired(provider_id="google", authorization_url="https://x/y")),
        ]
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            transcript = app.chat.transcript
            viewport = transcript.size.height

            for widget in widgets:
                await app.chat.transcript.mount(widget)
                await pilot.pause()
                # No single widget may claim the whole viewport.
                assert widget.size.height < viewport, f"{type(widget).__name__} is filling the viewport"

    async def test_many_widgets_make_the_transcript_scrollable(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            transcript = app.chat.transcript
            for index in range(12):
                await transcript.mount(ToolCall(ToolCallRecord(tool_use_id=f"t{index}", name=f"tool_{index}", result="ok")))
            await pilot.pause()

            assert transcript.virtual_size.height > transcript.size.height
            assert transcript.max_scroll_y > 0, "transcript content exceeds the viewport but cannot scroll"
