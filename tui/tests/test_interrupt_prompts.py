"""The paused-turn prompts, end to end through the chat screen.

The server pauses with an interrupt event; the screen renders a prompt; a key
answers it; a second request carries ``interrupt_responses``. Each prompt
flavour is driven once through a MockTransport that scripts both legs, so the
assertion is on the wire — the shape the SPA sends and the server parses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from aps_tui.client.agent_events import (
    BrowserLoginRequired,
    ClientToolCall,
    OAuthRequired,
    QuestionOption,
    ToolApprovalRequired,
    UserQuestion,
    UserQuestionRequired,
)
from aps_tui.config import Config
from aps_tui.credentials import CredentialSource
from aps_tui.local_tools import ToolExecutor
from aps_tui.widgets import AssistantMessage
from aps_tui.widgets.prompts import ClientToolPrompt, ToolApprovalPrompt, UserQuestionPrompt

from .conftest import make_config, rendered_text, sse_body, sse_response, wait_for_turn
from .test_chat_screen import build_agent_app


def paused_turn(interrupt_event: str, payload: dict[str, Any], *, text: str = "Let me check.") -> bytes:
    return sse_body(
        [
            ("message_start", {"role": "assistant"}),
            ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": text}),
            ("message_stop", {"stopReason": "end_turn"}),
            (interrupt_event, {"type": interrupt_event, **payload}),
            ("done", {}),
        ]
    )


def finished_turn(text: str = "Done.") -> bytes:
    return sse_body(
        [
            ("message_start", {"role": "assistant"}),
            ("content_block_delta", {"contentBlockIndex": 0, "type": "text", "text": text}),
            ("message_stop", {"stopReason": "end_turn"}),
            ("metadata", {"usage": {"inputTokens": 50, "outputTokens": 5}}),
            ("done", {}),
        ]
    )


class Script:
    """First request pauses; the resume finishes. Records every body."""

    def __init__(self, first: bytes, second: bytes = finished_turn()) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._responses = [first, second]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return sse_response(self._responses.pop(0) if self._responses else finished_turn())

    @property
    def resume(self) -> dict[str, Any]:
        return self.bodies[1]


async def start_turn(pilot, app, prompt: str = "do it") -> None:
    app.chat.composer.text = prompt
    await pilot.press("enter")
    # Let the first stream drain and the prompt mount.
    for _ in range(20):
        await pilot.pause()
        if app.chat.turn.paused:  # type: ignore[attr-defined]
            break


def local_tools_config(**overrides: Any) -> Config:
    settings: dict[str, Any] = {"api_key": None, "credential_source": CredentialSource.BFF_SESSION, "local_tools": True}
    settings.update(overrides)
    return make_config(**settings)


CLIENT_TOOL = {
    "interruptId": "v1:tool_call:tu-1:abc",
    "toolUseId": "tu-1",
    "toolName": "run_command",
    "arguments": {"command": "echo hi", "reason": "To check."},
}


class TestClientToolPrompt:
    async def test_the_turn_declares_the_local_tools(self) -> None:
        script = Script(finished_turn())
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat.composer.text = "hi"
            await pilot.press("enter")
            await wait_for_turn(pilot, app)
        names = [spec["name"] for spec in script.bodies[0]["client_tools"]]
        assert names == ["workspace_info", "run_command", "read_file", "list_directory", "write_file"]

    async def test_no_local_tools_means_none_declared(self) -> None:
        script = Script(finished_turn())
        app = build_agent_app(script, config=local_tools_config(local_tools=False))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat.composer.text = "hi"
            await pilot.press("enter")
            await wait_for_turn(pilot, app)
        assert "client_tools" not in script.bodies[0]

    async def test_y_runs_the_command_and_resumes_with_its_output(self, tmp_path: Path) -> None:
        script = Script(paused_turn("client_tool_call", CLIENT_TOOL))
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat._executor = ToolExecutor(cwd=tmp_path)
            await start_turn(pilot, app)

            prompt = app.chat.query_one(ClientToolPrompt)
            frame = rendered_text(app)
            assert "echo hi" in frame and "To check." in frame
            assert app.chat.composer.submit_enabled is False

            prompt.focus()
            await pilot.press("y")
            await wait_for_turn(pilot, app)
            assert not app.chat.turn.busy
            assert app.chat.composer.submit_enabled is True

        entry = script.resume["interrupt_responses"][0]
        assert entry["interruptId"] == "v1:tool_call:tu-1:abc"
        assert entry["response"]["isError"] is False
        assert entry["response"]["output"].startswith("hi\n[exit 0]")
        assert script.resume["message"] == ""
        # The resume re-declares the same set, so the paused agent is rebuilt identically.
        assert "run_command" in [s["name"] for s in script.resume["client_tools"]]
        assert app.chat.store.messages[-1].content == "Done."

    async def test_n_declines_without_running(self, tmp_path: Path) -> None:
        marker = tmp_path / "ran"
        script = Script(paused_turn("client_tool_call", {**CLIENT_TOOL, "arguments": {"command": f"touch {marker}"}}))
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat._executor = ToolExecutor(cwd=tmp_path)
            await start_turn(pilot, app)
            app.chat.query_one(ClientToolPrompt).focus()
            await pilot.press("n")
            await wait_for_turn(pilot, app)
        assert script.resume["interrupt_responses"][0]["response"] == {"declined": True}
        assert not marker.exists()

    async def test_a_remembers_the_tool_for_the_session(self, tmp_path: Path) -> None:
        script = Script(
            paused_turn("client_tool_call", CLIENT_TOOL), paused_turn("client_tool_call", {**CLIENT_TOOL, "interruptId": "v1:tool_call:tu-2:def"})
        )
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat._executor = ToolExecutor(cwd=tmp_path)
            await start_turn(pilot, app)
            app.chat.query_one(ClientToolPrompt).focus()
            await pilot.press("a")
            # The second pause auto-approves: nothing to press.
            await wait_for_turn(pilot, app)
            assert app.chat._executor.is_pre_approved("run_command")
        assert len(script.bodies) == 3
        assert script.bodies[2]["interrupt_responses"][0]["response"]["output"].startswith("hi")

    async def test_the_prompt_collapses_to_its_outcome(self, tmp_path: Path) -> None:
        script = Script(paused_turn("client_tool_call", CLIENT_TOOL))
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat._executor = ToolExecutor(cwd=tmp_path)
            await start_turn(pilot, app)
            app.chat.query_one(ClientToolPrompt).focus()
            await pilot.press("y")
            await wait_for_turn(pilot, app)
            await pilot.pause()
            frame = rendered_text(app)
            assert "✓ run_command · hi" in frame
            assert "y run" not in frame
            # The resumed answer got its own message after the prompt.
            assert len(app.chat.query(AssistantMessage)) == 2

    async def test_escape_cancels_a_paused_turn(self, tmp_path: Path) -> None:
        script = Script(paused_turn("client_tool_call", CLIENT_TOOL))
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await start_turn(pilot, app)
            await app.chat.action_cancel_turn()
            await pilot.pause()
            assert not app.chat.turn.busy
            assert app.chat.composer.submit_enabled is True
            assert "Cancelled" in rendered_text(app)
        assert len(script.bodies) == 1


class TestServerApprovalPrompt:
    PAYLOAD = {
        "interruptId": "v1:tool_call:tu-9:x",
        "toolUseId": "tu-9",
        "toolName": "canvas_grade",
        "message": "This tool changes grades.",
        "toolInput": '{"course": 1}',
    }

    @pytest.mark.parametrize(("key", "expected"), [("y", "approved"), ("n", "declined")])
    async def test_answers_with_the_spas_string(self, key: str, expected: str) -> None:
        script = Script(paused_turn("tool_approval_required", self.PAYLOAD))
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await start_turn(pilot, app)
            assert "This tool changes grades." in rendered_text(app)
            app.chat.query_one(ToolApprovalPrompt).focus()
            await pilot.press(key)
            await wait_for_turn(pilot, app)
        assert script.resume["interrupt_responses"] == [{"interruptId": "v1:tool_call:tu-9:x", "response": expected}]


class TestUserQuestionPrompt:
    PAYLOAD = {
        "interruptId": "v1:tool_call:tu-5:q",
        "toolUseId": "tu-5",
        "questions": [
            {
                "header": "Scope",
                "question": "How much?",
                "multiSelect": False,
                "options": [{"label": "All", "description": "Everything"}, {"label": "Some"}],
            },
            {
                "header": "Format",
                "question": "Which format?",
                "multiSelect": True,
                "options": [{"label": "PDF"}, {"label": "HTML"}, {"label": "Text"}],
            },
        ],
    }

    async def test_single_then_multi_select(self) -> None:
        script = Script(paused_turn("user_question_required", self.PAYLOAD))
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await start_turn(pilot, app)
            frame = rendered_text(app)
            assert "Question 1 of 2" in frame and "How much?" in frame
            app.chat.query_one(UserQuestionPrompt).focus()
            await pilot.press("2")  # Some → advances
            await pilot.pause()
            assert "Question 2 of 2" in rendered_text(app)
            await pilot.press("1")
            await pilot.press("3")
            await pilot.press("enter")
            await wait_for_turn(pilot, app)
        response = script.resume["interrupt_responses"][0]["response"]
        assert response == {"answers": {"Scope": {"selected": ["Some"]}, "Format": {"selected": ["PDF", "Text"]}}}

    async def test_other_free_text_and_skip(self) -> None:
        script = Script(paused_turn("user_question_required", self.PAYLOAD))
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await start_turn(pilot, app)
            app.chat.query_one(UserQuestionPrompt).focus()
            await pilot.press("o")
            await pilot.pause()
            for ch in "Half":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("s")
            await wait_for_turn(pilot, app)
        response = script.resume["interrupt_responses"][0]["response"]
        assert response["answers"]["Scope"] == {"selected": [], "freeText": "Half"}
        assert response["answers"]["Format"] == {"skipped": True}

    async def test_x_skips_everything(self) -> None:
        script = Script(paused_turn("user_question_required", self.PAYLOAD))
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await start_turn(pilot, app)
            app.chat.query_one(UserQuestionPrompt).focus()
            await pilot.press("x")
            await wait_for_turn(pilot, app)
        assert script.resume["interrupt_responses"][0]["response"] == {"skipped": True}


class TestOtherPrompts:
    async def test_oauth_enter_means_consented(self) -> None:
        payload = {"interruptId": "v1:x", "providerId": "google", "authorizationUrl": "https://accounts.example/auth?x=1"}
        script = Script(paused_turn("oauth_required", payload))
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await start_turn(pilot, app)
            assert "https://accounts.example/auth?x=1" in rendered_text(app)
            app.chat.query_one("OAuthPrompt").focus()
            await pilot.press("enter")
            await wait_for_turn(pilot, app)
        assert script.resume["interrupt_responses"] == [{"interruptId": "v1:x", "response": "consented"}]

    async def test_browser_login_can_only_be_skipped(self) -> None:
        payload = {"interruptId": "v1:b", "toolUseId": "tu", "message": "Log in to the portal."}
        script = Script(paused_turn("browser_login_required", payload))
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await start_turn(pilot, app)
            app.chat.query_one("BrowserLoginPrompt").focus()
            await pilot.press("enter")
            await wait_for_turn(pilot, app)
        assert script.resume["interrupt_responses"] == [{"interruptId": "v1:b", "response": {"skipped": True}}]

    async def test_two_interrupts_resume_once_with_both_answers(self) -> None:
        body = sse_body(
            [
                ("message_start", {"role": "assistant"}),
                ("message_stop", {"stopReason": "end_turn"}),
                ("tool_approval_required", {"type": "tool_approval_required", "interruptId": "a", "toolUseId": "1", "toolName": "t1"}),
                ("tool_approval_required", {"type": "tool_approval_required", "interruptId": "b", "toolUseId": "2", "toolName": "t2"}),
                ("done", {}),
            ]
        )
        script = Script(body)
        app = build_agent_app(script, config=local_tools_config())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await start_turn(pilot, app)
            prompts = list(app.chat.query(ToolApprovalPrompt))
            assert len(prompts) == 2
            prompts[0].focus()
            await pilot.press("y")
            await pilot.pause()
            assert len(script.bodies) == 1  # not yet
            prompts[1].focus()
            await pilot.press("n")
            await wait_for_turn(pilot, app)
        assert script.resume["interrupt_responses"] == [{"interruptId": "a", "response": "approved"}, {"interruptId": "b", "response": "declined"}]


class TestParsing:
    """The dialect parses the three new interrupt events."""

    def test_shapes(self) -> None:
        from aps_tui.client.agent_events import parse_agent_event

        ct = parse_agent_event("client_tool_call", json.dumps(CLIENT_TOOL))
        assert isinstance(ct, ClientToolCall) and ct.tool_name == "run_command" and ct.arguments["command"] == "echo hi"

        uq = parse_agent_event("user_question_required", json.dumps(TestUserQuestionPrompt.PAYLOAD))
        assert isinstance(uq, UserQuestionRequired)
        assert uq.questions[0] == UserQuestion(
            header="Scope", question="How much?", options=(QuestionOption("All", "Everything"), QuestionOption("Some"))
        )
        assert uq.questions[1].multi_select

        bl = parse_agent_event("browser_login_required", json.dumps({"interruptId": "b", "message": "m"}))
        assert isinstance(bl, BrowserLoginRequired) and bl.message == "m"

        oa = parse_agent_event("oauth_required", json.dumps({"interruptId": "o", "providerId": "p", "authorizationUrl": "u"}))
        assert isinstance(oa, OAuthRequired)
        ta = parse_agent_event("tool_approval_required", json.dumps(TestServerApprovalPrompt.PAYLOAD))
        assert isinstance(ta, ToolApprovalRequired)

    def test_a_question_without_options_is_dropped(self) -> None:
        from aps_tui.client.agent_events import parse_agent_event

        uq = parse_agent_event(
            "user_question_required", json.dumps({"interruptId": "q", "questions": [{"header": "X", "question": "?", "options": []}]})
        )
        assert isinstance(uq, UserQuestionRequired) and uq.questions == ()
