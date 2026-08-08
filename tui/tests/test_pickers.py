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

from agentcore_tui.client.auth import SessionAuth
from agentcore_tui.client.catalog import CatalogClient, ConversationSummary, HistoryMessage, Tool
from agentcore_tui.screens.conversations import (
    GROUP_ORDER,
    ConfirmDelete,
    ConversationList,
    ConversationRow,
    RenameConversation,
    group_for,
)
from agentcore_tui.screens.tool_picker import ToolPicker

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

    CSS_PATH = "../src/agentcore_tui/app.tcss"

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


def session_wire(session_id: str, title: str, *, last: str = "2026-08-08T11:00:00Z", unread: bool = False) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "title": title,
        "status": "active",
        "createdAt": last,
        "lastMessageAt": last,
        "messageCount": 2,
        "unread": unread,
    }


def routing_handler(
    *,
    sessions: list[dict[str, Any]] | None = None,
    next_token: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    capture: list[httpx.Request] | None = None,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        path = request.url.path
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

    async def __call__(self, summary: ConversationSummary, messages: list[HistoryMessage]) -> None:
        self.calls.append((summary, messages))


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
