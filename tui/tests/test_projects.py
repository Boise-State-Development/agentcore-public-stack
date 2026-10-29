"""Shared Projects in the terminal (Tier 1).

A project conversation is an ordinary session whose every turn carries the
project's harness as ``rag_assistant_id``. These tests cover the four places
that fact has to be true: the catalogue that lists projects, the store that
remembers the binding, the request that carries it, and the screens that let
the user pick, see and leave a project.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from textual.app import App
from textual.widgets import OptionList

from agentcore_tui.client.agent_events import ErrorEvent
from agentcore_tui.client.catalog import ConversationSummary, Project
from agentcore_tui.conversation import ConversationStore, ProjectBinding
from agentcore_tui.credentials import CredentialSource
from agentcore_tui.screens.conversations import ConversationList, ConversationRow
from agentcore_tui.screens.project_picker import ProjectPicker
from agentcore_tui.turn import refusal_hint

from .conftest import make_config, rendered_text, send, sse_response
from .test_chat_screen import agent_turn, build_agent_app
from .test_pickers import Opened, catalog_for, project_wire, routing_handler, session_wire, status_text

# ---------------------------------------------------------------- catalogue


class TestProjectModel:
    def test_from_wire_reads_the_list_response_shape(self) -> None:
        project = Project.from_wire(project_wire("p1", "Grants", role="editor", status="archived"))
        assert project.project_id == "p1"
        assert project.harness_agent_id == "harness-p1"
        assert project.role == "editor" and not project.mine
        assert project.archived
        assert project.owner_label == "Pat Owner"

    def test_owner_label_falls_back_to_email(self) -> None:
        wire = project_wire("p1", "Grants")
        wire["ownerName"] = None
        assert Project.from_wire(wire).owner_label == "owner@example.edu"

    def test_summary_reads_project_binding_from_preferences(self) -> None:
        summary = ConversationSummary.from_wire(session_wire("s1", "t", project_id="p1", assistant_id="harness-p1"))
        assert summary.project_id == "p1" and summary.assistant_id == "harness-p1"
        plain = ConversationSummary.from_wire(session_wire("s2", "t"))
        assert plain.project_id is None and plain.assistant_id is None


class TestProjectsEndpoint:
    async def test_lists_active_mine_first_then_shared_then_archived(self) -> None:
        api = catalog_for(
            routing_handler(
                projects=[
                    project_wire("z", "Zed shared", role="viewer"),
                    project_wire("a", "Archived mine", status="archived"),
                    project_wire("m", "Mine"),
                    project_wire("b", "Beta shared", role="editor"),
                ]
            )
        )
        projects = await api.projects()
        await api.aclose()
        assert projects is not None
        assert [p.name for p in projects] == ["Mine", "Beta shared", "Zed shared", "Archived mine"]

    async def test_404_means_the_feature_is_off(self) -> None:
        api = catalog_for(routing_handler(projects_status=404))
        assert await api.projects() is None
        await api.aclose()


# -------------------------------------------------------------------- store


class TestStoreBinding:
    def test_new_conversation_keeps_the_project_by_default(self) -> None:
        store = ConversationStore()
        binding = ProjectBinding("p1", "Grants", "harness-p1")
        store.bind_project(binding)
        store.append_user("hi")
        store.reset()
        assert store.project == binding
        store.reset(keep_project=False)
        assert store.project is None

    def test_adopt_carries_a_binding(self) -> None:
        store = ConversationStore()
        binding = ProjectBinding("p1", "Grants", "harness-p1")
        store.adopt("s9", [], project=binding)
        assert store.project == binding
        store.adopt("s10", [])
        assert store.project is None


# ------------------------------------------------------------ request shape


def agent_app_with_capture(bodies: list[dict[str, Any]], *, projects: list[dict[str, Any]] | None = None, project_get: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/stream"):
            bodies.append(json.loads(request.content))
            return sse_response(agent_turn("ok"))
        if path.endswith("/projects"):
            return httpx.Response(200, json={"projects": projects or []})
        if "/projects/" in path and project_get is not None:
            return httpx.Response(200, json=project_get)
        return httpx.Response(404, json={"detail": "no route"})

    config = make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION)
    app = build_agent_app(handler, config=config)
    # The catalog client shares the handler through the same mock transport; the
    # chat screen would otherwise build one from a keyring session that tests
    # do not have.
    app.chat._catalog = catalog_for(handler)
    return app, handler


class TestRequestShape:
    async def test_a_bound_conversation_sends_the_harness_on_every_turn(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.store.bind_project(ProjectBinding("p1", "Grants", "harness-p1"))
            await send(pilot, app, "first")
            await send(pilot, app, "second")
        assert [b.get("rag_assistant_id") for b in bodies] == ["harness-p1", "harness-p1"]

    async def test_an_unbound_conversation_sends_no_assistant_field(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "hello")
        assert "rag_assistant_id" not in bodies[0]


# ------------------------------------------------------------------- picker


class Host(App[None]):
    pass


def sample_projects() -> list[Project]:
    return [
        Project.from_wire(project_wire("m", "Mine")),
        Project.from_wire(project_wire("s", "Shared", role="viewer")),
        Project.from_wire(project_wire("a", "Old", status="archived")),
    ]


class TestProjectPicker:
    async def test_groups_mine_and_shared_and_marks_the_current(self) -> None:
        app = Host()
        picker = ProjectPicker(sample_projects(), current="s")
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(picker)
            await pilot.pause()
            frame = rendered_text(app)
            assert "No project" in frame
            assert "Mine" in frame and "Shared with me" in frame
            assert "viewer" in frame and "Pat Owner" in frame
            assert "archived" in frame
            option_list = picker.query_one("#picker-list", OptionList)
            assert option_list.highlighted is not None
            assert option_list.get_option_at_index(option_list.highlighted).id == "s"

    async def test_enter_returns_the_project(self) -> None:
        app = Host()
        results: list[object] = []
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(ProjectPicker(sample_projects()), callback=results.append)
            await pilot.pause()
            await pilot.press("down")  # past the "Mine" heading onto the first project
            await pilot.press("enter")
            await pilot.pause()
        assert len(results) == 1 and isinstance(results[0], Project) and results[0].project_id == "m"

    async def test_no_project_returns_false_and_escape_returns_none(self) -> None:
        app = Host()
        results: list[object] = []
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(ProjectPicker(sample_projects(), current="m"), callback=results.append)
            await pilot.pause()
            await pilot.press("home")
            await pilot.press("enter")
            await pilot.pause()
            app.push_screen(ProjectPicker(sample_projects()), callback=results.append)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert results == [False, None]


# ------------------------------------------------------------ chat commands


class TestProjectCommand:
    async def test_project_by_name_binds_and_shows_in_the_status_bar(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies, projects=[project_wire("p1", "Grants"), project_wire("p2", "Other")])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project grants")
            await pilot.pause()
            assert app.store.project == ProjectBinding("p1", "Grants", "harness-p1")
            assert "⌂ Grants" in app.chat.status.line
            await send(pilot, app, "hello")
        assert bodies[0]["rag_assistant_id"] == "harness-p1"

    async def test_switching_project_mid_conversation_starts_a_new_one(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies, projects=[project_wire("p1", "Grants"), project_wire("p2", "Other")])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "hello")
            first_session = app.store.session_id
            await send(pilot, app, "/project other")
            await pilot.pause()
            assert app.store.session_id != first_session
            assert app.store.is_empty
            assert app.store.project is not None and app.store.project.name == "Other"

    async def test_project_off_leaves_and_clears_the_status_bar(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies, projects=[project_wire("p1", "Grants")])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project grants")
            await pilot.pause()
            await send(pilot, app, "/project off")
            await pilot.pause()
            assert app.store.project is None
            assert "⌂" not in app.chat.status.line

    async def test_ambiguous_and_unknown_names_do_not_bind(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies, projects=[project_wire("p1", "Grants A"), project_wire("p2", "Grants B")])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project grants")
            await pilot.pause()
            assert app.store.project is None
            await send(pilot, app, "/project nothing-like-this")
            await pilot.pause()
            assert app.store.project is None

    async def test_exact_name_wins_over_a_substring_match(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies, projects=[project_wire("p1", "Grants"), project_wire("p2", "Grants Archive")])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project grants")
            await pilot.pause()
            assert app.store.project is not None and app.store.project.project_id == "p1"

    async def test_new_conversation_stays_in_the_project(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies, projects=[project_wire("p1", "Grants")])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project grants")
            await pilot.pause()
            await send(pilot, app, "hello")
            await send(pilot, app, "/new")
            await pilot.pause()
            assert app.store.project is not None
            assert "⌂ Grants" in app.chat.status.line

    async def test_feature_off_is_reported_not_crashed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/projects"):
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(404)

        app = build_agent_app(handler)
        app.chat._catalog = catalog_for(handler)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project")
            await pilot.pause()
            assert app.store.project is None

    async def test_project_info_describes_the_bound_project(self) -> None:
        bodies: list[dict[str, Any]] = []
        wire = project_wire("p1", "Grants", role="editor")
        wire["description"] = "Grant applications for FY27"
        app, _ = agent_app_with_capture(bodies, projects=[wire], project_get=wire)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project grants")
            await pilot.pause()
            await send(pilot, app, "/project info")
            await pilot.pause()
            frame = rendered_text(app)
            assert "Grants" in frame and "Pat Owner" in frame and "editor" in frame
            assert "FY27" in frame
            assert app.store.is_empty


# ------------------------------------------------------ conversation list


class TestConversationListProjects:
    async def test_rows_in_a_project_show_its_name(self) -> None:
        handler = routing_handler(
            sessions=[session_wire("s1", "In Grants", project_id="p1", assistant_id="harness-p1"), session_wire("s2", "Plain")],
            projects=[project_wire("p1", "Grants")],
        )
        api = catalog_for(handler)
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            rows = {row.summary.session_id: row for row in screen.query(ConversationRow)}
            assert "⌂ Grants" in rows["s1"]._row_text()
            assert "⌂" not in rows["s2"]._row_text()
        await api.aclose()

    async def test_opening_hands_back_the_project(self) -> None:
        handler = routing_handler(
            sessions=[session_wire("s1", "In Grants", project_id="p1", assistant_id="harness-p1")],
            projects=[project_wire("p1", "Grants")],
        )
        api = catalog_for(handler)
        opened = Opened()
        screen = ConversationList(lambda: api, on_open=opened)
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
        assert len(opened.projects) == 1
        project = opened.projects[0]
        assert isinstance(project, Project) and project.project_id == "p1"
        await api.aclose()

    async def test_a_failed_projects_fetch_still_lists_conversations(self) -> None:
        handler = routing_handler(sessions=[session_wire("s1", "Plain")], projects_status=500)
        api = catalog_for(handler)
        screen = ConversationList(lambda: api, on_open=Opened())
        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            assert [r.summary.title for r in screen.query(ConversationRow)] == ["Plain"]
            assert "1 conversation(s)" in status_text(screen)
        await api.aclose()


class TestAdoptingAProjectConversation:
    async def test_resuming_rebinds_so_the_next_turn_carries_the_harness(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            summary = ConversationSummary.from_wire(session_wire("s7", "Old one", project_id="p1", assistant_id="harness-p1"))
            await app.chat._adopt_conversation(summary, [], Project.from_wire(project_wire("p1", "Grants")))
            await pilot.pause()
            assert app.store.project == ProjectBinding("p1", "Grants", "harness-p1")
            assert "⌂ Grants" in app.chat.status.line
            await send(pilot, app, "continue please")
        assert bodies[0]["session_id"] == "s7"
        assert bodies[0]["rag_assistant_id"] == "harness-p1"

    async def test_resuming_without_the_project_record_still_binds_from_the_session(self) -> None:
        """The project may be unlistable (left it, or the list failed) while the
        conversation still belongs to it; the stored assistantId keeps the turn
        acceptable to the server."""
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            summary = ConversationSummary.from_wire(session_wire("s7", "Old one", project_id="p1", assistant_id="harness-p1"))
            await app.chat._adopt_conversation(summary, [], None)
            await pilot.pause()
            assert app.store.project is not None and app.store.project.harness_agent_id == "harness-p1"
            await send(pilot, app, "go on")
        assert bodies[0]["rag_assistant_id"] == "harness-p1"

    async def test_resuming_a_plain_conversation_clears_any_binding(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.store.bind_project(ProjectBinding("p1", "Grants", "harness-p1"))
            app.chat.status.set_project("Grants")
            await app.chat._adopt_conversation(ConversationSummary.from_wire(session_wire("s8", "Plain")), [], None)
            await pilot.pause()
            assert app.store.project is None
            assert "⌂" not in app.chat.status.line


# ---------------------------------------------------------------- refusals


class TestRefusalHints:
    def _forbidden(self, message: str) -> ErrorEvent:
        return ErrorEvent(message=message, code="FORBIDDEN", recoverable=False)

    def test_archived_project_points_at_conversations_and_project_off(self) -> None:
        hint = refusal_hint(self._forbidden('The project "Grants" is archived, so it\'s read-only.'), in_project=True)
        assert "/project off" in hint and "/conversations" in hint

    def test_gone_project_points_at_leaving(self) -> None:
        hint = refusal_hint(self._forbidden("This project no longer exists."), in_project=True)
        assert "/project off" in hint

    def test_recoverable_errors_get_no_hint(self) -> None:
        event = ErrorEvent(message="Model is busy", code="THROTTLED", recoverable=True)
        assert refusal_hint(event, in_project=True) == ""

    def test_forbidden_outside_a_project_gets_no_project_hint(self) -> None:
        assert refusal_hint(self._forbidden("You may not use this agent."), in_project=False) == ""


class TestPinnedModel:
    async def test_a_project_that_pins_a_model_shows_it_as_set_by_the_project(self) -> None:
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/projects/p1/model"):
                return httpx.Response(200, json={"modelConfig": {"modelId": "us.anthropic.claude-sonnet-4-5-v1:0"}, "canEdit": False})
            if path.endswith("/projects"):
                return httpx.Response(200, json={"projects": [project_wire("p1", "Grants")]})
            if path.endswith("/chat/stream"):
                bodies.append(json.loads(request.content))
                return sse_response(agent_turn("ok"))
            return httpx.Response(404, json={"detail": "no route"})

        app = build_agent_app(handler, config=make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION))
        app.chat._catalog = catalog_for(handler)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project grants")
            await pilot.pause()
            assert "claude-sonnet-4-5 (set by Grants)" in app.chat.status.line
            await send(pilot, app, "/project off")
            await pilot.pause()
            assert "set by" not in app.chat.status.line

    async def test_a_project_without_a_pin_shows_the_users_model(self) -> None:
        bodies: list[dict[str, Any]] = []
        app, _ = agent_app_with_capture(bodies, projects=[project_wire("p1", "Grants")])
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/project grants")
            await pilot.pause()
            assert "set by" not in app.chat.status.line
