"""Restoring a turn that paused while nobody was here.

The server keeps a paused turn, and one ``PendingInterrupt`` row per open
prompt, until someone answers. These tests follow a stored row through the
three layers that have to agree: it parses into the live event type, the
catalog fetches it (minting a fresh OAuth URL), the controller re-enters the
paused state without opening a stream, and the chat screen mounts the same
prompt a live pause would and resumes with the answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from aps_tui.client.agent_events import (
    BrowserLoginRequired,
    ClientToolCall,
    OAuthRequired,
    ToolApprovalRequired,
    UserQuestionRequired,
    interrupt_from_pending,
)
from aps_tui.client.catalog import ConversationSummary
from aps_tui.local_tools import ToolExecutor
from aps_tui.widgets.prompts import ClientToolPrompt, OAuthPrompt

from .conftest import rendered_text, sse_response, wait_for_turn
from .test_agent_turn import answer, build
from .test_chat_screen import build_agent_app
from .test_interrupt_prompts import finished_turn, local_tools_config
from .test_pickers import catalog_for, session_wire

# ------------------------------------------------------------------- rows


def client_tool_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "interruptId": "v1:tool:tu-1:abc",
        "kind": "client_tool",
        "toolUseId": "tu-1",
        "toolName": "run_command",
        "toolInput": json.dumps({"command": "echo restored", "reason": "To check."}),
        "createdAt": "2026-10-29T00:00:00Z",
    }
    row.update(overrides)
    return row


class TestRowParsing:
    def test_client_tool_row_decodes_its_json_arguments(self) -> None:
        event = interrupt_from_pending(client_tool_row())
        assert isinstance(event, ClientToolCall)
        assert event.interrupt_id == "v1:tool:tu-1:abc"
        assert event.tool_name == "run_command"
        assert event.arguments == {"command": "echo restored", "reason": "To check."}

    def test_tool_approval_row(self) -> None:
        event = interrupt_from_pending(
            {"interruptId": "i2", "kind": "tool_approval", "toolUseId": "tu-2", "toolName": "hello", "toolInput": '{"a": 1}', "message": "OK?"}
        )
        assert isinstance(event, ToolApprovalRequired)
        assert event.tool_name == "hello" and event.message == "OK?" and event.tool_input == '{"a": 1}'

    def test_user_question_row_decodes_its_json_questions(self) -> None:
        questions = [{"header": "Scope", "question": "How much?", "options": [{"label": "All"}, {"label": "Some"}]}]
        event = interrupt_from_pending({"interruptId": "i3", "kind": "user_question", "toolUseId": "tu-3", "questions": json.dumps(questions)})
        assert isinstance(event, UserQuestionRequired)
        assert [q.header for q in event.questions] == ["Scope"]
        assert [o.label for o in event.questions[0].options] == ["All", "Some"]

    def test_a_question_row_with_nothing_answerable_is_dropped(self) -> None:
        assert interrupt_from_pending({"interruptId": "i3", "kind": "user_question", "questions": "[]"}) is None
        assert interrupt_from_pending({"interruptId": "i3", "kind": "user_question", "questions": "{not json"}) is None

    def test_oauth_row_takes_the_url_it_is_given(self) -> None:
        event = interrupt_from_pending({"interruptId": "i4", "kind": "oauth", "providerId": "google"}, authorization_url="https://consent")
        assert isinstance(event, OAuthRequired)
        assert event.provider_id == "google" and event.authorization_url == "https://consent"

    def test_a_row_without_kind_is_oauth_for_backward_compatibility(self) -> None:
        event = interrupt_from_pending({"interruptId": "i4", "providerId": "google"})
        assert isinstance(event, OAuthRequired)

    def test_browser_login_row_uses_its_reason(self) -> None:
        event = interrupt_from_pending({"interruptId": "i5", "kind": "browser_login", "reason": "Sign into Canvas"})
        assert isinstance(event, BrowserLoginRequired) and event.message == "Sign into Canvas"

    def test_unknown_kinds_and_missing_ids_are_skipped(self) -> None:
        assert interrupt_from_pending({"interruptId": "i6", "kind": "teleport"}) is None
        assert interrupt_from_pending(client_tool_row(interruptId="")) is None

    def test_bad_tool_input_json_becomes_empty_arguments(self) -> None:
        event = interrupt_from_pending(client_tool_row(toolInput="{nope"))
        assert isinstance(event, ClientToolCall) and event.arguments == {}


# ---------------------------------------------------------------- catalog


def pending_handler(rows: list[dict[str, Any]], *, consent: dict[str, Any] | int = 200, capture: list[httpx.Request] | None = None):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        path = request.url.path
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [], "nextToken": None, "pendingInterrupts": rows})
        if path.endswith("/initiate-consent"):
            if isinstance(consent, int):
                return httpx.Response(consent, json={"detail": "nope"})
            return httpx.Response(200, json=consent)
        return httpx.Response(404, json={"detail": "no route"})

    return handler


class TestCatalogPendingInterrupts:
    async def test_reads_rows_from_a_one_message_page(self) -> None:
        capture: list[httpx.Request] = []
        api = catalog_for(pending_handler([client_tool_row()], capture=capture))
        events = await api.pending_interrupts("s1")
        await api.aclose()
        assert [type(e) for e in events] == [ClientToolCall]
        assert capture[0].url.path.endswith("/sessions/s1/messages")
        assert capture[0].url.params["limit"] == "1"

    async def test_oauth_rows_get_a_fresh_consent_url(self) -> None:
        capture: list[httpx.Request] = []
        api = catalog_for(
            pending_handler(
                [{"interruptId": "i4", "kind": "oauth", "providerId": "google"}],
                consent={"connected": False, "authorization_url": "https://fresh"},
                capture=capture,
            )
        )
        [event] = await api.pending_interrupts("s1")
        await api.aclose()
        assert isinstance(event, OAuthRequired) and event.authorization_url == "https://fresh"
        assert any(r.method == "POST" and r.url.path.endswith("/connectors/google/initiate-consent") for r in capture)

    async def test_already_connected_and_failed_mints_leave_the_url_empty(self) -> None:
        row = {"interruptId": "i4", "kind": "oauth", "providerId": "google"}
        for consent in ({"connected": True}, 500):
            api = catalog_for(pending_handler([row], consent=consent))
            [event] = await api.pending_interrupts("s1")
            await api.aclose()
            assert isinstance(event, OAuthRequired) and event.authorization_url == ""

    async def test_unusable_rows_are_skipped_not_raised(self) -> None:
        api = catalog_for(pending_handler([{"kind": "teleport", "interruptId": "x"}, "garbage", client_tool_row()]))  # type: ignore[list-item]
        events = await api.pending_interrupts("s1")
        await api.aclose()
        assert [type(e) for e in events] == [ClientToolCall]


# ------------------------------------------------------------- controller


class TestControllerRestore:
    async def test_enters_busy_and_paused_and_hands_each_interrupt_to_the_sink(self) -> None:
        controller, sink, _store, client = build(answer("Done."))
        events = [interrupt_from_pending(client_tool_row()), interrupt_from_pending(client_tool_row(interruptId="i2", toolUseId="tu-2"))]
        await controller.restore_paused(events)  # type: ignore[arg-type]
        assert controller.busy and controller.paused
        assert [e.interrupt_id for e in sink.interrupts] == ["v1:tool:tu-1:abc", "i2"]
        assert ("Waiting for you", True, False) in sink.states
        assert client.stream_calls == []  # nothing was sent

    async def test_resume_after_restore_sends_the_answers(self) -> None:
        controller, _sink, store, client = build(answer("Done."))
        await controller.restore_paused([interrupt_from_pending(client_tool_row())])  # type: ignore[list-item]
        await controller.resume([{"interruptId": "v1:tool:tu-1:abc", "response": {"output": "ok", "isError": False}}])
        assert not controller.busy and not controller.paused
        call = client.stream_calls[0]
        assert call["message"] == "" and call["session_id"] == store.session_id
        assert call["interrupt_responses"][0]["interruptId"] == "v1:tool:tu-1:abc"

    async def test_cancel_after_restore_does_not_tell_the_server_to_stop(self) -> None:
        """Nothing is generating; the server's paused copy stays for a later try."""
        controller, _sink, _store, client = build(answer("Done."))
        await controller.restore_paused([interrupt_from_pending(client_tool_row())])  # type: ignore[list-item]
        await controller.cancel()
        assert not controller.busy and not controller.paused
        assert client.interrupts == []

    async def test_restore_while_busy_or_with_nothing_is_a_no_op(self) -> None:
        controller, sink, _store, _client = build(answer("Done."))
        await controller.restore_paused([])
        assert not controller.busy
        await controller.begin("hi")
        await controller.restore_paused([interrupt_from_pending(client_tool_row())])  # type: ignore[list-item]
        assert sink.interrupts == []


# ------------------------------------------------------------- chat screen


class StreamScript:
    """Answers /chat/stream with a finished turn and records bodies; serves
    the messages page with the given pending rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.bodies: list[dict[str, Any]] = []
        self.interrupt_posts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/stream"):
            self.bodies.append(json.loads(request.content))
            return sse_response(finished_turn("Finished after restore."))
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [], "nextToken": None, "pendingInterrupts": self.rows})
        if path.endswith("/interrupt"):
            self.interrupt_posts += 1
            return httpx.Response(200, json={})
        if path.endswith("/initiate-consent"):
            return httpx.Response(200, json={"connected": True})
        return httpx.Response(404, json={"detail": "no route"})


def restore_app(script: StreamScript):  # type: ignore[no-untyped-def]
    app = build_agent_app(script, config=local_tools_config())
    app.chat._catalog = catalog_for(script)
    return app


async def adopt(pilot, app, session_id: str = "s-paused") -> None:  # type: ignore[no-untyped-def]
    summary = ConversationSummary.from_wire(session_wire(session_id, "Half done"))
    await app.chat._adopt_conversation(summary, [], None)
    for _ in range(10):
        await pilot.pause()
        if app.chat.turn.paused:
            break


class TestChatRestore:
    async def test_opening_a_paused_conversation_mounts_its_prompt_and_blocks_sending(self) -> None:
        script = StreamScript([client_tool_row()])
        app = restore_app(script)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await adopt(pilot, app)
            assert app.chat.turn.paused
            assert app.chat.query(ClientToolPrompt)
            frame = rendered_text(app)
            assert "echo restored" in frame
            assert "paused on 1 question" in frame
            assert app.chat.composer.submit_enabled is False

    async def test_answering_the_restored_prompt_resumes_the_turn(self, tmp_path: Path) -> None:
        script = StreamScript([client_tool_row()])
        app = restore_app(script)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.chat._executor = ToolExecutor(cwd=tmp_path)
            await adopt(pilot, app)
            app.chat.query_one(ClientToolPrompt).focus()
            await pilot.press("y")
            await wait_for_turn(pilot, app)
            assert not app.chat.turn.busy
            assert app.chat.composer.submit_enabled is True
            assert "Finished after restore." in rendered_text(app)
        body = script.bodies[0]
        assert body["session_id"] == "s-paused" and body["message"] == ""
        entry = body["interrupt_responses"][0]
        assert entry["interruptId"] == "v1:tool:tu-1:abc"
        assert entry["response"]["output"].startswith("restored\n[exit 0]")

    async def test_escape_leaves_the_server_paused_and_frees_the_composer(self) -> None:
        script = StreamScript([client_tool_row()])
        app = restore_app(script)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await adopt(pilot, app)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()
            assert not app.chat.turn.busy
            assert app.chat.composer.submit_enabled is True
        assert script.interrupt_posts == 0
        assert script.bodies == []

    async def test_a_conversation_with_nothing_pending_opens_idle(self) -> None:
        script = StreamScript([])
        app = restore_app(script)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await adopt(pilot, app)
            assert not app.chat.turn.busy
            assert app.chat.composer.submit_enabled is True
            assert "paused on" not in rendered_text(app)

    async def test_an_oauth_restore_when_already_connected_says_so(self) -> None:
        script = StreamScript([{"interruptId": "i4", "kind": "oauth", "providerId": "google"}])
        app = restore_app(script)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await adopt(pilot, app)
            assert app.chat.query(OAuthPrompt)
            assert "You're connected now" in rendered_text(app)

    async def test_a_failed_check_still_opens_the_conversation(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        app = build_agent_app(handler, config=local_tools_config())
        app.chat._catalog = catalog_for(handler)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await adopt(pilot, app)
            assert not app.chat.turn.busy
            assert app.store.session_id == "s-paused"
