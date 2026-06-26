"""Tests for the conversation transcript renderer."""

from __future__ import annotations

from typing import List, Optional

import pytest

from apis.shared.sessions.models import Citation, MessageContent, MessageResponse

from apis.app_api.export_targets.models import ExportFormat, ExportInclude
from apis.app_api.export_targets.render import render_transcript


def _msg(role: str, blocks: List[MessageContent], created_at: str = "") -> MessageResponse:
    return MessageResponse(id=f"m-{role}", role=role, content=blocks, createdAt=created_at)


def _text(role: str, text: str, created_at: str = "") -> MessageResponse:
    return _msg(role, [MessageContent(type="text", text=text)], created_at)


class TestMarkdownStructure:
    def test_title_and_role_headings(self):
        msgs = [_text("user", "hi"), _text("assistant", "hello")]
        out = render_transcript("My Chat", msgs, ExportFormat.MARKDOWN)
        body = out.content.decode()
        assert out.mime_type == "text/markdown"
        assert out.suggested_name == "My Chat.md"
        assert body.startswith("# My Chat")
        assert "## User" in body
        assert "## Assistant" in body
        assert "hi" in body and "hello" in body

    def test_empty_messages_still_renders_title(self):
        out = render_transcript("Empty", [], ExportFormat.MARKDOWN)
        assert out.content.decode().strip() == "# Empty"

    def test_blank_title_falls_back(self):
        out = render_transcript("   ", [_text("user", "x")], ExportFormat.MARKDOWN)
        assert out.content.decode().startswith("# Conversation")


class TestIncludeFlags:
    def test_tool_calls_included_by_default_and_omittable(self):
        msgs = [
            _msg(
                "assistant",
                [
                    MessageContent(type="text", text="done"),
                    MessageContent(type="toolUse", toolUse={"name": "calc", "input": {"x": 1}}),
                    MessageContent(
                        type="toolResult",
                        toolResult={"status": "success", "content": [{"text": "2"}]},
                    ),
                ],
            )
        ]
        with_tools = render_transcript("t", msgs, ExportFormat.MARKDOWN).content.decode()
        assert "Tool call: `calc`" in with_tools
        assert "Tool result" in with_tools

        without = render_transcript(
            "t", msgs, ExportFormat.MARKDOWN, ExportInclude(tool_calls=False)
        ).content.decode()
        assert "Tool call" not in without
        assert "done" in without  # text still present

    def test_reasoning_off_by_default_on_when_requested(self):
        msgs = [
            _msg(
                "assistant",
                [
                    MessageContent(
                        type="reasoningContent",
                        reasoningContent={"reasoningText": {"text": "let me think"}},
                    ),
                    MessageContent(type="text", text="answer"),
                ],
            )
        ]
        default = render_transcript("t", msgs, ExportFormat.MARKDOWN).content.decode()
        assert "let me think" not in default

        on = render_transcript(
            "t", msgs, ExportFormat.MARKDOWN, ExportInclude(reasoning=True)
        ).content.decode()
        assert "let me think" in on
        assert "Reasoning" in on

    def test_timestamps_appended_to_heading_when_requested(self):
        msgs = [_text("user", "hi", created_at="2026-06-25T12:00:00Z")]
        off = render_transcript("t", msgs, ExportFormat.MARKDOWN).content.decode()
        assert "2026-06-25" not in off
        on = render_transcript(
            "t", msgs, ExportFormat.MARKDOWN, ExportInclude(timestamps=True)
        ).content.decode()
        assert "2026-06-25T12:00:00Z" in on

    def test_user_messages_can_be_excluded(self):
        msgs = [_text("user", "secret prompt"), _text("assistant", "reply")]
        out = render_transcript(
            "t", msgs, ExportFormat.MARKDOWN, ExportInclude(user_messages=False)
        ).content.decode()
        assert "secret prompt" not in out
        assert "reply" in out

    def test_images_embedded_as_data_uri_and_omittable(self):
        msgs = [
            _msg(
                "user",
                [MessageContent(type="image", image={"format": "png", "source": {"bytes": "QUJD"}})],
            )
        ]
        on = render_transcript("t", msgs, ExportFormat.MARKDOWN).content.decode()
        assert "data:image/png;base64,QUJD" in on
        off = render_transcript(
            "t", msgs, ExportFormat.MARKDOWN, ExportInclude(images=False)
        ).content.decode()
        assert "data:image" not in off

    def test_document_blocks_become_placeholder(self):
        msgs = [
            _msg("user", [MessageContent(type="document", document={"name": "spec.pdf"})])
        ]
        out = render_transcript("t", msgs, ExportFormat.MARKDOWN).content.decode()
        assert "[attached document: spec.pdf]" in out

    def test_citations_rendered_when_present(self):
        msg = MessageResponse(
            id="m1",
            role="assistant",
            content=[MessageContent(type="text", text="answer")],
            createdAt="",
            citations=[
                Citation(assistantId="a1", documentId="d1", fileName="handbook.pdf", text="x")
            ],
        )
        out = render_transcript("t", [msg], ExportFormat.MARKDOWN).content.decode()
        assert "Sources:" in out and "handbook.pdf" in out

        off = render_transcript(
            "t", [msg], ExportFormat.MARKDOWN, ExportInclude(citations=False)
        ).content.decode()
        assert "handbook.pdf" not in off


class TestGoogleDocHtml:
    def test_markdown_maps_to_html_styling(self):
        msgs = [_text("assistant", "# Heading\n\n**bold** and a list:\n\n- one\n- two")]
        out = render_transcript("Doc", msgs, ExportFormat.GOOGLE_DOC)
        html = out.content.decode()
        assert out.mime_type == "text/html"
        assert out.suggested_name == "Doc"
        assert "<strong>bold</strong>" in html
        assert "<li>one</li>" in html
        assert "<title>Doc</title>" in html

    def test_table_rule_enabled(self):
        table = "| a | b |\n| - | - |\n| 1 | 2 |"
        out = render_transcript("t", [_text("assistant", table)], ExportFormat.GOOGLE_DOC)
        assert "<table>" in out.content.decode()

    def test_raw_html_in_message_is_escaped_not_injected(self):
        out = render_transcript(
            "t", [_text("user", "<script>alert(1)</script>")], ExportFormat.GOOGLE_DOC
        )
        html = out.content.decode()
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_html_title_is_escaped(self):
        out = render_transcript(
            "<b>x</b>", [_text("user", "hi")], ExportFormat.GOOGLE_DOC
        )
        assert "<title>&lt;b&gt;x&lt;/b&gt;</title>" in out.content.decode()


class TestUnsupportedFormat:
    def test_pdf_raises(self):
        with pytest.raises(ValueError):
            render_transcript("t", [_text("user", "x")], ExportFormat.PDF)
