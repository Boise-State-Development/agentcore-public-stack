"""The slash-command registry and the ``/`` completion popup.

The registry is pure Python and tested without an app. The popup tests drive
the composer through the pilot and assert on the menu's state, since the menu
never takes focus and is only ever moved by keys the composer forwards.
"""

from __future__ import annotations

import inspect
import re

import pytest

from aps_tui.commands import COMMANDS, CommandRegistry, SlashCommand
from aps_tui.screens.chat import ChatScreen
from aps_tui.widgets.messages import Notice

from .conftest import build_app, ok_handler, rendered_text, send
from .conftest import make_config, sse_response
from .test_chat_screen import agent_turn, build_agent_app
from aps_tui.credentials import CredentialSource


def agent_app(*, local_tools: bool = True):  # type: ignore[no-untyped-def]
    config = make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION, local_tools=local_tools)
    return build_agent_app(lambda _r: sse_response(agent_turn()), config=config)


# ---------------------------------------------------------------- registry


class TestRegistry:
    def test_every_registered_command_has_a_dispatcher_case(self) -> None:
        """The popup, /help and the dispatcher must agree. The dispatcher is a
        match statement on ``command.name``; read its source and check every
        registered name appears as a case."""
        source = inspect.getsource(ChatScreen._run_slash_command)
        cases = set(re.findall(r'case "([a-z-]+)"', source))
        missing = [command.name for command in COMMANDS if command.name not in cases]
        assert missing == []

    def test_no_dispatcher_case_without_a_registration(self) -> None:
        source = inspect.getsource(ChatScreen._run_slash_command)
        cases = set(re.findall(r'case "([a-z-]+)"', source))
        unregistered = sorted(case for case in cases if COMMANDS.resolve(case) is None)
        assert unregistered == []

    def test_aliases_resolve_to_the_canonical_command(self) -> None:
        assert COMMANDS.resolve("exit") is COMMANDS.resolve("quit")
        assert COMMANDS.resolve("chats") is COMMANDS.resolve("conversations")
        assert COMMANDS.resolve("?") is COMMANDS.resolve("help")
        assert COMMANDS.resolve("QUIT") is COMMANDS.resolve("quit")

    def test_unknown_names_resolve_to_none(self) -> None:
        assert COMMANDS.resolve("frobnicate") is None

    def test_duplicate_spellings_are_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="twice"):
            CommandRegistry([SlashCommand("a", "x"), SlashCommand("b", "y", aliases=("a",))])

    def test_match_is_a_prefix_on_name_alias_or_keyword(self) -> None:
        names = [c.name for c in COMMANDS.match("c", agent=True)]
        assert names[:1] == ["conversations"] or "conversations" in names
        assert "continue" in names and "copy" in names and "clear" in names
        # aliases do not complete; they only dispatch
        assert COMMANDS.match("hist", agent=True) == []
        # keyword
        assert [c.name for c in COMMANDS.match("sess", agent=True)] == ["conversations"]
        # empty prefix lists everything visible
        assert len(COMMANDS.match("", agent=True)) == len(COMMANDS)

    def test_agent_only_commands_are_hidden_from_api_key_sessions(self) -> None:
        api_key = {c.name for c in COMMANDS.visible(agent=False)}
        agent = {c.name for c in COMMANDS.visible(agent=True)}
        assert {"tools", "conversations", "attach", "continue"} <= agent - api_key
        assert {"model", "new", "help", "quit"} <= api_key

    def test_help_lines_are_aligned_and_cover_every_visible_command(self) -> None:
        lines = COMMANDS.help_lines(agent=True)
        assert len(lines) == len(COMMANDS)
        starts = {line.index(c.summary) for line, c in zip(lines, COMMANDS, strict=True)}
        assert len(starts) == 1, "summaries should start in one column"

    def test_display_shows_usage_when_present(self) -> None:
        attach = COMMANDS.resolve("attach")
        assert attach is not None and attach.display == "/attach <path>"
        quit_ = COMMANDS.resolve("quit")
        assert quit_ is not None and quit_.display == "/quit"


# -------------------------------------------------------------------- popup


async def type_text(pilot, app, text: str) -> None:  # type: ignore[no-untyped-def]
    """Type into the composer key by key so ``Changed`` fires as it would live."""
    for char in text:
        await pilot.press("slash" if char == "/" else char)
    await pilot.pause()


class TestCommandMenu:
    async def test_slash_opens_the_menu_with_every_command(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            menu = app.chat.command_menu
            assert not menu.is_open

            await type_text(pilot, app, "/")
            assert menu.is_open
            assert [c.name for c in menu.commands] == [c.name for c in COMMANDS.visible(agent=False)]
            assert menu.current is not None and menu.current.name == "new"

    async def test_typing_filters_and_keeps_an_exact_match_highlighted(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            menu = app.chat.command_menu
            await type_text(pilot, app, "/c")
            names = [c.name for c in menu.commands]
            assert set(names) == {"copy", "clear"}  # not `new` via its alias, not `stop` via `cancel`
            await type_text(pilot, app, "lear")
            assert [c.name for c in menu.commands] == ["clear"]
            assert menu.current is not None and menu.current.name == "clear"

    async def test_a_space_closes_the_menu_because_arguments_have_started(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            menu = app.chat.command_menu
            await type_text(pilot, app, "/model")
            assert menu.is_open
            await pilot.press("space")
            await pilot.pause()
            assert not menu.is_open

    async def test_no_match_closes_the_menu(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await type_text(pilot, app, "/zzz")
            assert not app.chat.command_menu.is_open

    async def test_a_double_slash_is_a_literal_prompt_not_a_menu(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await type_text(pilot, app, "//")
            assert not app.chat.command_menu.is_open

    async def test_up_and_down_move_the_highlight_and_wrap(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            menu = app.chat.command_menu
            await type_text(pilot, app, "/")
            first = menu.current
            await pilot.press("down")
            await pilot.pause()
            assert menu.current is not first
            await pilot.press("up")
            await pilot.pause()
            assert menu.current is first
            await pilot.press("up")
            await pilot.pause()
            assert menu.current is menu.commands[-1]
            # The composer's text did not move: the keys went to the menu.
            assert app.chat.composer.text == "/"

    async def test_tab_completes_the_highlighted_command_and_leaves_room_for_arguments(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await type_text(pilot, app, "/mod")
            await pilot.press("tab")
            await pilot.pause()
            assert app.chat.composer.text == "/model "
            assert not app.chat.command_menu.is_open
            assert app.chat.composer.has_focus

    async def test_tab_on_a_command_without_arguments_adds_no_space(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await type_text(pilot, app, "/hel")
            await pilot.press("tab")
            await pilot.pause()
            assert app.chat.composer.text == "/help"

    async def test_escape_closes_the_menu_and_keeps_the_text(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await type_text(pilot, app, "/mo")
            await pilot.press("escape")
            await pilot.pause()
            assert not app.chat.command_menu.is_open
            assert app.chat.composer.text == "/mo"

    async def test_enter_runs_the_highlighted_command(self) -> None:
        app = build_app(ok_handler(["never"]))
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await type_text(pilot, app, "/hel")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert app.chat.query(Notice)
            assert "/quit" in rendered_text(app)
            assert app.chat.store.is_empty
            assert app.chat.composer.text == ""

    async def test_enter_on_a_partial_name_runs_the_highlighted_not_the_typed(self) -> None:
        """`/q` highlights quit, but `/c` highlights the first `c` command; Enter
        runs what is highlighted so what you see is what you get."""
        app = build_app(ok_handler(["never"]))
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await type_text(pilot, app, "/sta")
            assert app.chat.command_menu.current is not None
            assert app.chat.command_menu.current.name == "status"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert "Server" in rendered_text(app)

    async def test_agent_sessions_see_agent_only_commands(self) -> None:
        app = agent_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await type_text(pilot, app, "/")
            names = [c.name for c in app.chat.command_menu.commands]
            assert "tools" in names and "conversations" in names and "attach" in names

    async def test_the_menu_never_takes_focus(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await type_text(pilot, app, "/")
            assert app.chat.composer.has_focus
            assert not app.chat.command_menu.can_focus


# ------------------------------------------------------------ new commands


class TestHousekeepingCommands:
    async def test_theme_lists_then_switches(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            before = app.theme
            target = next(name for name in sorted(app.available_themes) if name != before)
            await send(pilot, app, f"/theme {target}")
            assert app.theme == target

    async def test_theme_with_an_unknown_name_does_not_change_the_theme(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            before = app.theme
            await send(pilot, app, "/theme no-such-theme")
            assert app.theme == before

    async def test_status_reports_server_model_and_log(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/status")
            frame = rendered_text(app)
            assert "Server" in frame and "Model" in frame and "Log" in frame
            assert app.chat.store.is_empty

    async def test_unknown_command_points_at_the_slash_menu(self) -> None:
        app = build_app(ok_handler(["never"]))
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/frobnicate")
            frame = rendered_text(app)
            assert "Unknown command /frobnicate" in frame
            assert app.chat.store.is_empty

    async def test_tools_local_off_removes_client_tools_from_the_next_turn(self) -> None:
        app = agent_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert app.chat.config.local_tools is True
            await send(pilot, app, "/tools local off")
            assert app.chat.config.local_tools is False
            assert app.chat.turn._client_tools == []  # type: ignore[attr-defined]
            await send(pilot, app, "/tools local on")
            assert app.chat.config.local_tools is True
            assert app.chat.turn._client_tools  # type: ignore[attr-defined]

    async def test_tools_local_needs_a_signed_in_session(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/tools local off")
            # Nothing crashed and the API-key config did not grow a flag it cannot use.
            assert app.chat.config.local_tools is False
