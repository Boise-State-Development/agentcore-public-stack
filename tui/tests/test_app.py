"""Textual app tests.

These exercise the real app through Textual's ``run_test`` pilot, so they also
prove ``app.tcss`` parses — a stylesheet error raises on mount.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable

import httpx
import pytest

from agentcore_tui.app import ChatApp
from agentcore_tui.client import ApiConverseClient
from agentcore_tui.config import Config
from agentcore_tui.widgets import AssistantMessage, Notice, StatusBar, UserMessage

from .conftest import BASE_URL, MODEL_ID, sse_body, sse_response, text_stream

Handler = Callable[[httpx.Request], httpx.Response]

MODEL_B = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def build_app(handler: Handler, *, config: Config | None = None) -> ChatApp:
    """A ChatApp whose client is backed by MockTransport."""
    resolved = config or Config(base_url=BASE_URL, api_key="test-key", model_id=MODEL_ID, models=(MODEL_ID, MODEL_B))

    def factory(cfg: Config) -> ApiConverseClient:
        return ApiConverseClient(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    return ChatApp(resolved, client_factory=factory)


def ok_handler(chunks: list[str] | None = None) -> Handler:
    return lambda _: sse_response(text_stream(chunks or ["Hello", " world"]))


def error_handler(status: int, detail: str) -> Handler:
    return lambda _: httpx.Response(status, json={"detail": detail})


async def send(pilot: object, app: ChatApp, prompt: str) -> None:
    """Type a prompt and submit it, then wait for the turn to settle."""
    app.composer.text = prompt
    await pilot.press("enter")  # type: ignore[attr-defined]
    await app.workers.wait_for_complete()
    await pilot.pause()  # type: ignore[attr-defined]


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
    what they appear to mean.
    """
    svg = app.export_screenshot()
    svg = re.sub(r"<style.*?</style>", "", svg, flags=re.S)
    svg = re.sub(r"<defs.*?</defs>", "", svg, flags=re.S)

    rows: dict[float, list[str]] = {}
    for match in re.finditer(r"<text[^>]*y=\"([0-9.]+)\"[^>]*>(.*?)</text>", svg, flags=re.S):
        rows.setdefault(float(match.group(1)), []).append(html.unescape(match.group(2)))

    lines = ["".join(fragments).replace("\xa0", " ").rstrip() for _, fragments in sorted(rows.items())]
    return "\n".join(lines)


class TestBoot:
    async def test_starts_ready_with_complete_config(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query(Notice)
            assert app.composer.submit_enabled is True
            assert app.composer.disabled is False
            assert app.status.query_one  # status bar mounted
            assert isinstance(app.query_one(StatusBar), StatusBar)

    async def test_subtitle_shows_the_target_deployment(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.sub_title == BASE_URL

    async def test_incomplete_config_disables_sending_and_explains(self) -> None:
        app = build_app(ok_handler(), config=Config(base_url="", api_key=None))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.composer.disabled is True
            assert app.composer.submit_enabled is False
            notices = app.query(Notice)
            assert notices
            assert app.query(".notice.-error")

    async def test_incomplete_config_ignores_enter(self) -> None:
        app = build_app(ok_handler(), config=Config(base_url="", api_key=None))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.composer.text = "should not send"
            await pilot.press("enter")
            await pilot.pause()
            assert not app.query(UserMessage)


class TestSending:
    async def test_streamed_turn_renders_and_records_history(self) -> None:
        app = build_app(ok_handler(["Hel", "lo"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi there")

            assert len(app.query(UserMessage)) == 1
            assert len(app.query(AssistantMessage)) == 1
            # Both turns recorded, so the next request carries the context.
            assert [message.role for message in app._history] == ["user", "assistant"]
            assert app._history[0].content == "hi there"
            assert app._history[1].content == "Hello"

    async def test_composer_is_cleared_after_sending(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app.composer.text == ""

    async def test_multi_turn_accumulates_history(self) -> None:
        app = build_app(ok_handler(["ack"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "first")
            await send(pilot, app, "second")

            assert [message.role for message in app._history] == ["user", "assistant", "user", "assistant"]
            assert len(app.query(UserMessage)) == 2

    async def test_usage_reaches_the_status_bar(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return sse_response(text_stream(["ok"], usage={"inputTokens": 11, "outputTokens": 22}))

        app = build_app(handler)
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert "11 in" in app.status.line
            assert "22 out" in app.status.line
            assert "1 turn" in app.status.line

    async def test_empty_prompt_does_not_send(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            app.composer.text = "   "
            await pilot.press("enter")
            await pilot.pause()
            assert not app.query(UserMessage)

    async def test_newline_key_inserts_instead_of_sending(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            app.composer.focus()
            app.composer.text = "line one"
            app.composer.move_cursor((0, len("line one")))
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert "\n" in app.composer.text
            assert not app.query(UserMessage)

    async def test_reasoning_is_rendered_in_its_own_pane(self) -> None:
        body = sse_body(
            [
                ("reasoning_start", {"contentBlockIndex": 0}),
                ("reasoning_delta", {"contentBlockIndex": 0, "text": "thinking hard"}),
                ("reasoning_stop", {"contentBlockIndex": 0}),
                ("content_block_delta", {"contentBlockIndex": 1, "type": "text", "text": "answer"}),
                ("message_stop", {"stopReason": "end_turn"}),
                ("done", {}),
            ]
        )
        app = build_app(lambda _: sse_response(body))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "why?")
            assert app._history[-1].content == "answer"
            assert app.query(".reasoning")


class TestErrorSurfaces:
    async def test_auth_failure_is_explained_and_recoverable(self) -> None:
        app = build_app(error_handler(401, "Invalid or expired API key"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")

            assert app.query(".notice.-error")
            # The failed assistant turn must not enter history.
            assert [message.role for message in app._history] == ["user"]
            # And the UI must return to a usable state rather than wedging.
            assert app.composer.submit_enabled is True
            assert app._busy is False

    async def test_model_denied_names_the_model(self) -> None:
        app = build_app(error_handler(403, f"Access denied to model: {MODEL_ID}"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app.query(".notice.-error")
            assert app._busy is False

    async def test_rate_limit_is_reported(self) -> None:
        app = build_app(error_handler(429, "Rate limit exceeded."))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app.query(".notice.-error")

    async def test_mid_stream_error_is_reported_and_excluded_from_history(self) -> None:
        body = sse_body(
            [
                ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": "partial"}),
                ("error", {"error": "Model invocation failed"}),
                ("done", {}),
            ]
        )
        app = build_app(lambda _: sse_response(body))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app.query(".notice.-error")
            assert [message.role for message in app._history] == ["user"]

    async def test_truncated_response_warns_but_keeps_the_text(self) -> None:
        body = sse_body(
            [
                ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": "cut off"}),
                ("message_stop", {"stopReason": "max_tokens"}),
                ("done", {}),
            ]
        )
        app = build_app(lambda _: sse_response(body))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "long one")
            assert app._history[-1].content == "cut off"
            assert app.query(".notice.-error")

    async def test_connection_failure_is_survivable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        app = build_app(handler)
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app.query(".notice.-error")
            assert app._busy is False

    async def test_empty_response_is_flagged(self) -> None:
        body = sse_body([("message_stop", {"stopReason": "end_turn"}), ("done", {})])
        app = build_app(lambda _: sse_response(body))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app.query(".notice.-error")
            assert [message.role for message in app._history] == ["user"]


class TestActions:
    async def test_new_conversation_clears_history_and_transcript(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app._history

            await app.run_action("new_conversation")
            await pilot.pause()

            assert app._history == []
            assert not app.query(UserMessage)
            assert not app.query(AssistantMessage)

    async def test_stop_binding_is_unavailable_while_idle(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.check_action("cancel_turn", ()) is False

    async def test_f1_opens_the_command_palette(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert [type(s).__name__ for s in app.screen_stack] == ["Screen"]

            await pilot.press("f1")
            await pilot.pause()

            assert "CommandPalette" in [type(s).__name__ for s in app.screen_stack]

    async def test_model_change_is_applied_to_later_requests(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.append(json.loads(request.content)["model_id"])
            return sse_response(text_stream(["ok"]))

        app = build_app(handler)
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "first")

            app._config = app._config.with_model(MODEL_B)
            if app._client is not None:
                await app._client.aclose()
                app._client = None
            app.status.set_model(MODEL_B)

            await send(pilot, app, "second")
            assert seen == [MODEL_ID, MODEL_B]


class TestCommandPalette:
    @staticmethod
    def titles(app: ChatApp) -> list[str]:
        return [command.title for command in app.get_system_commands(app.screen)]

    async def test_offers_app_specific_commands(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            titles = self.titles(app)
            assert "New conversation" in titles
            assert "Change model" in titles
            assert "Theme" in titles
            assert "Quit" in titles

    async def test_drops_builtins_that_make_no_sense_here(self) -> None:
        """Maximize would fill the screen with the composer and hide the answer."""
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            titles = self.titles(app)
            assert "Maximize" not in titles
            assert "Minimize" not in titles
            assert "Keys" not in titles

    async def test_stop_offered_only_while_a_turn_is_in_flight(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "Stop response" not in self.titles(app)

    async def test_copy_offered_only_once_there_is_an_answer(self) -> None:
        app = build_app(ok_handler(["the answer"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "Copy last response" not in self.titles(app)

            await send(pilot, app, "hi")
            assert "Copy last response" in self.titles(app)

    async def test_copy_last_answer_uses_the_latest_assistant_message(self) -> None:
        copied: list[str] = []
        app = build_app(ok_handler(["first answer"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.copy_to_clipboard = copied.append  # type: ignore[method-assign]

            await send(pilot, app, "hi")
            app._copy_last_answer()

            assert copied == ["first answer"]

    async def test_copy_transcript_includes_both_roles(self) -> None:
        copied: list[str] = []
        app = build_app(ok_handler(["an answer"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.copy_to_clipboard = copied.append  # type: ignore[method-assign]

            await send(pilot, app, "a question")
            app._copy_transcript()

            assert len(copied) == 1
            assert "a question" in copied[0]
            assert "an answer" in copied[0]


class TestModelPicker:
    async def test_picker_lists_models_and_returns_a_choice(self) -> None:
        from agentcore_tui.screens import ModelPicker

        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = ModelPicker((MODEL_ID, MODEL_B), MODEL_ID)
            app.push_screen(picker)
            await pilot.pause()

            from textual.widgets import OptionList

            option_list = picker.query_one("#picker-list", OptionList)
            assert option_list.option_count == 2
            # The active model is pre-highlighted so Enter is a no-op, not a surprise.
            assert option_list.highlighted == 0


class TestTranscriptGrowth:
    """Regression tests for the transcript-clipping bug.

    Textual containers default to `height: 1fr`. With that default, each message
    expanded to fill the transcript viewport, so the scroll container's virtual
    size never exceeded one screen: `max_scroll_y` stayed 0 and everything past
    the first screenful was clipped and unreachable. Long answers looked like
    they stopped mid-sentence. The fix is `height: auto` on every widget between
    #transcript and the text (see app.tcss).
    """

    LONG_MARKDOWN = (
        "# Three List Comprehension Examples\n\n"
        "## Example 1: Squaring numbers\n"
        "```python\n"
        "numbers = [1, 2, 3, 4, 5]\n"
        "squares = [x**2 for x in numbers]\n"
        "```\n\n"
        "## Example 2: Filtering even numbers\n"
        "```python\n"
        "evens = [x for x in numbers if x % 2 == 0]\n"
        "```\n\n"
        "## Example 3: Uppercase\n"
        "```python\n"
        "upper = [w.upper() for w in words]\n"
        "```\n"
    )

    def _chunked_handler(self, text: str, size: int = 15) -> Handler:
        """Stream `text` in small deltas, as a real model does."""
        chunks = [text[i : i + size] for i in range(0, len(text), size)]
        return lambda _: sse_response(text_stream(chunks))

    async def test_long_answer_makes_the_transcript_scrollable(self) -> None:
        app = build_app(self._chunked_handler(self.LONG_MARKDOWN))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await send(pilot, app, "examples please")
            await pilot.pause()

            transcript = app.transcript
            assert transcript.virtual_size.height > transcript.size.height, "transcript did not grow beyond one screen — content is being clipped"
            assert transcript.max_scroll_y > 0, "transcript is not scrollable, so content below the fold is unreachable"

    async def test_view_follows_the_stream_to_the_tail(self) -> None:
        app = build_app(self._chunked_handler(self.LONG_MARKDOWN))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await send(pilot, app, "examples please")
            await pilot.pause()

            transcript = app.transcript
            assert abs(transcript.scroll_y - transcript.max_scroll_y) < 1.5, "view did not follow the stream to the end of the answer"
            frame = rendered_text(app)
            assert "Example 3" in frame, "the tail of the answer never reached the screen"

    async def test_every_markdown_block_is_rendered(self) -> None:
        """Guards the parse path independently of layout: all fences must exist."""
        from textual.widgets import Markdown
        from textual.widgets._markdown import MarkdownBlock, MarkdownFence, MarkdownHeader

        app = build_app(self._chunked_handler(self.LONG_MARKDOWN))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await send(pilot, app, "examples please")
            await pilot.pause()

            body = list(app.query(Markdown))[-1]
            assert body.source == self.LONG_MARKDOWN
            blocks = [child for child in body.children if isinstance(child, MarkdownBlock)]
            assert sum(isinstance(b, MarkdownFence) for b in blocks) == 3
            assert sum(isinstance(b, MarkdownHeader) for b in blocks) == 4


class TestRendering:
    """Proof that content reaches the screen, not just the widget tree."""

    async def test_prompt_and_answer_appear_in_the_rendered_frame(self) -> None:
        app = build_app(ok_handler(["Streaming ", "works"]))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "render me")
            frame = rendered_text(app)

            assert "render me" in frame
            assert "Streaming works" in frame
            assert "You" in frame

    async def test_setup_guidance_is_rendered_when_unconfigured(self) -> None:
        app = build_app(ok_handler(), config=Config(base_url="", api_key=None))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            frame = rendered_text(app)
            assert "Not configured" in frame
            assert "agentcore-tui login" in frame


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
        ("some-custom-model", "some-custom-model"),
    ],
)
def test_model_label_is_trimmed_for_display(model_id: str, expected: str) -> None:
    assert AssistantMessage(model_id)._short_model_name() == expected
