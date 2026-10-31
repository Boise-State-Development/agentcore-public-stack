"""A project's shared conversations: the list, and a read-only view of one.

Members share a conversation *with the project* in the web app; the share is a
snapshot frozen at that moment. Here the list is ``/project shared``, Enter
reads a snapshot in the same transcript widgets a live turn uses, and Enter
again in the viewer forks it into a conversation of your own (the web app's
"Continue"), which keeps the project binding.

"Task" is the wire name for these; everything on screen says "conversation",
as the web app does (shared-projects spec, 2026-10-12).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import ClassVar

from rich.markup import escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..client.catalog import CatalogClient, SharedConversation
from ..errors import AgentCoreTuiError
from ..widgets.history import render_history

logger = logging.getLogger(__name__)

CatalogSupplier = Callable[[], CatalogClient]
#: ``(session_id, title)`` of the new conversation a "Continue" created.
ContinueHandler = Callable[[str, str], Awaitable[None]]


class SharedConversationList(ModalScreen[SharedConversation | None]):
    """Pick one of a project's shared conversations."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss_picker", "Cancel")]

    def __init__(self, project_name: str, shared: list[SharedConversation]) -> None:
        super().__init__()
        self._project_name = project_name
        self._shared = shared

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-frame") as frame:
            frame.border_title = f"Shared in {self._project_name}"
            if self._shared:
                yield Label("Conversations members shared with this project. Enter reads one.", id="picker-title")
                yield OptionList(*(self._option(item) for item in self._shared), id="picker-list")
            else:
                yield Label("Nobody has shared a conversation with this project yet.", id="picker-title")
                yield OptionList(id="picker-list")
            yield Label("↑↓ move · Enter read · Esc cancel", id="picker-help")

    @staticmethod
    def _option(item: SharedConversation) -> Option:
        who = "you" if item.is_mine else item.shared_by
        when = item.shared_at[:10]
        label = f"{escape(item.title)}  [dim]{escape(who)} · {when}[/]"
        if item.note:
            label += f"\n  [dim italic]{escape(item.note)}[/]"
        return Option(label, id=item.share_id)

    def on_mount(self) -> None:
        self.query_one("#picker-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        for item in self._shared:
            if item.share_id == event.option.id:
                self.dismiss(item)
                return

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)


class SharedConversationView(Screen[None]):
    """Read a shared snapshot. Enter continues it as your own; Esc goes back."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Back"),
        Binding("enter", "continue_shared", "Continue as mine", priority=True),
    ]

    def __init__(
        self,
        shared: SharedConversation,
        catalog_supplier: CatalogSupplier,
        *,
        on_continue: ContinueHandler,
        model_label: str,
    ) -> None:
        super().__init__()
        self._shared = shared
        self._catalog_supplier = catalog_supplier
        self._on_continue = on_continue
        self._model_label = model_label
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="shared-banner")
        yield VerticalScroll(id="transcript")
        yield Footer()

    @property
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    @property
    def banner(self) -> Static:
        return self.query_one("#shared-banner", Static)

    async def on_mount(self) -> None:
        self.sub_title = self._shared.title
        who = "you" if self._shared.is_mine else self._shared.shared_by
        self.banner.update(f"Shared by {escape(who)} · read-only · Enter continues it as your own conversation")
        try:
            messages = await self._catalog_supplier().shared_snapshot(self._shared.share_id)
        except AgentCoreTuiError as exc:
            self.banner.update(f"Could not open it: {escape(exc.message)}" + (f" — {escape(exc.hint)}" if exc.hint else ""))
            self.banner.add_class("-error")
            return
        await render_history(self.transcript, messages, model_label=self._model_label)
        self.transcript.scroll_home(animate=False)
        self.transcript.focus()

    def action_close(self) -> None:
        self.dismiss(None)

    @work(exclusive=True)
    async def action_continue_shared(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.banner.update("Making a copy you can continue...")
        try:
            session_id, title = await self._catalog_supplier().continue_shared(self._shared.share_id)
        except AgentCoreTuiError as exc:
            self._busy = False
            self.banner.update(f"Could not continue it: {escape(exc.message)}")
            self.banner.add_class("-error")
            return
        self.dismiss(None)
        await self._on_continue(session_id, title)
