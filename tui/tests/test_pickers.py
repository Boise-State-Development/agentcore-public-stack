"""Tests for the tool picker and the conversation list.

Both are driven through Textual's ``run_test`` pilot, so they also prove the
stylesheet parses. The catalogue is served by ``httpx.MockTransport`` through an
injected client, so no keyring and no network are involved.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, SelectionList

from aps_tui.client.auth import SessionAuth
from aps_tui.client.catalog import CatalogClient, ConversationSummary, HistoryMessage, StoredAttachment, StoredToolUse, Tool
from aps_tui.screens.conversations import (
    GROUP_ORDER,
    ConfirmDelete,
    ConversationList,
    ConversationRow,
    RenameConversation,
    group_for,
)
from aps_tui.screens.tool_picker import ToolPicker

from .conftest import rendered_text

BASE_URL = "https://screens.invalid/api"


def tool(tool_id: str, *, name: str = "", category: str = "utility", enabled: bool = False, **extra: Any) -> Tool:
    return Tool(tool_id=tool_id, name=name or tool_id, category=category, status="active", enabled=enabled, **extra)


async def answer_modal(pilot: Any, modal_type: type, value: Any, *, tries: int = 20) -> None:
    """Wait for a modal to appear, dismiss it with `value`, and let the caller settle.

    The actions that open a modal are `@work`-decorated, because
    `push_screen_wait` requires a worker. That makes them scheduled rather than
    awaited, so a fixed number of `pause()` calls is a race. This polls instead.
    """
    for _ in range(tries):
        if isinstance(pilot.app.screen, modal_type):
            pilot.app.screen.dismiss(value)
            break
        await pilot.pause()
    else:  # pragma: no cover - a genuine failure to open
        raise AssertionError(f"{modal_type.__name__} never appeared")
    for _ in range(tries):
        await pilot.pause()


def status_text(screen: ConversationList) -> str:
    """The status line's text.

    Textual 8.x Labels expose `.content`, not the `.renderable` older versions
    had; going through the widget keeps this in one place when that changes again.
    """
    return str(screen.status.content)


class Host(App[None]):
    """A bare App to push a screen onto."""

    CSS_PATH = "../src/aps_tui/app.tcss"

    def compose(self) -> ComposeResult:
        yield from ()


# ---------------------------------------------------------------------------
# Tool picker
# ---------------------------------------------------------------------------


class TestToolPicker:
    async def test_shows_the_servers_enabled_state(self) -> None:
        picker = ToolPicker([tool("a", enabled=True), tool("b", enabled=False)])
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(picker)
            await pilot.pause()
            assert set(picker.query_one("#tool-list", SelectionList).selected) == {"a"}

    async def test_returns_an_explicit_decision_for_every_tool(self) -> None:
        """Not just the enabled ids.

        A list of enabled ids cannot express "I turned this off", which is the
        case that matters for a tool a role enables by default.
        """
        picker = ToolPicker([tool("a", enabled=True), tool("b", enabled=False)])
        app = Host()
        result: list[dict[str, bool] | None] = []
        async with app.run_test() as pilot:
            app.push_screen(picker, callback=result.append)
            await pilot.pause()
            picker.action_save()
            await pilot.pause()

        assert result == [{"a": True, "b": False}]

    async def test_cancelling_discards_the_selection(self) -> None:
        picker = ToolPicker([tool("a", enabled=True)])
        app = Host()
        result: list[dict[str, bool] | None] = []
        async with app.run_test() as pilot:
            app.push_screen(picker, callback=result.append)
            await pilot.pause()
            picker.query_one("#tool-list", SelectionList).deselect_all()
            picker.action_cancel()
            await pilot.pause()

        assert result == [None]

    async def test_select_all_and_none(self) -> None:
        picker = ToolPicker([tool("a"), tool("b"), tool("c")])
        app = Host()
        result: list[dict[str, bool] | None] = []
        async with app.run_test() as pilot:
            app.push_screen(picker, callback=result.append)
            await pilot.pause()
            picker.action_select_all()
            await pilot.pause()
            assert all(picker._decisions().values())
            picker.action_select_none()
            await pilot.pause()
            assert not any(picker._decisions().values())
            picker.action_save()
            await pilot.pause()

        assert result == [{"a": False, "b": False, "c": False}]

    async def test_groups_by_category(self) -> None:
        """Category headings are disabled rows, so they cannot be selected into
        the result."""
        picker = ToolPicker([tool("a", category="code"), tool("b", category="search")])
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(picker)
            await pilot.pause()
            picker.action_select_all()
            await pilot.pause()
            assert set(picker._decisions()) == {"a", "b"}

    async def test_flags_a_tool_that_needs_a_sign_in(self) -> None:
        """Choosing it can pause a turn for consent the terminal cannot give."""
        picker = ToolPicker([tool("gdrive", requires_oauth_provider="google")])
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(picker)
            await pilot.pause()
            rendered = " ".join(str(option.prompt) for option in picker.query_one("#tool-list", SelectionList).options)
        assert "needs sign-in" in rendered

    async def test_an_empty_catalogue_says_so(self) -> None:
        picker = ToolPicker([])
        app = Host()
        result: list[dict[str, bool] | None] = []
        async with app.run_test() as pilot:
            app.push_screen(picker, callback=result.append)
            await pilot.pause()
            assert picker.query("#tool-picker-empty")
            picker.action_save()
            await pilot.pause()
        assert result == [{}]

    async def test_the_buttons_match_the_bindings(self) -> None:
        picker = ToolPicker([tool("a", enabled=True)])
        app = Host()
        result: list[dict[str, bool] | None] = []
        async with app.run_test() as pilot:
            app.push_screen(picker, callback=result.append)
            await pilot.pause()
            await pilot.click("#tool-cancel")
            await pilot.pause()
        assert result == [None]


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


class TestGrouping:
    @pytest.mark.parametrize(
        ("timestamp", "expected"),
        [
            ("2026-08-08T09:00:00Z", "Today"),
            ("2026-08-07T23:00:00Z", "Yesterday"),
            ("2026-08-03T09:00:00Z", "Last 7 days"),
            ("2026-07-20T09:00:00Z", "Last 30 days"),
            ("2026-01-01T09:00:00Z", "Older"),
        ],
    )
    def test_buckets_match_the_web_app(self, timestamp: str, expected: str) -> None:
        now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        assert group_for(timestamp, now=now) == expected

    @pytest.mark.parametrize("timestamp", ["", "not a date", "2026-13-45"])
    def test_an_unusable_timestamp_still_lands_somewhere(self, timestamp: str) -> None:
        """A row the server sent must always be reachable."""
        assert group_for(timestamp) == "Older"

    def test_a_naive_timestamp_is_treated_as_utc(self) -> None:
        now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        assert group_for("2026-08-08T09:00:00", now=now) == "Today"

    def test_every_bucket_has_a_heading(self) -> None:
        now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        produced = {
            group_for(stamp, now=now)
            for stamp in ("2026-08-08T09:00:00Z", "2026-08-07T09:00:00Z", "2026-08-03T09:00:00Z", "2026-07-20T09:00:00Z", "")
        }
        assert produced <= set(GROUP_ORDER)


# ---------------------------------------------------------------------------
# Conversation list
# ---------------------------------------------------------------------------


def session_wire(
    session_id: str,
    title: str,
    *,
    last: str = "2026-08-08T11:00:00Z",
    unread: bool = False,
    project_id: str | None = None,
    assistant_id: str | None = None,
) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "sessionId": session_id,
        "title": title,
        "status": "active",
        "createdAt": last,
        "lastMessageAt": last,
        "messageCount": 2,
        "unread": unread,
    }
    if project_id or assistant_id:
        wire["preferences"] = {"projectId": project_id, "assistantId": assistant_id}
    return wire


def project_wire(project_id: str, name: str, *, role: str = "owner", status: str = "active", harness: str | None = None) -> dict[str, Any]:
    return {
        "projectId": project_id,
        "name": name,
        "description": "",
        "ownerEmail": "owner@example.edu",
        "ownerName": "Pat Owner",
        "role": role,
        "status": status,
        "editorsManageMembers": True,
        "memberCount": 3,
        "harnessAgentId": harness or f"harness-{project_id}",
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-08T00:00:00Z",
    }


def routing_handler(
    *,
    sessions: list[dict[str, Any]] | None = None,
    next_token: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    capture: list[httpx.Request] | None = None,
    projects: list[dict[str, Any]] | None = None,
    projects_status: int = 200,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        path = request.url.path
        if path.endswith("/projects"):
            if projects_status != 200:
                return httpx.Response(projects_status, json={"detail": "nope"})
            return httpx.Response(200, json={"projects": projects or []})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": messages or [], "nextToken": None})
        if path.endswith("/sessions"):
            body: dict[str, Any] = {"sessions": sessions or []}
            if next_token and not request.url.params.get("next_token"):
                body["nextToken"] = next_token
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={})

    return handler


def catalog_for(handler: Any) -> CatalogClient:
    return CatalogClient(BASE_URL, auth=SessionAuth("s"), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class Opened:
    """Records what the screen handed back."""

    def __init__(self) -> None:
        self.calls: list[tuple[ConversationSummary, list[HistoryMessage]]] = []
        self.projects: list[object] = []

    async def __call__(self, summary: ConversationSummary, messages: list[HistoryMessage], project: object = None) -> None:
        self.calls.append((summary, messages))
        self.projects.append(project)


class TestConversationList:
    async def test_lists_conversations_under_date_headings(self) -> None:
        handler = routing_handler(
            sessions=[
                session_wire("s1", "Today thing", last="2026-08-08T11:00:00Z"),
                session_wire("s2", "Ancient thing", last="2020-01-01T11:00:00Z"),
            ]
        )
        api = catalog_for(handler)
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()

            titles = [item.summary.title for item in screen.query(ConversationRow)]
            assert titles == ["Today thing", "Ancient thing"]
            assert "2 conversation(s)" in status_text(screen)
        await api.aclose()

    async def test_the_first_conversation_is_highlighted_not_its_heading(self) -> None:
        """Enter must open something immediately; the heading row is disabled."""
        api = catalog_for(routing_handler(sessions=[session_wire("s1", "Only thing")]))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            selected = screen.selected()
            assert selected is not None
            assert selected.title == "Only thing"
        await api.aclose()

    async def test_an_empty_list_says_so_rather_than_looking_broken(self) -> None:
        api = catalog_for(routing_handler(sessions=[]))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            assert "No conversations yet" in status_text(screen)
        await api.aclose()

    async def test_a_failed_fetch_is_reported_in_place(self) -> None:
        api = catalog_for(lambda _r: httpx.Response(500, json={"detail": "database down"}))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            assert "database down" in status_text(screen)
        await api.aclose()

    async def test_opening_restores_history_and_hands_it_over(self) -> None:
        opened = Opened()
        api = catalog_for(
            routing_handler(
                sessions=[session_wire("s1", "Arithmetic")],
                messages=[
                    {"id": "m1", "role": "user", "createdAt": "t", "content": [{"type": "text", "text": "2+2"}]},
                    {"id": "m2", "role": "assistant", "createdAt": "t", "content": [{"type": "text", "text": "4"}]},
                ],
            )
        )
        screen = ConversationList(lambda: api, on_open=opened)
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.list_view.index = 1  # 0 is the date heading
            await screen.action_open()
            await pilot.pause()

            assert len(opened.calls) == 1
            summary, messages = opened.calls[0]
            assert summary.session_id == "s1"
            assert [(m.role, m.text) for m in messages] == [("user", "2+2"), ("assistant", "4")]
        await api.aclose()

    async def test_opening_marks_the_conversation_read(self) -> None:
        captured: list[httpx.Request] = []
        api = catalog_for(routing_handler(sessions=[session_wire("s1", "A", unread=True)], capture=captured))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.list_view.index = 1
            await screen.action_open()
            await pilot.pause()

        assert any(request.url.path.endswith("/read") for request in captured)
        await api.aclose()

    async def test_a_heading_cannot_be_opened(self) -> None:
        opened = Opened()
        api = catalog_for(routing_handler(sessions=[session_wire("s1", "A")]))
        screen = ConversationList(lambda: api, on_open=opened)
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.list_view.index = 0  # the date heading
            await screen.action_open()
            await pilot.pause()

        assert opened.calls == []
        await api.aclose()

    async def test_rename_sends_the_new_title_and_updates_the_row(self) -> None:
        captured: list[httpx.Request] = []
        api = catalog_for(routing_handler(sessions=[session_wire("s1", "Old name")], capture=captured))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.list_view.index = 1

            screen.action_rename()
            await answer_modal(pilot, RenameConversation, "New name")

            rename = [r for r in captured if r.method == "PUT"]
            assert rename and json.loads(rename[0].content) == {"title": "New name"}
            # Updated in place rather than by refetching, so a local edit costs
            # no round trip.
            assert [item.summary.title for item in screen.query(ConversationRow)] == ["New name"]
        await api.aclose()

    async def test_delete_asks_first(self) -> None:
        captured: list[httpx.Request] = []
        api = catalog_for(routing_handler(sessions=[session_wire("s1", "Doomed")], capture=captured))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.list_view.index = 1

            screen.action_delete()
            await answer_modal(pilot, ConfirmDelete, False)

            assert not [r for r in captured if r.method == "DELETE"]
            assert len(screen.query(ConversationRow)) == 1
        await api.aclose()

    async def test_confirming_delete_removes_the_row(self) -> None:
        captured: list[httpx.Request] = []
        api = catalog_for(routing_handler(sessions=[session_wire("s1", "Doomed")], capture=captured))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.list_view.index = 1

            screen.action_delete()
            await answer_modal(pilot, ConfirmDelete, True)

            assert [r.method for r in captured if r.method == "DELETE"] == ["DELETE"]
            assert not screen.query(ConversationRow)
        await api.aclose()

    async def test_load_more_only_when_there_is_a_cursor(self) -> None:
        captured: list[httpx.Request] = []
        api = catalog_for(routing_handler(sessions=[session_wire("s1", "A")], capture=captured))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            before = len(captured)
            await screen.action_load_more()
            await pilot.pause()
            assert len(captured) == before
        await api.aclose()

    async def test_more_is_advertised_when_a_cursor_exists(self) -> None:
        api = catalog_for(routing_handler(sessions=[session_wire("s1", "A")], next_token="cursor-2"))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            assert "press m for more" in status_text(screen)
        await api.aclose()


class TestModals:
    async def test_rename_refuses_an_empty_title(self) -> None:
        """The server would accept it and replace a useful name with nothing."""
        app = Host()
        result: list[str | None] = []
        async with app.run_test() as pilot:
            app.push_screen(RenameConversation("Old"), callback=result.append)
            await pilot.pause()
            app.screen.query_one("#rename-input", Input).value = "   "
            await pilot.press("enter")
            await pilot.pause()
        assert result == [None]

    async def test_confirm_delete_focuses_cancel(self) -> None:
        """So Enter on a reflex does the safe thing."""
        app = Host()
        async with app.run_test() as pilot:
            screen = ConfirmDelete("Doomed")
            app.push_screen(screen)
            await pilot.pause()
            assert app.focused is screen.query_one("#confirm-no", Button)


class TestStoredToolBlocks:
    """A saved conversation's tool calls render like a live turn's."""

    WIRE = [
        {"id": "m1", "role": "user", "createdAt": "t", "content": [{"type": "text", "text": "weather in boise?"}]},
        {
            "id": "m2",
            "role": "assistant",
            "createdAt": "t",
            "content": [
                {"type": "text", "text": "Checking."},
                {"type": "toolUse", "toolUse": {"toolUseId": "tu1", "name": "get_weather", "input": {"city": "Boise"}}},
            ],
        },
        {
            "id": "m3",
            "role": "user",
            "createdAt": "t",
            "content": [{"type": "toolResult", "toolResult": {"toolUseId": "tu1", "status": "success", "content": [{"json": {"temp": 71}}]}}],
        },
        {"id": "m4", "role": "assistant", "createdAt": "t", "content": [{"type": "text", "text": "71°F."}]},
        {
            "id": "m5",
            "role": "user",
            "createdAt": "t",
            "content": [
                {"type": "document", "document": {"name": "notes.pdf", "format": "pdf"}},
                {"type": "text", "text": "summarise"},
                {"type": "video"},
            ],
        },
    ]

    def test_history_message_keeps_structured_blocks(self) -> None:
        m2 = HistoryMessage.from_wire(self.WIRE[1])
        assert m2.tool_uses == (StoredToolUse(tool_use_id="tu1", name="get_weather", arguments={"city": "Boise"}),)
        m3 = HistoryMessage.from_wire(self.WIRE[2])
        assert m3.tool_results[0].tool_use_id == "tu1"
        assert json.loads(m3.tool_results[0].text) == {"temp": 71}
        assert not m3.tool_results[0].is_error
        m5 = HistoryMessage.from_wire(self.WIRE[4])
        assert m5.attachments == (StoredAttachment(kind="document", name="notes.pdf", format="pdf"),)
        assert m5.other_blocks == ("video",)

    async def test_adopting_renders_tool_calls_and_attachments(self) -> None:
        from aps_tui.widgets import ToolCall
        from aps_tui.widgets.messages import Notice

        from .conftest import rendered_text, sse_response
        from .test_chat_screen import agent_turn, build_agent_app

        app = build_agent_app(lambda _r: sse_response(agent_turn()))
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            summary = ConversationSummary(session_id="s1", title="Weather", message_count=4)
            messages = [HistoryMessage.from_wire(w) for w in self.WIRE]
            await app.chat._adopt_conversation(summary, messages)
            await pilot.pause()

            [tool] = app.chat.query(ToolCall)
            assert tool.has_class("-finished")
            frame = rendered_text(app)
            assert "get_weather · Boise" in frame
            assert "Attached: notes.pdf" in frame
            assert "1 stored item(s)" in frame
            # Only prose reaches the store: the tool-result-only message is not a turn.
            assert [m.role for m in app.chat.store.messages] == ["user", "assistant", "assistant", "user"]
            assert not any("not shown" in str(n.render()) for n in app.chat.query(Notice))


class TestConversationFilter:
    SESSIONS = [
        session_wire("s1", "Grant budget sanity check", last="2026-08-08T11:00:00Z"),
        session_wire("s2", "Draft syllabus", last="2026-08-07T11:00:00Z"),
        session_wire("s3", "Budget for the lab", last="2020-01-01T11:00:00Z"),
    ]

    async def test_typing_filters_titles_locally(self) -> None:
        api = catalog_for(routing_handler(sessions=self.SESSIONS))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.filter_input.focus()
            for ch in "budget":
                await pilot.press(ch)
            await pilot.pause()
            titles = [item.summary.title for item in screen.query(ConversationRow)]
            assert titles == ["Grant budget sanity check", "Budget for the lab"]
            assert "2 of 3 match" in status_text(screen)
        await api.aclose()

    async def test_no_match_says_so(self) -> None:
        api = catalog_for(routing_handler(sessions=self.SESSIONS))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.filter_input.focus()
            for ch in "zzz":
                await pilot.press(ch)
            await pilot.pause()
            assert not screen.query(ConversationRow)
            assert "No matches" in status_text(screen)
        await api.aclose()

    async def test_enter_searches_the_server_when_enabled(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/sessions/search"):
                assert request.url.params["q"] == "lab"
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "sessionId": "s9",
                                "title": "Lab notes",
                                "lastMessageAt": "2026-08-08T11:00:00Z",
                                "matchKind": "text",
                                "snippet": "the lab needs a fridge",
                            }
                        ],
                        "textSearchAvailable": True,
                    },
                )
            return routing_handler(sessions=self.SESSIONS)(request)

        api = catalog_for(handler)
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.filter_input.focus()
            for ch in "lab":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            titles = [item.summary.title for item in screen.query(ConversationRow)]
            assert titles == ["Lab notes"]
            assert "1 result(s)" in status_text(screen)
            from .conftest import rendered_text

            frame = rendered_text(app)
            assert "the lab needs a fridge" in frame
        await api.aclose()

    async def test_enter_falls_back_when_search_is_off(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/sessions/search"):
                return httpx.Response(404, json={"detail": "Not Found"})
            return routing_handler(sessions=self.SESSIONS)(request)

        api = catalog_for(handler)
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.filter_input.focus()
            for ch in "budget":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert len(screen.query(ConversationRow)) == 2
            assert "message search is off" in status_text(screen)
        await api.aclose()


class TestBulkDelete:
    @staticmethod
    def _handler(captured: list[httpx.Request], sessions: list[dict[str, Any]], *, fail: set[str] = frozenset()) -> Any:
        base = routing_handler(sessions=sessions, capture=captured)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/sessions/bulk-delete"):
                captured.append(request)
                ids = json.loads(request.content)["sessionIds"]
                results = [{"sessionId": i, "success": i not in fail, "error": None if i not in fail else "boom"} for i in ids]
                ok = sum(1 for r in results if r["success"])
                return httpx.Response(200, json={"deletedCount": ok, "failedCount": len(ids) - ok, "results": results})
            return base(request)

        return handler

    async def test_space_marks_rows_and_delete_removes_all_marked_in_one_request(self) -> None:
        captured: list[httpx.Request] = []
        sessions = [session_wire("s1", "One"), session_wire("s2", "Two"), session_wire("s3", "Three")]
        api = catalog_for(self._handler(captured, sessions))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.list_view.index = 1
            screen.list_view.focus()
            await pilot.press("space")  # marks One, moves to Two
            await pilot.press("space")  # marks Two, moves to Three
            await pilot.pause()
            assert "2 marked" in rendered_text(app)
            assert [item.marked for item in screen.query(ConversationRow)] == [True, True, False]

            screen.action_delete()
            await answer_modal(pilot, ConfirmDelete, True)

            bulk = [r for r in captured if r.url.path.endswith("/bulk-delete")]
            assert (
                len(bulk) == 1
                and json.loads(bulk[0].content) == {"sessionIds": ["s1", "s2"]}
                or json.loads(bulk[0].content) == {"sessionIds": ["s2", "s1"]}
            )
            assert not [r for r in captured if r.method == "DELETE"]
            assert [item.summary.title for item in screen.query(ConversationRow)] == ["Three"]
            assert "marked" not in rendered_text(app)
        await api.aclose()

    async def test_space_again_unmarks(self) -> None:
        api = catalog_for(routing_handler(sessions=[session_wire("s1", "One"), session_wire("s2", "Two")]))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.list_view.index = 1
            screen.list_view.focus()
            await pilot.press("space")
            await pilot.press("up")
            await pilot.press("space")
            await pilot.pause()
            assert not any(item.marked for item in screen.query(ConversationRow))
            assert "marked" not in rendered_text(app)
        await api.aclose()

    async def test_the_confirmation_names_the_count(self) -> None:
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(ConfirmDelete(["A", "B", "C"]))
            await pilot.pause()
            text = rendered_text(app)
            assert "Delete these 3 conversations?" in text and "A" in text and "C" in text

    async def test_failures_stay_marked_and_are_reported(self) -> None:
        captured: list[httpx.Request] = []
        sessions = [session_wire("s1", "One"), session_wire("s2", "Two")]
        api = catalog_for(self._handler(captured, sessions, fail={"s2"}))
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            screen.list_view.index = 1
            screen.list_view.focus()
            await pilot.press("space")
            await pilot.press("space")
            screen.action_delete()
            await answer_modal(pilot, ConfirmDelete, True)
            assert [item.summary.title for item in screen.query(ConversationRow)] == ["Two"]
            assert screen._marked == {"s2"}
        await api.aclose()


class TestDeleteManyCatalog:
    async def test_chunks_at_the_server_limit_and_returns_deleted_ids(self) -> None:
        bodies: list[list[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            ids = json.loads(request.content)["sessionIds"]
            bodies.append(ids)
            results = [{"sessionId": i, "success": i != "x7"} for i in ids]
            return httpx.Response(200, json={"deletedCount": 0, "failedCount": 0, "results": results})

        api = catalog_for(handler)
        ids = [f"x{i}" for i in range(25)]
        deleted = await api.delete_many(ids)
        assert [len(b) for b in bodies] == [20, 5]
        assert deleted == set(ids) - {"x7"}
        await api.aclose()
