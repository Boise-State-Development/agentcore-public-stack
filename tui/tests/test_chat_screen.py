"""Chat screen tests.

These exercise the real screen through Textual's ``run_test`` pilot, so they also
prove ``app.tcss`` parses — a stylesheet error raises on mount.

Assertions go through the conversation store and the turn controller rather than
private App attributes. That matters beyond tidiness: the old suite asserted on
``app._history`` and one test reimplemented the model-change action by hand, so
it passed whether or not that action worked.
"""

from __future__ import annotations

import json

import httpx
import pytest

from agentcore_tui.app import ChatApp
from agentcore_tui.client import AgentStreamClient
from agentcore_tui.client.auth import SessionAuth
from agentcore_tui.config import Config
from agentcore_tui.credentials import CredentialSource
from agentcore_tui.turn import AgentTurnController, TurnController
from agentcore_tui.widgets import AssistantMessage, Notice, StatusBar, ToolCall, UserMessage

from .conftest import (
    MODEL_B,
    MODEL_ID,
    Handler,
    build_app,
    command_titles,
    error_handler,
    make_config,
    ok_handler,
    rendered_text,
    sample_models,
    run_command,
    send,
    sse_body,
    sse_response,
    text_stream,
)


def unconfigured() -> object:
    return make_config(base_url="", api_key=None, credential_source=CredentialSource.NONE)


def build_agent_app(handler: Handler, *, config: Config | None = None) -> ChatApp:
    """A ChatApp driven through the *agent* transport, backed by MockTransport.

    Uses the agent factory seam so no session and no keyring are involved: the
    screen only needs a supplier, not a credential.
    """
    resolved = config if config is not None else make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION)

    def factory(cfg: Config) -> AgentStreamClient:
        return AgentStreamClient(
            cfg,
            auth=SessionAuth("sealed-test"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    return ChatApp(resolved, agent_client_factory=factory)


def agent_turn(text: str = "Hello", *, tools: bool = False) -> bytes:
    """An agent-dialect turn, optionally with one tool round trip."""
    frames: list[tuple[str, dict[str, object] | None]] = [
        ("init_event_loop", {}),
        ("session_title", {"type": "session_title", "sessionId": "s", "title": "A chat"}),
        ("message_start", {"role": "assistant"}),
    ]
    if tools:
        frames += [
            ("tool_use", {"toolUseId": "t1", "name": "calculator", "input": {"expression": "2+2"}}),
            ("tool_result", {"toolUseId": "t1", "content": [{"text": "Result: 4"}], "status": "success"}),
            ("message_start", {"role": "assistant"}),
        ]
    frames += [
        ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": text}),
        ("message_stop", {"stopReason": "end_turn"}),
        ("metadata", {"usage": {"inputTokens": 50, "outputTokens": 5}, "contextWindow": 200000}),
        ("done", {}),
    ]
    return sse_body(frames)


class TestAgentMode:
    """The session-native path: the screen talks to the tool-using agent."""

    async def test_a_session_selects_the_agent_controller(self) -> None:
        config = make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION)
        app = build_agent_app(lambda _r: sse_response(agent_turn()), config=config)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.chat.turn, AgentTurnController)

    async def test_an_api_key_still_selects_the_converse_controller(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.chat.turn, TurnController)

    async def test_the_welcome_mentions_what_a_session_unlocks(self) -> None:
        """The difference is the whole point of signing in."""
        app = build_agent_app(lambda _r: sse_response(agent_turn()))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "tools, memory" in rendered_text(app)

    async def test_a_turn_renders_and_is_recorded(self) -> None:
        app = build_agent_app(lambda _r: sse_response(agent_turn("4")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "what is 2+2")
            assert app.chat.store.messages[-1].content == "4"
            assert app.chat.query(AssistantMessage)

    async def test_a_tool_call_mounts_exactly_one_widget(self) -> None:
        """The controller reports the same record several times as the call
        progresses; mounting per report would stack duplicates."""
        app = build_agent_app(lambda _r: sse_response(agent_turn("4", tools=True)))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "what is 2+2")
            assert len(app.chat.query(ToolCall)) == 1

    async def test_the_tool_widget_shows_the_finished_call(self) -> None:
        app = build_agent_app(lambda _r: sse_response(agent_turn("4", tools=True)))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "what is 2+2")
            text = rendered_text(app)
            assert "calculator" in text

    async def test_the_session_title_becomes_the_sub_title(self) -> None:
        app = build_agent_app(lambda _r: sse_response(agent_turn()))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app.chat.sub_title == "A chat"

    async def test_the_prompt_is_sent_alone_against_the_session_id(self) -> None:
        """Regression guard on the payload shape from the screen's own path."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return sse_response(agent_turn())

        app = build_agent_app(handler)
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "just this")

        body = json.loads(captured[0].content)
        assert body["message"] == "just this"
        assert body["session_id"] == app.chat.store.session_id
        assert "messages" not in body


class TestNoChatTransport:
    async def test_a_credential_the_transport_cannot_send_is_reported_honestly(self) -> None:
        """The bug this replaced: the screen said "Ready", then the first message
        failed telling the user to run a login they had already run."""
        config = make_config(api_key=None, credential_source=CredentialSource.API_KEY)
        app = ChatApp(config)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.chat.composer.disabled is True
            text = rendered_text(app)
            assert "Signed in, but chat needs" in text
            assert "No chat transport" in text


class TestBoot:
    async def test_starts_ready_with_complete_config(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.chat.query(Notice)
            assert app.chat.composer.submit_enabled is True
            assert app.chat.composer.disabled is False
            assert isinstance(app.chat.query_one(StatusBar), StatusBar)

    async def test_incomplete_config_disables_sending_and_explains(self) -> None:
        app = build_app(ok_handler(), config=unconfigured())  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.chat.composer.disabled is True
            assert app.chat.composer.submit_enabled is False
            assert app.chat.query(".notice.-error")

    async def test_incomplete_config_ignores_enter(self) -> None:
        app = build_app(ok_handler(), config=unconfigured())  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            app.chat.composer.text = "should not send"
            await pilot.press("enter")
            await pilot.pause()
            assert not app.chat.query(UserMessage)

    async def test_an_sso_session_is_not_treated_as_unconfigured(self) -> None:
        """The regression this discriminant exists to prevent: under OIDC there
        is no API key, and a correctly signed-in user must not be shown setup
        help."""
        config = make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION)
        app = build_app(ok_handler(), config=config)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.chat.composer.disabled is False
            assert not app.chat.query(".notice.-error")


class TestSending:
    async def test_streamed_turn_renders_and_records_the_conversation(self) -> None:
        app = build_app(ok_handler(["Hel", "lo"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi there")

            assert len(app.chat.query(UserMessage)) == 1
            assert len(app.chat.query(AssistantMessage)) == 1
            assert [(m.role, m.content) for m in app.store] == [("user", "hi there"), ("assistant", "Hello")]

    async def test_composer_is_cleared_after_sending(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app.chat.composer.text == ""

    async def test_multi_turn_accumulates_history(self) -> None:
        app = build_app(ok_handler(["ack"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "first")
            await send(pilot, app, "second")

            assert [m.role for m in app.store] == ["user", "assistant", "user", "assistant"]
            assert len(app.chat.query(UserMessage)) == 2

    async def test_history_is_sent_so_the_model_has_context(self) -> None:
        sent: list[list[dict[str, str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content)["messages"])
            return sse_response(text_stream(["ok"]))

        app = build_app(handler)
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "first")
            await send(pilot, app, "second")

        assert [m["content"] for m in sent[0]] == ["first"]
        assert [m["content"] for m in sent[1]] == ["first", "ok", "second"]

    async def test_usage_reaches_the_status_bar(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return sse_response(text_stream(["ok"], usage={"inputTokens": 11, "outputTokens": 22}))

        app = build_app(handler)
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert "11 in" in app.chat.status.line
            assert "22 out" in app.chat.status.line
            assert "1 turn" in app.chat.status.line

    async def test_empty_prompt_does_not_send(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            app.chat.composer.text = "   "
            await pilot.press("enter")
            await pilot.pause()
            assert not app.chat.query(UserMessage)

    async def test_newline_key_inserts_instead_of_sending(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            app.chat.composer.focus()
            app.chat.composer.text = "line one"
            app.chat.composer.move_cursor((0, len("line one")))
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert "\n" in app.chat.composer.text
            assert not app.chat.query(UserMessage)

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
            last = app.store.last_assistant
            assert last is not None
            assert last.content == "answer"
            assert app.chat.query(".reasoning")


class TestErrorSurfaces:
    async def test_auth_failure_is_explained_and_recoverable(self) -> None:
        app = build_app(error_handler(401, "Invalid or expired API key"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")

            assert app.chat.query(".notice.-error")
            # The failed assistant turn must not enter the conversation.
            assert [m.role for m in app.store] == ["user"]
            # And the UI must return to a usable state rather than wedging.
            assert app.chat.composer.submit_enabled is True
            assert app.chat.turn.busy is False

    @pytest.mark.parametrize(
        ("status", "detail"),
        [
            (403, f"Access denied to model: {MODEL_ID}"),
            (429, "Rate limit exceeded."),
            (502, "Bedrock is unavailable"),
        ],
    )
    async def test_http_failures_are_reported_without_wedging(self, status: int, detail: str) -> None:
        app = build_app(error_handler(status, detail))
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app.chat.query(".notice.-error")
            assert app.chat.turn.busy is False
            assert app.chat.composer.submit_enabled is True

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
            assert app.chat.query(".notice.-error")
            assert [m.role for m in app.store] == ["user"]

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
            last = app.store.last_assistant
            assert last is not None
            assert last.content == "cut off"
            assert app.chat.query(".notice.-error")

    async def test_connection_failure_is_survivable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        app = build_app(handler)
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert app.chat.query(".notice.-error")
            assert app.chat.turn.busy is False


class TestActions:
    async def test_new_conversation_clears_the_store_and_transcript(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "hi")
            assert len(app.store) == 2

            await app.chat.run_action("new_conversation")
            await pilot.pause()

            assert app.store.is_empty
            assert not app.chat.query(UserMessage)
            assert not app.chat.query(AssistantMessage)

    async def test_new_conversation_starts_a_new_session(self) -> None:
        """The old conversation may already be persisted under the previous id."""
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            first = app.store.session_id
            await app.chat.run_action("new_conversation")
            await pilot.pause()
            assert app.store.session_id != first

    async def test_stop_binding_is_unavailable_while_idle(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.chat.check_action("cancel_turn", ()) is False

    async def test_model_change_is_applied_to_later_requests(self) -> None:
        """Drives the real ``set_model`` rather than reproducing its steps, which
        is what the previous version of this test did — it would have passed with
        the action broken."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["model_id"])
            return sse_response(text_stream(["ok"]))

        app = build_app(handler)
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "first")

            await app.chat.set_model(MODEL_B)
            await pilot.pause()

            await send(pilot, app, "second")
            assert seen == [MODEL_ID, MODEL_B]

    async def test_model_change_shows_in_the_status_bar(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.chat.set_model(MODEL_B)
            await pilot.pause()
            assert "sonnet" in app.chat.status.line


class TestPaletteCommands:
    async def test_stop_offered_only_while_a_turn_is_in_flight(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "Stop response" not in command_titles(app)

    async def test_copy_offered_only_once_there_is_an_answer(self) -> None:
        app = build_app(ok_handler(["the answer"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "Copy last response" not in command_titles(app)

            await send(pilot, app, "hi")
            assert "Copy last response" in command_titles(app)

    async def test_copy_last_answer_uses_the_latest_assistant_message(self) -> None:
        copied: list[str] = []
        app = build_app(ok_handler(["first answer"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.copy_to_clipboard = copied.append  # type: ignore[method-assign]

            await send(pilot, app, "hi")
            await run_command(app, "Copy last response")

            assert copied == ["first answer"]

    async def test_copy_transcript_includes_both_roles(self) -> None:
        copied: list[str] = []
        app = build_app(ok_handler(["an answer"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.copy_to_clipboard = copied.append  # type: ignore[method-assign]

            await send(pilot, app, "a question")
            await run_command(app, "Copy transcript")

            assert len(copied) == 1
            assert "a question" in copied[0]
            assert "an answer" in copied[0]


class TestModelPicker:
    async def test_picker_lists_models_with_their_providers(self) -> None:
        """The provider is shown because it is what actually gets sent, and it is
        the difference between two similarly-named models."""
        from textual.widgets import OptionList

        from agentcore_tui.screens import ModelPicker

        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = ModelPicker(sample_models(), current=MODEL_ID)
            app.push_screen(picker)
            await pilot.pause()

            option_list = picker.query_one("#picker-list", OptionList)
            # Two models plus the System Default row.
            assert option_list.option_count == 3
            # The active model is pre-highlighted so Enter is a no-op, not a
            # surprise. Index 1 because System Default occupies index 0.
            assert option_list.highlighted == 1

    async def test_selecting_a_model_returns_its_provider_too(self) -> None:
        """The pair is one decision.

        Not a crash-avoidance measure — the server resolves a missing provider
        from its registry — but sending it pins the turn to the provider the
        catalogue advertised, instead of to registry state at request time.
        """
        from textual.widgets import OptionList

        from agentcore_tui.screens.model_picker import ModelChoice, ModelPicker

        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = ModelPicker(sample_models(), current=MODEL_ID)
            result: list[ModelChoice | None] = []
            app.push_screen(picker, callback=result.append)
            await pilot.pause()

            picker.query_one("#picker-list", OptionList).highlighted = 2
            await pilot.press("enter")
            await pilot.pause()

        assert result == [ModelChoice(model_id=MODEL_B, provider="mantle")]

    async def test_system_default_sends_neither_field(self) -> None:
        from textual.widgets import OptionList

        from agentcore_tui.screens.model_picker import SYSTEM_DEFAULT, ModelPicker

        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = ModelPicker(sample_models(), current=MODEL_ID)
            result: list[object] = []
            app.push_screen(picker, callback=result.append)
            await pilot.pause()

            picker.query_one("#picker-list", OptionList).highlighted = 0
            await pilot.press("enter")
            await pilot.pause()

        assert result == [SYSTEM_DEFAULT]
        assert SYSTEM_DEFAULT.is_system_default

    async def test_an_empty_catalogue_still_offers_system_default(self) -> None:
        """A failed `/models` fetch must not leave a dead keybinding."""
        from textual.widgets import OptionList

        from agentcore_tui.screens import ModelPicker

        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = ModelPicker([], current="", error="Could not load models: boom")
            app.push_screen(picker)
            await pilot.pause()

            assert picker.query_one("#picker-list", OptionList).option_count == 1
            assert "Could not load models" in rendered_text(app)


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

            transcript = app.chat.transcript
            assert transcript.virtual_size.height > transcript.size.height, "transcript did not grow beyond one screen — content is being clipped"
            assert transcript.max_scroll_y > 0, "transcript is not scrollable, so content below the fold is unreachable"

    async def test_view_follows_the_stream_to_the_tail(self) -> None:
        app = build_app(self._chunked_handler(self.LONG_MARKDOWN))
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await send(pilot, app, "examples please")
            await pilot.pause()

            transcript = app.chat.transcript
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

            body = list(app.chat.query(Markdown))[-1]
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
        app = build_app(ok_handler(), config=unconfigured())  # type: ignore[arg-type]
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
