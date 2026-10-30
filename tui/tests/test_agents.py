"""Talking to an Agent from the terminal (``/agent``).

Same wire fact as a project — ``rag_assistant_id`` on every turn — so these
tests mostly prove the two bindings stay mutually exclusive and that the Agent
list has the web app's ``@``-menu scope: own and shared, then pins.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from aps_tui.client.agent_events import ErrorEvent
from aps_tui.client.catalog import Agent, ConversationSummary
from aps_tui.conversation import AgentBinding, ConversationStore, ProjectBinding
from aps_tui.credentials import CredentialSource
from aps_tui.screens.agent_picker import AgentPicker
from aps_tui.turn import refusal_hint

from .conftest import make_config, rendered_text, send, sse_response
from .test_chat_screen import agent_turn, build_agent_app
from .test_pickers import Host, catalog_for, project_wire, session_wire


def agent_wire(agent_id: str, name: str, *, shared: bool = False, status: str = "COMPLETE", **extra: Any) -> dict[str, Any]:
    wire = {
        "agentId": agent_id,
        "ownerName": "Dana Reyes" if shared else "You",
        "name": name,
        "description": f"{name} does things",
        "visibility": "SHARED" if shared else "PRIVATE",
        "usageCount": 0,
        "status": status,
        "createdAt": "2026-10-01T00:00:00Z",
        "updatedAt": "2026-10-01T00:00:00Z",
        "isSharedWithMe": shared,
    }
    wire.update(extra)
    return wire


def pin_wire(agent_id: str, name: str, *, publisher: str = "Library") -> dict[str, Any]:
    return {
        "agentId": agent_id,
        "name": name,
        "tagline": "Ask the library",
        "publisher": {"label": publisher, "kind": "department"},
        "source": "role",
    }


class Backend:
    def __init__(
        self,
        *,
        agents: list[dict[str, Any]] | None = None,
        pins: list[dict[str, Any]] | None = None,
        agents_status: int = 200,
        pins_status: int = 200,
    ) -> None:
        self.agents = agents if agents is not None else [agent_wire("a1", "Grader"), agent_wire("a2", "Shared helper", shared=True)]
        self.pins = pins if pins is not None else [pin_wire("a3", "Library bot")]
        self.agents_status = agents_status
        self.pins_status = pins_status
        self.stream_bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agents/pins"):
            if self.pins_status != 200:
                return httpx.Response(self.pins_status, json={"detail": "Not found"})
            return httpx.Response(200, json={"pins": self.pins})
        if path.endswith("/agents"):
            if self.agents_status != 200:
                return httpx.Response(self.agents_status, json={"detail": "Not found"})
            return httpx.Response(200, json={"agents": self.agents})
        if "/agents/" in path:
            agent_id = path.rsplit("/", 1)[-1]
            for wire in self.agents:
                if wire["agentId"] == agent_id:
                    return httpx.Response(200, json={**wire, "starters": ["Grade this essay"], "modelLabel": "Claude Sonnet"})
            return httpx.Response(404, json={"detail": "Not found"})
        if path.endswith("/projects"):
            return httpx.Response(200, json={"projects": [project_wire("p1", "Grants")]})
        if path.endswith("/chat/stream"):
            self.stream_bodies.append(json.loads(request.content))
            return sse_response(agent_turn("ok"))
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [], "nextToken": None, "pendingInterrupts": []})
        return httpx.Response(404, json={"detail": f"no route {path}"})


def app_for(backend: Backend):  # type: ignore[no-untyped-def]
    app = build_agent_app(backend, config=make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION))
    app.chat._catalog = catalog_for(backend)
    return app


class TestCatalog:
    async def test_own_then_shared_then_pins_without_duplicates_or_drafts(self) -> None:
        backend = Backend(
            agents=[agent_wire("a2", "Zed shared", shared=True), agent_wire("a1", "Mine"), agent_wire("d", "Half done", status="DRAFT")],
            pins=[pin_wire("a1", "Mine again"), pin_wire("a3", "Library bot")],
        )
        api = catalog_for(backend)
        agents = await api.agents()
        await api.aclose()
        assert agents is not None
        assert [(a.agent_id, a.source) for a in agents] == [("a1", "own"), ("a2", "shared"), ("a3", "pinned")]
        assert agents[2].owner == "Library"

    async def test_agents_off_is_none_and_pins_off_still_lists(self) -> None:
        api = catalog_for(Backend(agents_status=404))
        assert await api.agents() is None
        await api.aclose()
        api = catalog_for(Backend(pins_status=404))
        agents = await api.agents()
        await api.aclose()
        assert agents is not None and [a.source for a in agents] == ["own", "shared"]


class TestStore:
    def test_agent_and_project_are_mutually_exclusive(self) -> None:
        store = ConversationStore()
        store.bind_project(ProjectBinding("p1", "Grants", "h1"))
        store.bind_agent(AgentBinding("a1", "Grader"))
        assert store.project is None and store.bound_agent_id == "a1"
        store.bind_project(ProjectBinding("p1", "Grants", "h1"))
        assert store.agent is None and store.bound_agent_id == "h1"

    def test_reset_keeps_the_agent_and_adopt_replaces_it(self) -> None:
        store = ConversationStore()
        store.bind_agent(AgentBinding("a1", "Grader"))
        store.reset()
        assert store.agent == AgentBinding("a1", "Grader")
        store.adopt("s2", [])
        assert store.agent is None and store.bound_agent_id is None


class TestPicker:
    async def test_groups_and_returns_the_choice(self) -> None:
        agents = [
            Agent("a1", "Grader", source="own"),
            Agent("a2", "Shared helper", source="shared", owner="Dana Reyes"),
            Agent("a3", "Library bot", source="pinned", owner="Library", summary="Ask the library"),
        ]
        app = Host()
        results: list[object] = []
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(AgentPicker(agents), callback=results.append)
            await pilot.pause()
            frame = rendered_text(app)
            assert "Mine" in frame and "Shared with me" in frame and "Pinned" in frame
            assert "Dana Reyes" in frame and "Ask the library" in frame
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
        assert isinstance(results[0], Agent) and results[0].agent_id == "a1"


class TestCommand:
    async def test_agent_by_name_binds_sends_and_shows(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/agent grader")
            await pilot.pause()
            assert app.store.agent == AgentBinding("a1", "Grader")
            assert "@ Grader" in app.chat.status.line
            await send(pilot, app, "hello")
        assert backend.stream_bodies[0]["rag_assistant_id"] == "a1"

    async def test_choosing_an_agent_leaves_the_project_and_vice_versa(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project grants")
            await pilot.pause()
            await send(pilot, app, "/agent grader")
            await pilot.pause()
            assert app.store.project is None and app.store.agent is not None
            assert "⌂" not in app.chat.status.line and "@ Grader" in app.chat.status.line
            await send(pilot, app, "/project grants")
            await pilot.pause()
            assert app.store.agent is None and app.store.project is not None
            assert "@" not in app.chat.status.line

    async def test_agent_off(self) -> None:
        app = app_for(Backend())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/agent grader")
            await pilot.pause()
            await send(pilot, app, "/agent off")
            await pilot.pause()
            assert app.store.agent is None and "@" not in app.chat.status.line

    async def test_info_shows_model_and_starters(self) -> None:
        app = app_for(Backend())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/agent grader")
            await pilot.pause()
            await send(pilot, app, "/agent info")
            await pilot.pause()
            frame = rendered_text(app)
            assert "Claude Sonnet" in frame and "Grade this essay" in frame

    async def test_agents_off_is_reported(self) -> None:
        app = app_for(Backend(agents_status=404))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/agent")
            await pilot.pause()
            assert app.store.agent is None


class TestAdoption:
    async def test_opening_an_agent_conversation_rebinds_by_its_stored_id(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            summary = ConversationSummary.from_wire(session_wire("s9", "With grader", assistant_id="a1"))
            await app.chat._adopt_conversation(summary, [], None)
            await pilot.pause()
            assert app.store.agent == AgentBinding("a1", "Grader")
            assert "@ Grader" in app.chat.status.line
            await send(pilot, app, "more")
        assert backend.stream_bodies[0]["rag_assistant_id"] == "a1"

    async def test_an_unknown_agent_still_rebinds_with_a_placeholder_name(self) -> None:
        backend = Backend(agents=[])
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            summary = ConversationSummary.from_wire(session_wire("s9", "With gone agent", assistant_id="gone"))
            await app.chat._adopt_conversation(summary, [], None)
            await pilot.pause()
            assert app.store.bound_agent_id == "gone"


class TestRefusal:
    def test_agent_refusal_points_at_agent_off(self) -> None:
        event = ErrorEvent(message="This agent or project isn't available to you.", code="FORBIDDEN", recoverable=False)
        assert "/agent off" in refusal_hint(event, in_project=False, in_agent=True)


class TestPinnedModel:
    async def test_an_agent_that_pins_a_model_shows_it(self) -> None:
        backend = Backend(agents=[agent_wire("a1", "Grader", modelConfig={"modelId": "us.anthropic.claude-sonnet-4-5-v1:0"})])
        app = app_for(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/agent grader")
            await pilot.pause()
            assert "claude-sonnet-4-5 (set by Grader)" in app.chat.status.line
            # The wire still carries the user's own model; the server applies the pin.
            await send(pilot, app, "hi")
        assert backend.stream_bodies[0]["rag_assistant_id"] == "a1"
