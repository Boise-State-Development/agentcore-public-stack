"""App-level tests: wiring, the screen stack, and the command palette.

Chat behaviour lives in ``test_chat_screen.py`` and the turn lifecycle in
``test_turn.py``. This file should stay short — if it grows, something has moved
back into the App that belongs on a screen.
"""

from __future__ import annotations

from agentcore_tui.conversation import ConversationStore
from agentcore_tui.screens import ChatScreen

from .conftest import BASE_URL, build_app, command_titles, ok_handler, send


class TestWiring:
    async def test_the_default_screen_is_the_chat_screen(self) -> None:
        """A Screen, not App-level widgets: this is what lets a second feature
        area exist alongside chat."""
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ChatScreen)

    async def test_subtitle_shows_the_target_deployment(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.sub_title == BASE_URL

    async def test_the_app_owns_the_store_and_shares_it_with_the_screen(self) -> None:
        """The store is App-owned so a conversation list and the chat screen can
        read the same conversation instead of holding two copies.

        Regression: the store defines ``__len__``, so an *empty* store is falsy.
        A ``store or ConversationStore()`` default silently handed the screen a
        private copy on every launch, and only stopped doing so once a message
        had been added — which never happens before mount.
        """
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.store, ConversationStore)
            assert app.chat.store is app.store

    async def test_an_empty_store_is_not_replaced(self) -> None:
        """The narrow version of the above, without needing an app at all."""
        from agentcore_tui.screens import ChatScreen

        from .conftest import make_config

        store = ConversationStore()
        assert store.is_empty
        assert ChatScreen(make_config(), store=store).store is store

    async def test_the_store_survives_a_screen_being_pushed_over_chat(self) -> None:
        from agentcore_tui.screens import ModelPicker

        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await send(pilot, app, "remember this")
            app.push_screen(ModelPicker(("m",), "m"))
            await pilot.pause()
            assert [message.content for message in app.store][0] == "remember this"


class TestCommandPalette:
    async def test_offers_screen_and_app_commands_together(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            titles = command_titles(app)
            # Contributed by the chat screen.
            assert "New conversation" in titles
            assert "Change model" in titles
            # Contributed by the app.
            assert "Theme" in titles
            assert "Quit" in titles
            assert "Show log file" in titles

    async def test_drops_builtins_that_make_no_sense_here(self) -> None:
        """Maximize would fill the screen with the composer and hide the answer."""
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            titles = command_titles(app)
            assert "Maximize" not in titles
            assert "Minimize" not in titles
            assert "Keys" not in titles

    async def test_a_non_chat_screen_contributes_no_chat_commands(self) -> None:
        """Guards the isinstance check in get_system_commands."""
        from agentcore_tui.screens import ModelPicker

        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            picker = ModelPicker(("m",), "m")
            app.push_screen(picker)
            await pilot.pause()
            titles = [command.title for command in app.get_system_commands(picker)]
            assert "New conversation" not in titles
            assert "Quit" in titles

    async def test_f1_opens_the_command_palette(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "CommandPalette" not in [type(screen).__name__ for screen in app.screen_stack]

            await pilot.press("f1")
            await pilot.pause()

            assert "CommandPalette" in [type(screen).__name__ for screen in app.screen_stack]
