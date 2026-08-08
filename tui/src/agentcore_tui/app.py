"""The Textual application.

Deliberately thin: bindings, the screen stack, the command palette, and startup.
Everything else belongs to a screen or to the domain layer —

* conversation contents → :mod:`agentcore_tui.conversation`
* one in-flight turn → :mod:`agentcore_tui.turn`
* the chat layout and its actions → :mod:`agentcore_tui.screens.chat`

Keeping this file small is the point. It was 418 lines holding all of the above,
which is the shape that makes a second screen impossible to add.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import ClassVar

from textual.app import App, SystemCommand
from textual.binding import Binding, BindingType
from textual.screen import Screen

from . import __version__
from .client import AgentStreamClient, ApiConverseClient
from .config import Config
from .conversation import ConversationStore
from .logging_setup import active_log_path
from .screens import ChatScreen, Splash
from .state import record_banner_shown, should_show_banner

logger = logging.getLogger(__name__)

ClientFactory = Callable[[Config], ApiConverseClient]
AgentClientFactory = Callable[[Config], AgentStreamClient]


class ChatApp(App[None]):
    """Terminal client for the AgentCore platform."""

    CSS_PATH = "app.tcss"
    TITLE = "AgentCore"

    #: Key that opens the command palette. Textual only auto-registers its own
    #: default binding when nothing else targets `command_palette`, so setting
    #: this and the F1 binding below is what keeps the palette reachable.
    COMMAND_PALETTE_BINDING = "f1"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit"),
        # show=False because the Footer already renders a palette hint.
        Binding("f1", "command_palette", "Palette", show=False, priority=True),
    ]

    def __init__(
        self,
        config: Config,
        *,
        client_factory: ClientFactory | None = None,
        agent_client_factory: AgentClientFactory | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._client_factory = client_factory
        self._agent_client_factory = agent_client_factory
        self._store = ConversationStore()
        self._chat: ChatScreen | None = None

    # -- screens -------------------------------------------------------------

    def get_default_screen(self) -> ChatScreen:
        """Chat is the default screen, not the App's own compose().

        A screen rather than App-level widgets so sessions, history and
        assistants can be pushed alongside it later.

        The instance is cached because Textual does not register the default
        screen under a lookup name, and callers need a handle on it.
        """
        if self._chat is None:
            supplier = None
            if self._client_factory is not None:
                factory = self._client_factory
                supplier = lambda: factory(self.chat.config)  # noqa: E731 - one-line adapter
            agent_supplier = None
            if self._agent_client_factory is not None:
                agent_factory = self._agent_client_factory
                agent_supplier = lambda: agent_factory(self.chat.config)  # noqa: E731 - one-line adapter
            self._chat = ChatScreen(
                self._config,
                store=self._store,
                client_supplier=supplier,
                agent_client_supplier=agent_supplier,
            )
        return self._chat

    @property
    def chat(self) -> ChatScreen:
        """The chat screen. Built on first access if the app has not started."""
        return self.get_default_screen()

    @property
    def store(self) -> ConversationStore:
        """The conversation. Owned by the App so several screens can share it."""
        return self._store

    async def on_mount(self) -> None:
        self.sub_title = self._config.base_url or "not configured"
        self._maybe_show_banner()

    # -- startup banner ------------------------------------------------------

    def _maybe_show_banner(self) -> None:
        """Push the splash if it is due. Deliberately not awaited.

        Called after the chat screen is built so the banner covers a finished
        UI, and dismissing it reveals a composer that is already focused.
        Awaiting the animation would delay first paint for decoration.
        """
        if not self._config.banner:
            return
        if not (self._config.force_banner or should_show_banner(__version__)):
            return

        # Recorded when the banner starts rather than when it ends, so a user
        # who quits mid-animation is not shown it again next launch.
        def on_finished() -> None:
            if not record_banner_shown(__version__):
                logger.debug("banner shown but not recorded; it will replay next launch")

        self.push_screen(Splash(on_finished=on_finished), callback=self._after_banner)

    def _after_banner(self, _result: None) -> None:
        """Return focus to the composer once the banner is gone.

        Focus was on the splash screen; without this the first keystroke after
        the banner would go nowhere.
        """
        if self._config.is_complete:
            self.chat.composer.focus()

    # -- command palette -----------------------------------------------------

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Curate the palette: the active screen's commands, then useful builtins.

        Textual's defaults include "Maximize", which maximizes the *focused*
        widget — always the composer here, so it fills the screen with an input
        box and hides the transcript. Its help panel is also unreadable at chat
        widths, and the footer already lists the bindings. Both are dropped.
        """
        if isinstance(screen, ChatScreen):
            yield from screen.system_commands()

        yield SystemCommand("Show log file", "Report where diagnostics are being written", self._show_log_path, discover=False)
        yield SystemCommand("Theme", "Change the colour theme", self.action_change_theme)
        yield SystemCommand("Screenshot", "Save an SVG snapshot of the screen", lambda: self.set_timer(0.1, self.deliver_screenshot), discover=False)
        yield SystemCommand("Quit", "Leave the application", self.action_quit)

    def _show_log_path(self) -> None:
        path = active_log_path()
        self.notify(str(path) if path else "Logging is not configured.", title="Log file", timeout=15)
