"""App-level tests: wiring, the screen stack, and the no-keys-for-features rule.

Chat behaviour lives in ``test_chat_screen.py`` and the turn lifecycle in
``test_turn.py``. This file should stay short — if it grows, something has moved
back into the App that belongs on a screen.
"""

from __future__ import annotations

import re

from agentcore_tui.conversation import ConversationStore
from agentcore_tui.screens import ChatScreen

from .conftest import BASE_URL, build_app, ok_handler, send


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
            app.push_screen(ModelPicker([], current="m"))
            await pilot.pause()
            assert [message.content for message in app.store][0] == "remember this"


class TestNoKeysForFeatures:
    """The UX paradigm (steering): features are slash commands, keys are not a way in.

    These guard the rule structurally so a well-meaning binding does not creep
    back. Adding one is a review rejection; this makes it a red test too.
    """

    def test_the_app_has_no_command_palette(self) -> None:
        from agentcore_tui.app import ChatApp

        assert ChatApp.ENABLE_COMMAND_PALETTE is False

    def test_the_only_app_key_is_quit(self) -> None:
        from agentcore_tui.app import ChatApp

        assert [(b.key, b.action) for b in ChatApp.BINDINGS] == [("ctrl+q", "quit")]

    def test_the_chat_screen_binds_nothing_but_stop(self) -> None:
        assert [(b.key, b.action) for b in ChatScreen.BINDINGS] == [("escape", "cancel_turn")]

    def test_no_function_key_is_bound_anywhere(self) -> None:
        """F-keys are swallowed by VS Code, macOS, tmux and SSH; see steering."""
        import importlib
        import inspect
        import pkgutil

        import agentcore_tui

        offenders: list[str] = []
        for module_info in pkgutil.walk_packages(agentcore_tui.__path__, "agentcore_tui."):
            module = importlib.import_module(module_info.name)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if obj.__module__ != module.__name__:
                    continue  # imported from Textual; its own F-keys are not ours to police
                for binding in obj.__dict__.get("BINDINGS", []) or []:
                    key = getattr(binding, "key", None)
                    if key and re.fullmatch(r"f\d+", key):
                        offenders.append(f"{obj.__name__}: {key}")
        assert offenders == []

    async def test_f1_does_nothing(self) -> None:
        app = build_app(ok_handler())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("f1")
            await pilot.pause()
            assert [type(screen).__name__ for screen in app.screen_stack] == ["ChatScreen"]
