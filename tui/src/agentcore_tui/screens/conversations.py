"""The conversation list — the web UI's session sidebar, as a screen.

A screen rather than a permanent pane because a terminal has less room than a
browser and the transcript deserves the width. It shares the App's
:class:`~agentcore_tui.conversation.ConversationStore`, so opening a conversation
replaces what the chat screen is showing rather than handing it a private copy.

Grouping matches the web app — Today / Yesterday / Last 7 days / Last 30 days /
Older, by ``lastMessageAt`` — because that is the only ordering that makes a long
list navigable, and users already know it.

Deleting is the one destructive action here, so it asks first. Renaming and
read/unread do not, being trivially reversible.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static

from ..client.catalog import CatalogClient, ConversationSummary, HistoryMessage
from ..errors import AgentCoreTuiError

logger = logging.getLogger(__name__)

#: Supplied rather than constructed so the screen never reads a keyring, and
#: tests can hand it a client backed by MockTransport.
CatalogSupplier = Callable[[], CatalogClient]

#: Called with the chosen conversation and its restored messages. The caller
#: owns the store, so it decides what "open" means to the rest of the app.
OpenHandler = Callable[[ConversationSummary, list[HistoryMessage]], Awaitable[None]]


def group_for(timestamp: str, *, now: datetime | None = None) -> str:
    """Which heading a conversation belongs under.

    Unparseable or missing timestamps land in "Older" rather than raising: a row
    the server sent must always be reachable, and a bad date is not the user's
    problem.
    """
    reference = now or datetime.now(timezone.utc)
    moment = _parse(timestamp)
    if moment is None:
        return "Older"

    delta = reference.date() - moment.date()
    if delta <= timedelta(days=0):
        return "Today"
    if delta <= timedelta(days=1):
        return "Yesterday"
    if delta <= timedelta(days=7):
        return "Last 7 days"
    if delta <= timedelta(days=30):
        return "Last 30 days"
    return "Older"


def _parse(timestamp: str) -> datetime | None:
    if not timestamp:
        return None
    text = timestamp.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Naive timestamps are treated as UTC, which is what the server sends.
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


#: The order headings appear in, regardless of which ones have rows.
GROUP_ORDER = ("Today", "Yesterday", "Last 7 days", "Last 30 days", "Older")


class ConfirmDelete(ModalScreen[bool]):
    """Confirms one deletion. Destructive and not undoable from here."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-frame"):
            yield Label("Delete this conversation?", id="picker-title")
            yield Static(self._title, id="confirm-subject")
            yield Label("This also removes it from the web app.", id="picker-help")
            with Horizontal(id="confirm-actions"):
                yield Button("Delete", id="confirm-yes", variant="error")
                yield Button("Cancel", id="confirm-no", variant="primary")

    def on_mount(self) -> None:
        # Cancel takes focus, so Enter on a reflex does the safe thing.
        self.query_one("#confirm-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class RenameConversation(ModalScreen[str | None]):
    """Renames one conversation. Dismisses with the new title, or None."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: str) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="rename-frame"):
            yield Label("Rename conversation", id="picker-title")
            yield Input(value=self._current, id="rename-input")
            yield Label("Enter to save · Esc to cancel", id="picker-help")

    def on_mount(self) -> None:
        self.query_one("#rename-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        title = event.value.strip()
        # An empty title would replace a useful name with nothing, and the
        # server would accept it.
        self.dismiss(title or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConversationRow(ListItem):
    """One conversation. Holds its summary so actions need no lookup."""

    def __init__(self, summary: ConversationSummary) -> None:
        super().__init__(classes="conversation-row")
        self.summary = summary

    def compose(self) -> ComposeResult:
        yield Static(self._row_text(), classes="conversation-title")

    def _row_text(self) -> str:
        marker = "● " if self.summary.unread else "  "
        counts = f"{self.summary.message_count} msg" if self.summary.message_count else "empty"
        context = f" · {self.summary.context_percent}% ctx" if self.summary.context_percent is not None else ""
        return f"{marker}{self.summary.title}\n   {counts}{context}"

    def refresh_title(self) -> None:
        self.query_one(Static).update(self._row_text())


class GroupHeading(ListItem):
    """A non-selectable date heading."""

    def __init__(self, label: str) -> None:
        super().__init__(classes="conversation-group")
        self.disabled = True
        self._label = label

    def compose(self) -> ComposeResult:
        yield Static(self._label, classes="conversation-group-label")


class ConversationList(Screen[None]):
    """Browse, open, rename and delete the user's conversations."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Back"),
        Binding("enter", "open", "Open", priority=True),
        Binding("f2", "rename", "Rename"),
        Binding("delete", "delete", "Delete"),
        Binding("u", "toggle_unread", "Unread"),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("m", "load_more", "More"),
    ]

    def __init__(self, catalog_supplier: CatalogSupplier, *, on_open: OpenHandler) -> None:
        super().__init__()
        self._catalog_supplier = catalog_supplier
        self._on_open = on_open
        self._rows: list[ConversationSummary] = []
        self._next_token: str | None = None
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(id="conversation-list")
        yield Label("Loading conversations...", id="conversation-status")
        yield Footer()

    @property
    def list_view(self) -> ListView:
        return self.query_one("#conversation-list", ListView)

    @property
    def status(self) -> Label:
        return self.query_one("#conversation-status", Label)

    def selected(self) -> ConversationSummary | None:
        """The highlighted conversation, or None on a heading or empty list."""
        item = self.list_view.highlighted_child
        return item.summary if isinstance(item, ConversationRow) else None

    # -- loading -------------------------------------------------------------

    async def on_mount(self) -> None:
        self.sub_title = "Conversations"
        await self._load(reset=True)

    async def _load(self, *, reset: bool) -> None:
        """Fetch a page and rebuild the list.

        Rebuilds rather than appends because the groupings have to be recomputed:
        a conversation touched since the last fetch moves between headings.
        """
        if self._busy:
            return
        self._busy = True
        try:
            if reset:
                self._rows = []
                self._next_token = None
            page = await self._catalog_supplier().conversations(next_token=self._next_token)
        except AgentCoreTuiError as exc:
            self.status.update(f"{exc.message} — {exc.hint}" if exc.hint else exc.message)
            self.status.add_class("-error")
            return
        finally:
            self._busy = False

        self._rows.extend(item for item in page.items if isinstance(item, ConversationSummary))
        self._next_token = page.next_token
        await self._rebuild()

    async def _rebuild(self) -> None:
        await self.list_view.clear()
        if not self._rows:
            self.status.update("No conversations yet. Press Esc and ask something.")
            return

        by_group: dict[str, list[ConversationSummary]] = {}
        for row in self._rows:
            by_group.setdefault(group_for(row.last_message_at), []).append(row)

        for heading in GROUP_ORDER:
            group = by_group.get(heading)
            if not group:
                continue
            await self.list_view.append(GroupHeading(heading))
            for summary in group:
                await self.list_view.append(ConversationRow(summary))

        more = " · press m for more" if self._next_token else ""
        self.status.remove_class("-error")
        self.status.update(f"{len(self._rows)} conversation(s){more}")

    # -- actions -------------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)

    async def action_reload(self) -> None:
        await self._load(reset=True)

    async def action_load_more(self) -> None:
        if self._next_token:
            await self._load(reset=False)

    async def action_open(self) -> None:
        """Restore the conversation and hand it to the caller."""
        summary = self.selected()
        if summary is None:
            return
        self.status.update(f"Opening {summary.title}...")
        try:
            page = await self._catalog_supplier().history(summary.session_id)
        except AgentCoreTuiError as exc:
            self.status.update(f"Could not open it: {exc.message}")
            self.status.add_class("-error")
            return

        messages = [item for item in page.items if isinstance(item, HistoryMessage)]
        # Best-effort, like the web app: a failed read receipt must not stop a
        # conversation from opening.
        await self._catalog_supplier().mark_read(summary.session_id)
        await self._on_open(summary, messages)
        self.dismiss(None)

    @work
    async def action_rename(self) -> None:
        summary = self.selected()
        if summary is None:
            return
        title = await self.app.push_screen_wait(RenameConversation(summary.title))
        if not title or title == summary.title:
            return
        try:
            await self._catalog_supplier().rename(summary.session_id, title)
        except AgentCoreTuiError as exc:
            self.notify(exc.message, title="Rename failed", severity="error", timeout=8)
            return
        self._replace(summary, replace(summary, title=title))

    @work
    async def action_delete(self) -> None:
        summary = self.selected()
        if summary is None:
            return
        if not await self.app.push_screen_wait(ConfirmDelete(summary.title)):
            return
        try:
            await self._catalog_supplier().delete(summary.session_id)
        except AgentCoreTuiError as exc:
            self.notify(exc.message, title="Delete failed", severity="error", timeout=8)
            return
        self._rows = [row for row in self._rows if row.session_id != summary.session_id]
        await self._rebuild()
        self.notify(f"Deleted {summary.title}", timeout=5)

    async def action_toggle_unread(self) -> None:
        summary = self.selected()
        if summary is None:
            return
        want_unread = not summary.unread
        # mark_read never raises and reports whether it stuck, so the row is only
        # updated when the server agreed.
        ok = await self._catalog_supplier().mark_read(summary.session_id, read=not want_unread)
        if ok:
            self._replace(summary, replace(summary, unread=want_unread))

    def _replace(self, old: ConversationSummary, new: ConversationSummary) -> None:
        """Swap one row in place, avoiding a full refetch for a local edit."""
        self._rows = [new if row.session_id == old.session_id else row for row in self._rows]
        for item in self.list_view.children:
            if isinstance(item, ConversationRow) and item.summary.session_id == new.session_id:
                item.summary = new
                item.refresh_title()
                break
