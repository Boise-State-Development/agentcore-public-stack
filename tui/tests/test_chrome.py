"""The chrome around the transcript: status bar, empty state, composer frame.

These exist because the previous pass shipped a status bar that laid out one
row too low — under the Footer — and nothing noticed, since every assertion
read ``status.line`` rather than the screen. The rendering tests here read the
frame.
"""

from __future__ import annotations

from aps_tui.client.agent_events import ToolCallRecord
from aps_tui.usage import Usage
from aps_tui.widgets import Notice, StatusBar, ToolCall, short_model
from aps_tui.widgets.agent_content import SUMMARY_CHARS, summarize_arguments

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
            assert "/help" in frame and "commands" in frame

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


class TestSlashCommands:
    async def test_help_lists_commands_without_sending_a_turn(self) -> None:
        app = build_app(ok_handler(["never"]))
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.chat.composer.text = "/help"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            frame = rendered_text(app)
            assert "/model" in frame and "/theme" in frame and "/quit" in frame
            # An API-key session has no tool picker, so /tools is not offered.
            assert "/tools" not in frame
            assert app.chat.store.is_empty
            assert app.chat.composer.text == ""

    async def test_unknown_command_says_so(self) -> None:
        app = build_app(ok_handler(["never"]))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat.composer.text = "/frobnicate now"
            await pilot.press("enter")
            await pilot.pause()
            assert "Unknown command /frobnicate" in rendered_text(app)
            assert app.chat.store.is_empty

    async def test_double_slash_sends_a_literal_slash(self) -> None:
        app = build_app(ok_handler(["ok"]))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "//etc/hosts is where?")
            assert app.chat.store.messages[0].content == "/etc/hosts is where?"

    async def test_new_resets_the_conversation(self) -> None:
        app = build_app(ok_handler(["ok"]))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "hello")
            assert not app.chat.store.is_empty
            app.chat.composer.text = "/new"
            await pilot.press("enter")
            await pilot.pause()
            assert app.chat.store.is_empty
            assert app.chat.query("#empty-state")

    async def test_model_by_name_switches_without_the_picker(self) -> None:
        from .conftest import MODEL_B

        app = build_app(ok_handler(["ok"]))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            needle = MODEL_B.split(".")[-1][:8]
            app.chat.composer.text = f"/model {needle}"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert app.chat.config.model_id == MODEL_B

    async def test_model_with_no_match_warns(self) -> None:
        app = build_app(ok_handler(["ok"]))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            before = app.chat.config.model_id
            app.chat.composer.text = "/model nonesuch"
            await pilot.press("enter")
            await pilot.pause()
            assert app.chat.config.model_id == before

    async def test_copy_with_nothing_warns_rather_than_crashing(self) -> None:
        app = build_app(ok_handler(["ok"]))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat.composer.text = "/copy"
            await pilot.press("enter")
            await pilot.pause()
            assert app.chat.store.is_empty


class TestGauges:
    def test_elapsed_formatting(self) -> None:
        from aps_tui.widgets.status import format_elapsed

        assert format_elapsed(4.26) == "4.3s"
        assert format_elapsed(65) == "1m 05s"

    async def test_context_percent_appears_in_the_status_line(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            bar = app.chat.status
            bar.set_context(50_000, 200_000)
            assert "ctx 25%" in bar.line
            bar.set_context(1234, None)
            assert "ctx 1,234" in bar.line
            bar.set_context(None, None)
            assert "ctx" not in bar.line
            bar.set_elapsed(3.0)
            assert "3.0s" in bar.line


class TestAttach:
    async def test_attach_queues_the_upload_for_the_next_turn(self, tmp_path) -> None:
        import json as _json

        import httpx

        from .test_chat_screen import agent_turn, build_agent_app

        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/files/presign"):
                return httpx.Response(200, json={"uploadId": "up-9", "presignedUrl": "https://bucket.example/up-9", "expiresAt": "t"})
            if request.url.host == "bucket.example":
                return httpx.Response(200)
            if path.endswith("/complete"):
                return httpx.Response(200, json={"uploadId": "up-9", "status": "completed", "s3Uri": "s3://b/k", "filename": "a.txt", "sizeBytes": 3})
            if path.endswith("/chat/stream"):
                bodies.append(_json.loads(request.content))
                from .conftest import sse_response

                return sse_response(agent_turn("got it"))
            return httpx.Response(404)

        target = tmp_path / "a.txt"
        target.write_text("abc")
        app = build_agent_app(handler)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # The catalog client shares the handler through the same mock transport.
            from aps_tui.client.auth import SessionAuth
            from aps_tui.client.catalog import CatalogClient

            app.chat._catalog = CatalogClient(
                app.chat.config.base_url, auth=SessionAuth("s"), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
            )
            app.chat.composer.text = f"/attach {target}"
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
                if app.chat.turn.pending_uploads:  # type: ignore[attr-defined]
                    break
            assert app.chat.turn.pending_uploads == ["up-9"]  # type: ignore[attr-defined]
            assert "Attached a.txt" in rendered_text(app)

            await send(pilot, app, "read it")
            await send(pilot, app, "and again")

        assert bodies[0]["file_upload_ids"] == ["up-9"]
        assert "file_upload_ids" not in bodies[1]

    async def test_attach_missing_file_is_reported(self) -> None:
        from .test_chat_screen import agent_turn, build_agent_app

        from .conftest import sse_response

        app = build_agent_app(lambda _r: sse_response(agent_turn()))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat.composer.text = "/attach /definitely/not/here.pdf"
            await pilot.press("enter")
            await pilot.pause()
            assert "No such file" in rendered_text(app)
