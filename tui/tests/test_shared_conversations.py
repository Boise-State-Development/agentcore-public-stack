"""A project's shared conversations: list, read the snapshot, continue as mine."""

from __future__ import annotations

import json
from typing import Any

import httpx
from textual.widgets import OptionList

from aps_tui.client.catalog import SharedConversation
from aps_tui.conversation import ProjectBinding
from aps_tui.screens.shared_conversations import SharedConversationList, SharedConversationView
from aps_tui.widgets import AssistantMessage, UserMessage

from .conftest import make_config, rendered_text, send, sse_response
from .test_chat_screen import agent_turn, build_agent_app
from .test_pickers import Host, catalog_for, project_wire, session_wire
from aps_tui.credentials import CredentialSource


def shared_wire(share_id: str, title: str, *, by: str = "Dana Reyes", mine: bool = False, note: str | None = None) -> dict[str, Any]:
    return {
        "shareId": share_id,
        "title": title,
        "sharedByEmail": "dana@example.edu",
        "sharedByName": by,
        "sharedAt": "2026-10-28T15:00:00Z",
        "shareUrl": f"https://x/shared/{share_id}",
        "isMine": mine,
        "note": note,
    }


def snapshot_messages() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": [{"text": "What does the rubric say about citations?"}]},
        {"role": "assistant", "content": [{"text": "APA 7, and every claim needs one."}]},
    ]


class Backend:
    """Serves projects, shared-tasks, the snapshot, export, metadata, history,
    and /chat/stream. Records what it was asked."""

    def __init__(self, *, shared: list[dict[str, Any]] | None = None, snapshot_status: int = 200) -> None:
        self.shared = shared if shared is not None else [shared_wire("sh1", "Citation rules", note="Read before grading")]
        self.snapshot_status = snapshot_status
        self.requests: list[httpx.Request] = []
        self.stream_bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/projects"):
            return httpx.Response(200, json={"projects": [project_wire("p1", "Grading")]})
        if path.endswith("/projects/p1/shared-tasks"):
            return httpx.Response(200, json={"tasks": self.shared})
        if "/shared/" in path:
            if self.snapshot_status != 200:
                return httpx.Response(self.snapshot_status, json={"detail": "Access denied"})
            return httpx.Response(200, json={"shareId": "sh1", "title": "Citation rules", "messages": snapshot_messages()})
        if path.endswith("/shares/sh1/export") and request.method == "POST":
            return httpx.Response(201, json={"sessionId": "fork-1", "title": "Citation rules (shared)"})
        if path.endswith("/sessions/fork-1/metadata"):
            return httpx.Response(200, json=session_wire("fork-1", "Citation rules (shared)", project_id="p1", assistant_id="harness-p1"))
        if path.endswith("/sessions/fork-1/messages"):
            return httpx.Response(200, json={"messages": snapshot_messages(), "nextToken": None, "pendingInterrupts": []})
        if path.endswith("/chat/stream"):
            self.stream_bodies.append(json.loads(request.content))
            return sse_response(agent_turn("Noted."))
        return httpx.Response(404, json={"detail": f"no route {path}"})


def app_for(backend: Backend):  # type: ignore[no-untyped-def]
    config = make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION)
    app = build_agent_app(backend, config=config)
    app.chat._catalog = catalog_for(backend)
    return app


class TestCatalog:
    async def test_lists_shared_conversations(self) -> None:
        api = catalog_for(Backend())
        [item] = await api.shared_conversations("p1")
        await api.aclose()
        assert item == SharedConversation("sh1", "Citation rules", "Dana Reyes", "2026-10-28T15:00:00Z", "Read before grading", False)

    async def test_continue_returns_the_new_session(self) -> None:
        api = catalog_for(Backend())
        assert await api.continue_shared("sh1") == ("fork-1", "Citation rules (shared)")
        await api.aclose()


class TestListScreen:
    async def test_rows_show_title_sharer_and_note(self) -> None:
        app = Host()
        items = [SharedConversation.from_wire(shared_wire("sh1", "Citation rules", note="Read before grading"))]
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(SharedConversationList("Grading", items))
            await pilot.pause()
            frame = rendered_text(app)
            assert "Shared in Grading" in frame
            assert "Citation rules" in frame and "Dana Reyes" in frame and "Read before grading" in frame

    async def test_empty_list_says_so(self) -> None:
        app = Host()
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(SharedConversationList("Grading", []))
            await pilot.pause()
            assert "Nobody has shared" in rendered_text(app)

    async def test_enter_returns_the_chosen_share(self) -> None:
        app = Host()
        results: list[object] = []
        items = [SharedConversation.from_wire(shared_wire("sh1", "A")), SharedConversation.from_wire(shared_wire("sh2", "B"))]
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(SharedConversationList("Grading", items), callback=results.append)
            await pilot.pause()
            app.screen.query_one("#picker-list", OptionList).highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
        assert [getattr(r, "share_id", r) for r in results] == ["sh2"]


class TestViewer:
    async def test_renders_the_snapshot_read_only(self) -> None:
        backend = Backend()
        api = catalog_for(backend)
        app = Host()
        item = SharedConversation.from_wire(shared_wire("sh1", "Citation rules"))
        continued: list[tuple[str, str]] = []

        async def on_continue(session_id: str, title: str) -> None:
            continued.append((session_id, title))

        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SharedConversationView(item, lambda: api, on_continue=on_continue, model_label="m"))
            await pilot.pause()
            await pilot.pause()
            view = app.screen
            assert len(view.query(UserMessage)) == 1 and len(view.query(AssistantMessage)) == 1
            frame = rendered_text(app)
            assert "rubric" in frame and "APA 7" in frame
            assert "Shared by Dana Reyes" in frame and "read-only" in frame
        assert continued == []
        await api.aclose()

    async def test_a_refused_snapshot_shows_the_reason(self) -> None:
        api = catalog_for(Backend(snapshot_status=403))
        app = Host()
        item = SharedConversation.from_wire(shared_wire("sh1", "Citation rules"))

        async def on_continue(session_id: str, title: str) -> None:  # pragma: no cover - not reached
            raise AssertionError

        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(SharedConversationView(item, lambda: api, on_continue=on_continue, model_label="m"))
            await pilot.pause()
            await pilot.pause()
            assert "Could not open it" in rendered_text(app)
        await api.aclose()


class TestChatIntegration:
    async def test_project_shared_needs_a_project(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project shared")
            await pilot.pause()
            assert not any(r.url.path.endswith("/shared-tasks") for r in backend.requests)

    async def test_read_then_continue_opens_the_copy_in_the_project(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.store.bind_project(ProjectBinding("p1", "Grading", "harness-p1"))
            await send(pilot, app, "/project shared")
            for _ in range(10):
                await pilot.pause()
                if isinstance(app.screen, SharedConversationList):
                    break
            await pilot.press("enter")  # read the first shared conversation
            for _ in range(10):
                await pilot.pause()
                if isinstance(app.screen, SharedConversationView) and app.screen.query(AssistantMessage):
                    break
            assert "APA 7" in rendered_text(app)
            await pilot.press("enter")  # continue as mine
            for _ in range(20):
                await pilot.pause()
                if app.store.session_id == "fork-1":
                    break
            assert app.store.session_id == "fork-1"
            assert app.store.project == ProjectBinding("p1", "Grading", "harness-p1")
            assert "⌂ Grading" in app.chat.status.line
            assert len(app.store.messages) == 2
            await send(pilot, app, "and footnotes?")
        assert any(r.method == "POST" and r.url.path.endswith("/shares/sh1/export") for r in backend.requests)
        assert backend.stream_bodies[-1]["session_id"] == "fork-1"
        assert backend.stream_bodies[-1]["rag_assistant_id"] == "harness-p1"
