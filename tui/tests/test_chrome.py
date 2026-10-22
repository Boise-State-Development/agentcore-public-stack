"""The chrome around the transcript: status bar, empty state, composer frame.

These exist because the previous pass shipped a status bar that laid out one
row too low — under the Footer — and nothing noticed, since every assertion
read ``status.line`` rather than the screen. The rendering tests here read the
frame.
"""

from __future__ import annotations

from agentcore_tui.client.agent_events import ToolCallRecord
from agentcore_tui.usage import Usage
from agentcore_tui.widgets import Notice, StatusBar, ToolCall, short_model
from agentcore_tui.widgets.agent_content import SUMMARY_CHARS, summarize_arguments

from .conftest import build_app, ok_handler, rendered_text, send


class TestShortModel:
    def test_strips_region_date_and_version(self) -> None:
        assert short_model("us.anthropic.claude-haiku-4-5-20251001-v1:0") == "claude-haiku-4-5"

    def test_unknown_shapes_pass_through(self) -> None:
        assert short_model("gpt-5.4") == "gpt-5.4"
        assert short_model("") == ""


class TestArgumentSummary:
    def test_prefers_the_argument_that_says_what_the_call_was_for(self) -> None:
        args = {"max_results": 5, "query": "haiku vs sonnet", "lang": "en"}
        assert summarize_arguments(args) == "haiku vs sonnet"

    def test_falls_back_to_the_first_string(self) -> None:
        assert summarize_arguments({"n": 3, "thing": "a value"}) == "a value"

    def test_takes_only_the_first_line_of_code(self) -> None:
        assert summarize_arguments({"code": "import pandas as pd\npd.read_csv('x')"}) == "import pandas as pd"

    def test_trims_long_values_with_an_ellipsis(self) -> None:
        text = summarize_arguments({"query": "x" * 200})
        assert len(text) == SUMMARY_CHARS
        assert text.endswith("…")

    def test_nothing_to_say_is_empty(self) -> None:
        assert summarize_arguments({}) == ""
        assert summarize_arguments({"n": 1}) == ""

    async def test_the_summary_is_in_the_header_line(self) -> None:
        record = ToolCallRecord(tool_use_id="t1", name="web_search", arguments={"query": "boise weather"})
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.chat.transcript.mount(ToolCall(record))
            await pilot.pause()
            frame = rendered_text(app)
            assert "web_search · boise weather" in frame


class TestStatusBar:
    async def test_the_status_row_is_actually_on_screen_above_the_footer(self) -> None:
        """The regression: docked under the Footer, it was laid out but invisible."""
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            bar = app.chat.query_one(StatusBar)
            footer_top = app.chat.query_one("Footer").region.y
            assert bar.region.height == 1
            assert bar.region.y < footer_top
            assert "Ready" in rendered_text(app)

    async def test_state_left_and_meta_right(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            bar = app.chat.status
            bar.set_usage(Usage(input_tokens=10, output_tokens=5))
            bar.set_turns(2)
            await pilot.pause()
            state = app.chat.query_one("#status-state")
            meta = app.chat.query_one("#status-meta")
            assert state.region.x < meta.region.x
            assert bar.line.startswith("Ready")
            assert "claude-haiku-4-5" in bar.line
            assert "2 turns" in bar.line
            assert "10 in" in bar.line

    async def test_busy_shows_a_spinner_that_is_not_part_of_the_line(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            bar = app.chat.status
            bar.set_state("Thinking…", busy=True)
            await pilot.pause()
            assert bar.has_class("-busy")
            assert str(app.chat.query_one("#status-state").content).endswith("Thinking…")
            assert str(app.chat.query_one("#status-state").content) != "Thinking…"
            assert bar.line.startswith("Thinking…")
            bar.set_state("Ready")
            await pilot.pause()
            assert str(app.chat.query_one("#status-state").content) == "Ready"


class TestEmptyState:
    async def test_shown_before_the_first_message_and_gone_after(self) -> None:
        app = build_app(ok_handler(["hi"]))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.chat.query("#empty-state")
            assert not app.chat.query(Notice)
            frame = rendered_text(app)
            assert "Ask anything" in frame
            assert "F2" in frame and "F3" in frame and "F4" in frame

            await send(pilot, app, "hello")
            assert not app.chat.query("#empty-state")

    async def test_new_conversation_brings_it_back(self) -> None:
        app = build_app(ok_handler(["hi"]))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "hello")
            await app.chat.action_new_conversation()
            await pilot.pause()
            assert app.chat.query("#empty-state")


class TestComposerFrame:
    async def test_names_the_model_and_the_send_key(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            composer = app.chat.composer
            assert composer.border_title == "claude-haiku-4-5"
            assert "Enter" in str(composer.border_subtitle)

    async def test_says_how_to_stop_while_busy(self) -> None:
        app = build_app(ok_handler(["hi"]))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat._set_composer_chrome(busy=True)
            assert "Esc" in str(app.chat.composer.border_subtitle)
            # A finished turn always puts the send hint back.
            await send(pilot, app, "go")
            assert "Enter" in str(app.chat.composer.border_subtitle)
            assert not app.chat.composer.has_class("-busy")
