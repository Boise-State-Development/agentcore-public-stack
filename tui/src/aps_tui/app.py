"""The Textual application.

Deliberately thin: the quit binding, the screen stack, and startup.
Everything else belongs to a screen or to the domain layer —

* conversation contents → :mod:`aps_tui.conversation`
* one in-flight turn → :mod:`aps_tui.turn`
* the chat layout and its actions → :mod:`aps_tui.screens.chat`

Keeping this file small is the point. It was 418 lines holding all of the above,
which is the shape that makes a second screen impossible to add.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from . import __version__
from .client import AgentStreamClient, ApiConverseClient
from .config import Config
from .conversation import ConversationStore
from .screens import ChatScreen, Splash
from .state import record_banner_shown, should_show_banner

logger = logging.getLogger(__name__)

ClientFactory = Callable[[Config], ApiConverseClient]
AgentClientFactory = Callable[[Config], AgentStreamClient]


class ChatApp(App[None]):
    """Terminal client for the AgentCore platform."""

    CSS_PATH = "app.tcss"
    TITLE = "AgentCore"

    #: No command palette: every feature is a slash command typed into the
    #: composer (steering: "UX paradigm"), and a second way in is the mixing
    #: that paradigm exists to end. Textual's palette is switched off rather
    #: than left reachable on its default key.
    ENABLE_COMMAND_PALETTE = False

    #: Quit is the one App-level key. Ctrl+Q rather than Ctrl+C because Textual
    #: binds Ctrl+C to *copy* while a TextArea has focus, which is always here;
    #: Textual's own Ctrl+C handler then tells the user to press Ctrl+Q.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit"),
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
