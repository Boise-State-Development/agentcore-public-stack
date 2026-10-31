"""The conversation list — the web UI's session sidebar, as a screen.

A screen rather than a permanent pane because a terminal has less room than a
browser and the transcript deserves the width. It shares the App's
:class:`~aps_tui.conversation.ConversationStore`, so opening a conversation
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

from ..client.catalog import Project, CatalogClient, ConversationSummary, HistoryMessage
from ..errors import AgentCoreTuiError

logger = logging.getLogger(__name__)

#: Supplied rather than constructed so the screen never reads a keyring, and
#: tests can hand it a client backed by MockTransport.
CatalogSupplier = Callable[[], CatalogClient]

#: Called with the chosen conversation and its restored messages. The caller
#: owns the store, so it decides what "open" means to the rest of the app.
#: ``(summary, messages, project)``: the project is resolved when the
#: conversation belongs to one the user can still see, else None.
OpenHandler = Callable[[ConversationSummary, list[HistoryMessage], "Project | None"], Awaitable[None]]


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


def _relative_day(timestamp: str, *, now: datetime | None = None) -> str:
    """``14:05`` for today, ``Tue`` within the week, ``12 Mar`` otherwise."""
    moment = _parse(timestamp)
    if moment is None:
        return ""
    local = moment.astimezone()
    reference = (now or datetime.now(timezone.utc)).astimezone()
    days = (reference.date() - local.date()).days
    if days <= 0:
        return local.strftime("%H:%M")
    if days < 7:
        return local.strftime("%a")
    return local.strftime("%-d %b")


class ConfirmDelete(ModalScreen[bool]):
    """Confirms a deletion of one or several conversations. Destructive and
    not undoable from here."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, titles: str | list[str]) -> None:
        super().__init__()
        self._titles = [titles] if isinstance(titles, str) else list(titles)

    def compose(self) -> ComposeResult:
        count = len(self._titles)
        shown = self._titles[:6]
        subject = "\n".join(shown) + (f"\n…and {count - len(shown)} more" if count > len(shown) else "")
        with Vertical(id="confirm-frame") as frame:
            frame.border_title = "Delete"
            yield Label("Delete this conversation?" if count == 1 else f"Delete these {count} conversations?", id="picker-title")
            yield Static(subject, id="confirm-subject", markup=False)
            yield Label("This also removes it from the web app. It cannot be undone.", id="picker-help")
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
        with Vertical(id="rename-frame") as frame:
            frame.border_title = "Rename"
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

    def __init__(self, summary: ConversationSummary, *, project_name: str | None = None, marked: bool = False) -> None:
        super().__init__(classes="conversation-row")
        self._project_name = project_name
        self.summary = summary
        self.marked = marked

    def compose(self) -> ComposeResult:
        yield Static(self._row_text(), classes="conversation-title")

    def _row_text(self) -> str:
        """Title on the first line; a muted meta line beneath.

        Markup is built here rather than trusting the title, which is user text:
        it is escaped so a conversation called ``[bold]`` renders literally.
        """
        from rich.markup import escape

        if self.marked:
            marker = "[$warning]✓[/] "
        else:
            marker = "[$accent]●[/] " if self.summary.unread else "  "
        counts = f"{self.summary.message_count} messages" if self.summary.message_count != 1 else "1 message"
        if not self.summary.message_count:
            counts = "empty"
        context = f" · {self.summary.context_percent}% of context" if self.summary.context_percent is not None else ""
        when = _relative_day(self.summary.last_message_at)
        project = f"⌂ {escape(self._project_name)}" if self._project_name else ""
        meta = " · ".join(part for part in (project, when, counts + context) if part)
        text = f"{marker}{escape(self.summary.title)}\n  [dim]{meta}[/]"
        if self.summary.snippet:
            text += f"\n  [dim]…{escape(self.summary.snippet.strip())}…[/]"
        return text

    def refresh_title(self) -> None:
        self.query_one(Static).update(self._row_text())


class GroupHeading(ListItem):
    """A non-selectable date heading."""

    def __init__(self, label: str) -> None:
        super().__init__(classes="conversation-group")
        self.disabled = True
        self._label = label

    def compose(self) -> ComposeResult:
        yield Static(self._label.upper(), classes="conversation-group-label", markup=False)


class ConversationList(Screen[None]):
    """Browse, open, rename and delete the user's conversations."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Back"),
        Binding("enter", "open", "Open", priority=True),
        Binding("r", "rename", "Rename"),
        Binding("space", "toggle_mark", "Mark"),
        Binding("delete", "delete", "Delete"),
        Binding("u", "toggle_unread", "Unread"),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("m", "load_more", "More"),
        Binding("slash", "focus_filter", "Filter", key_display="/"),
    ]

    def __init__(self, catalog_supplier: CatalogSupplier, *, on_open: OpenHandler, initial_query: str | None = None) -> None:
        super().__init__()
        self._catalog_supplier = catalog_supplier
        self._on_open = on_open
        self._rows: list[ConversationSummary] = []
        self._next_token: str | None = None
        self._busy = False
        #: Projects by id, for the row badge and for rebinding on open. Loaded
        #: once with the first page; empty when the feature is off or the
        #: fetch failed, in which case rows simply carry no badge.
        self._projects: dict[str, Project] = {}
        #: Current filter text; empty shows every loaded row.
        self._filter = (initial_query or "").strip()
        #: Rows from a server search, shown instead of the loaded page.
        self._search_results: list[ConversationSummary] | None = None
        #: Conversations marked with Space, for one Delete. Survives filtering
        #: and reloads; cleared when the deletion goes through.
        self._marked: set[str] = set()
        #: The status line without the marked-count suffix, so the suffix can
        #: be re-applied as marks change without re-deriving the line.
        self._status_base = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(value=self._filter, placeholder="Type to filter · Enter to search everything", id="conversation-filter")
        yield ListView(id="conversation-list")
        yield Label("Loading conversations...", id="conversation-status")
        yield Footer()

    @property
    def filter_input(self) -> Input:
        return self.query_one("#conversation-filter", Input)

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
        # The list owns focus; the filter is reached with `/`. Composing the
        # input first would otherwise hand it the initial focus and turn the
        # first Enter into a search.
        self.list_view.focus()
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
                await self._load_projects()
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

    async def _load_projects(self) -> None:
        """Best effort: a list without project names is still a usable list."""
        try:
            projects = await self._catalog_supplier().projects()
        except AgentCoreTuiError as exc:
            logger.info("project names unavailable for the conversation list: %s", exc)
            return
        self._projects = {p.project_id: p for p in projects or []}

    def project_for(self, summary: ConversationSummary) -> Project | None:
        """The project a row belongs to, when it is one the user can see."""
        if not summary.project_id:
            return None
        return self._projects.get(summary.project_id)

    def _visible_rows(self) -> list[ConversationSummary]:
        if self._search_results is not None:
            return self._search_results
        if not self._filter:
            return self._rows
        needle = self._filter.casefold()
        return [row for row in self._rows if needle in row.title.casefold()]

    async def _rebuild(self) -> None:
        await self.list_view.clear()
        if not self._rows and self._search_results is None:
            self.status.update("No conversations yet. Press Esc and ask something.")
            return

        rows = self._visible_rows()
        if not rows:
            where = "match" if self._search_results is None else "result"
            self.status.update(f"No {where}es for '{self._filter}'. Enter searches message text; Ctrl+U clears.")
            return

        by_group: dict[str, list[ConversationSummary]] = {}
        for row in rows:
            by_group.setdefault(group_for(row.last_message_at), []).append(row)

        for heading in GROUP_ORDER:
            group = by_group.get(heading)
            if not group:
                continue
            await self.list_view.append(GroupHeading(heading))
            for summary in group:
                project = self.project_for(summary)
                await self.list_view.append(
                    ConversationRow(summary, project_name=project.name if project else None, marked=summary.session_id in self._marked)
                )

        # Land on the first conversation, not the heading above it, so Enter
        # opens something straight away. Focus moves to the list only when the
        # user is not typing in the filter; a rebuild runs on every keystroke.
        for index, item in enumerate(self.list_view.children):
            if isinstance(item, ConversationRow):
                self.list_view.index = index
                break
        if not self.filter_input.has_focus:
            self.list_view.focus()

        self.status.remove_class("-error")
        if self._search_results is not None:
            self._status_base = f"{len(rows)} result(s) for '{self._filter}' · Ctrl+U clears"
        elif self._filter:
            self._status_base = f"{len(rows)} of {len(self._rows)} match '{self._filter}' · Enter searches message text"
        else:
            more = " · press m for more" if self._next_token else ""
            self._status_base = f"{len(self._rows)} conversation(s){more}"
        self._refresh_status_count()

    # -- filtering -----------------------------------------------------------

    def action_focus_filter(self) -> None:
        self.filter_input.focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "conversation-filter":
            return
        self._filter = event.value.strip()
        # Typing again after a search returns to the local filter.
        self._search_results = None
        await self._rebuild()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "conversation-filter":
            event.stop()
            await self._search(event.value)

    async def _search(self, raw_query: str) -> None:
        """Enter in the filter: search message text server-side, when offered."""
        query = raw_query.strip()
        if not query:
            self._search_results = None
            await self._rebuild()
            self.list_view.focus()
            return
        self.status.update(f"Searching for '{query}'...")
        try:
            results = await self._catalog_supplier().search_conversations(query)
        except AgentCoreTuiError as exc:
            self.status.update(f"Search failed: {exc.message}")
            self.status.add_class("-error")
            return
        if results is None:
            # Not enabled on this deployment; the local title filter already applies.
            self.status.update(f"{len(self._visible_rows())} title match(es) · message search is off on this server")
            self.list_view.focus()
            return
        self._search_results = results
        await self._rebuild()
        self.list_view.focus()

    # -- actions -------------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)

    async def action_reload(self) -> None:
        await self._load(reset=True)

    async def action_load_more(self) -> None:
        if self._next_token:
            await self._load(reset=False)

    async def action_open(self) -> None:
        """Restore the conversation and hand it to the caller.

        Bound to Enter with priority so it works from the list; when the filter
        has focus, Enter means "search" instead and is forwarded there.
        """
        if self.filter_input.has_focus:
            await self._search(self.filter_input.value)
            return
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
        await self._on_open(summary, messages, self.project_for(summary))
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

    def action_toggle_mark(self) -> None:
        """Space marks the highlighted conversation for one Delete of several."""
        item = self.list_view.highlighted_child
        if not isinstance(item, ConversationRow):
            return
        session_id = item.summary.session_id
        if session_id in self._marked:
            self._marked.discard(session_id)
            item.marked = False
        else:
            self._marked.add(session_id)
            item.marked = True
        item.refresh_title()
        self._refresh_status_count()
        if self.list_view.index is not None and self.list_view.index < len(self.list_view.children) - 1:
            self.list_view.action_cursor_down()

    def _refresh_status_count(self) -> None:
        suffix = f" · {len(self._marked)} marked · Delete removes them" if self._marked else ""
        self.status.update(self._status_base + suffix)

    @work
    async def action_delete(self) -> None:
        """Delete the marked conversations, or the highlighted one when none
        are marked."""
        known = {row.session_id: row for row in self._rows + (self._search_results or [])}
        targets = [known[sid] for sid in self._marked if sid in known]
        if not targets:
            summary = self.selected()
            if summary is None:
                return
            targets = [summary]
        if not await self.app.push_screen_wait(ConfirmDelete([t.title for t in targets])):
            return
        deleted: set[str] = set()
        try:
            if len(targets) == 1:
                await self._catalog_supplier().delete(targets[0].session_id)
                deleted = {targets[0].session_id}
            else:
                deleted = await self._catalog_supplier().delete_many([t.session_id for t in targets])
        except AgentCoreTuiError as exc:
            self.notify(exc.message, title="Delete failed", severity="error", timeout=8)
        self._rows = [row for row in self._rows if row.session_id not in deleted]
        if self._search_results is not None:
            self._search_results = [row for row in self._search_results if row.session_id not in deleted]
        self._marked -= deleted
        await self._rebuild()
        if not deleted:
            return
        failed = len(targets) - len(deleted)
        if len(targets) == 1:
            self.notify(f"Deleted {targets[0].title}", timeout=5)
        elif failed:
            self.notify(f"Deleted {len(deleted)}; {failed} could not be deleted and stay marked.", severity="warning", timeout=8)
        else:
            self.notify(f"Deleted {len(deleted)} conversations", timeout=5)

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
