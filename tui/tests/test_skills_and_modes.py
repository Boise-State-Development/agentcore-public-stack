"""Skills (``/skills``, ``/<slug>`` in a message) and conversation modes (``/mode``)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from agentcore_tui.client.catalog import ConversationSummary, Skill
from agentcore_tui.conversation import AgentBinding
from agentcore_tui.credentials import CredentialSource
from agentcore_tui.screens.mode_picker import ModePicker
from agentcore_tui.screens.skill_picker import SkillPicker

from .conftest import make_config, rendered_text, send, sse_response
from .test_chat_screen import agent_turn, build_agent_app
from .test_commands import type_text
from .test_pickers import Host, catalog_for, session_wire


def skill_wire(skill_id: str, name: str, *, slug: str, enabled: bool) -> dict[str, Any]:
    return {
        "skillId": skill_id,
        "displayName": name,
        "description": f"{name} helper",
        "category": None,
        "userEnabled": enabled,
        "isEnabled": enabled,
        "slug": slug,
    }


class Backend:
    def __init__(self, *, skills_status: int = 200, modes_status: int = 200) -> None:
        self.skills = [
            skill_wire("sk-xlsx", "Spreadsheets", slug="xlsx", enabled=True),
            skill_wire("sk-pdf", "PDF tools", slug="pdf", enabled=False),
            skill_wire("sk-new", "New thing", slug="new", enabled=True),  # collides with /new
        ]
        self.modes = [
            {"prompt_id": "m-soc", "name": "Socratic", "description": "Ask, don't tell"},
            {"prompt_id": "m-brief", "name": "Brief", "description": "Short answers"},
        ]
        self.skills_status = skills_status
        self.modes_status = modes_status
        self.stream_bodies: list[dict[str, Any]] = []
        self.preference_puts: list[dict[str, Any]] = []
        self.metadata_puts: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/skills/preferences"):
            self.preference_puts.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if path.endswith("/skills/"):
            if self.skills_status != 200:
                return httpx.Response(self.skills_status, json={"detail": "Not Found"})
            return httpx.Response(200, json={"skills": self.skills, "totalCount": len(self.skills)})
        if path.endswith("/system-prompts/"):
            if self.modes_status != 200:
                return httpx.Response(self.modes_status, json={"detail": "Not Found"})
            return httpx.Response(200, json={"prompts": self.modes})
        if path.endswith("/metadata") and request.method == "PUT":
            self.metadata_puts.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [], "nextToken": None, "pendingInterrupts": []})
        if path.endswith("/chat/stream"):
            self.stream_bodies.append(json.loads(request.content))
            return sse_response(agent_turn("ok"))
        return httpx.Response(404, json={"detail": f"no route {path}"})


def app_for(backend: Backend):  # type: ignore[no-untyped-def]
    app = build_agent_app(backend, config=make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION))
    app.chat._catalog = catalog_for(backend)
    return app


async def settle(pilot, app) -> None:  # type: ignore[no-untyped-def]
    for _ in range(10):
        await pilot.pause()
        if app.chat._skills:
            return


# ------------------------------------------------------------------ skills


class TestSkillsOnTheWire:
    async def test_enabled_skills_are_sent_and_disabled_ones_are_not(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot, app)
            await send(pilot, app, "hello")
        body = backend.stream_bodies[0]
        assert sorted(body["enabled_skills"]) == ["sk-new", "sk-xlsx"]
        assert "invoked_skills" not in body

    async def test_no_enabled_skills_sends_no_field(self) -> None:
        backend = Backend()
        for skill in backend.skills:
            skill["isEnabled"] = False
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            await send(pilot, app, "hello")
        assert "enabled_skills" not in backend.stream_bodies[0]

    async def test_skills_off_on_the_deployment_is_silent(self) -> None:
        backend = Backend(skills_status=404)
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            await send(pilot, app, "hello")
        assert "enabled_skills" not in backend.stream_bodies[0]


class TestInvokingASkill:
    async def test_a_leading_slug_is_a_prompt_that_invokes_the_skill(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot, app)
            await send(pilot, app, "/xlsx total the NET column")
        body = backend.stream_bodies[0]
        assert body["message"] == "/xlsx total the NET column"
        assert body["invoked_skills"] == ["sk-xlsx"]

    async def test_a_slug_mid_message_invokes_it_and_is_cleared_after(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot, app)
            await send(pilot, app, "please use /xlsx for this")
            await send(pilot, app, "and again without it")
        assert backend.stream_bodies[0]["invoked_skills"] == ["sk-xlsx"]
        assert "invoked_skills" not in backend.stream_bodies[1]

    async def test_paths_urls_and_disabled_skills_are_prose(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot, app)
            await send(pilot, app, "read src/xlsx/main.py and https://x/xlsx and /pdf too")
        assert "invoked_skills" not in backend.stream_bodies[0]

    async def test_a_command_name_still_runs_the_command(self) -> None:
        """`new` is also a skill slug here; commands win at the start of a message."""
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot, app)
            await send(pilot, app, "hello")
            first = app.store.session_id
            await send(pilot, app, "/new")
            await pilot.pause()
            assert app.store.session_id != first
        assert len(backend.stream_bodies) == 1

    async def test_the_menu_offers_enabled_skills_after_commands(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot, app)
            await type_text(pilot, app, "/xl")
            menu = app.chat.command_menu
            assert [c.name for c in menu.commands] == ["xlsx"]
            assert menu.commands[0].skill
            # The colliding slug is not offered as a skill; /pdf is off.
            app.chat.composer.clear_prompt()
            await type_text(pilot, app, "/")
            names = [c.name for c in menu.commands]
            assert names.count("new") == 1 and "pdf" not in names and names[-1] == "xlsx"

    async def test_enter_on_a_skill_completes_instead_of_running(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot, app)
            await type_text(pilot, app, "/xl")
            await pilot.press("enter")
            await pilot.pause()
            assert app.chat.composer.text == "/xlsx "
            assert backend.stream_bodies == []


class TestSkillPicker:
    async def test_save_returns_an_explicit_decision_for_every_skill(self) -> None:
        skills = [Skill("a", "Alpha", slug="alpha", enabled=True), Skill("b", "Beta", slug="beta", enabled=False)]
        app = Host()
        results: list[object] = []
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(SkillPicker(skills), callback=results.append)
            await pilot.pause()
            assert "/alpha" in rendered_text(app) and "/beta" in rendered_text(app)
            await pilot.press("ctrl+d")
            await pilot.press("ctrl+s")
            await pilot.pause()
        assert results == [{"a": False, "b": False}]

    async def test_a_project_or_agent_is_named_as_bringing_its_own(self) -> None:
        app = Host()
        async with app.run_test(size=(100, 24)) as pilot:
            app.push_screen(SkillPicker([Skill("a", "Alpha", slug="alpha")], locked_by="Grader"))
            await pilot.pause()
            assert "Grader" in rendered_text(app) and "brings its own skills" in rendered_text(app)

    async def test_saving_applies_and_persists(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot, app)
            await send(pilot, app, "/skills")
            for _ in range(10):
                await pilot.pause()
                if isinstance(app.screen, SkillPicker):
                    break
            await pilot.press("ctrl+d")
            await pilot.press("ctrl+s")
            for _ in range(10):
                await pilot.pause()
                if backend.preference_puts:
                    break
            await send(pilot, app, "hello")
        assert backend.preference_puts == [{"preferences": {"sk-xlsx": False, "sk-pdf": False, "sk-new": False}}]
        assert "enabled_skills" not in backend.stream_bodies[0]


# ------------------------------------------------------------------- modes


class TestModes:
    async def test_mode_by_name_is_sent_every_turn_and_shown(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/mode socratic")
            await pilot.pause()
            assert "mode: Socratic" in app.chat.status.line
            await send(pilot, app, "one")
            await send(pilot, app, "two")
        assert [b.get("selected_prompt_id") for b in backend.stream_bodies] == ["m-soc", "m-soc"]

    async def test_mode_survives_a_model_change_and_new(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/mode brief")
            await pilot.pause()
            await app.chat.set_model("other-model")
            await send(pilot, app, "/new")
            await send(pilot, app, "hi")
        assert backend.stream_bodies[-1]["selected_prompt_id"] == "m-brief"

    async def test_clearing_mid_conversation_writes_null_to_the_session(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/mode socratic")
            await send(pilot, app, "one")
            await send(pilot, app, "/mode off")
            await pilot.pause()
            await send(pilot, app, "two")
            assert "mode:" not in app.chat.status.line
        assert backend.metadata_puts == [{"selectedPromptId": None}]
        assert "selected_prompt_id" not in backend.stream_bodies[-1]

    async def test_clearing_before_any_turn_writes_nothing(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/mode socratic")
            await send(pilot, app, "/mode off")
            await pilot.pause()
        assert backend.metadata_puts == []

    async def test_a_mode_is_not_sent_to_an_agent(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/mode socratic")
            app.store.bind_agent(AgentBinding("a1", "Grader"))
            await send(pilot, app, "hi")
        assert backend.stream_bodies[0]["rag_assistant_id"] == "a1"
        assert "selected_prompt_id" not in backend.stream_bodies[0]

    async def test_opening_a_conversation_adopts_its_stored_mode(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            wire = session_wire("s5", "Old")
            wire["preferences"] = {"selectedPromptId": "m-brief"}
            await app.chat._adopt_conversation(ConversationSummary.from_wire(wire), [], None)
            await pilot.pause()
            assert "mode: Brief" in app.chat.status.line
            await send(pilot, app, "go on")
            await app.chat._adopt_conversation(ConversationSummary.from_wire(session_wire("s6", "Plain")), [], None)
            await pilot.pause()
            assert "mode:" not in app.chat.status.line
        assert backend.stream_bodies[0]["selected_prompt_id"] == "m-brief"

    async def test_modes_off_is_reported(self) -> None:
        app = app_for(Backend(modes_status=404))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/mode")
            await pilot.pause()
            assert app.chat._mode is None

    async def test_the_picker_returns_false_for_no_mode(self) -> None:
        from agentcore_tui.client.catalog import SystemPromptOption

        app = Host()
        results: list[object] = []
        async with app.run_test(size=(80, 20)) as pilot:
            app.push_screen(ModePicker([SystemPromptOption("m1", "Socratic")], current="m1"), callback=results.append)
            await pilot.pause()
            assert "Socratic" in rendered_text(app)
            await pilot.press("home")
            await pilot.press("enter")
            await pilot.pause()
        assert results == [False]
