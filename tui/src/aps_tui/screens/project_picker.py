"""Modal project picker, driven by ``GET /projects``.

A Shared Project's chat behaviour lives in a hidden Agent (its *harness*);
a conversation joins the project by binding to that harness on its first
turn. This screen only chooses; the chat screen decides what the choice
means for the current conversation (a new one if the current has messages,
since the server will not rebind a conversation that has started).

Two groups, as the web app's list page has them: **Mine** (owned) and
**Shared with me**, with the caller's role and the owner shown on shared rows.
Archived projects are listed, dimmed, because the user may want to *read*
their conversations; choosing one is allowed and the server's refusal says
why a new turn cannot run.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from ..client.catalog import Project

#: Sentinel option id for "leave the project".
_NONE = "__none__"


class ProjectPicker(ModalScreen[Project | None | bool]):
    """Choose a project to run conversations in.

    Dismisses with a :class:`Project`, with ``False`` for "no project" (leave
    the current one), or ``None`` when cancelled. Three outcomes rather than
    two because "cancel" and "leave" must not be confused: Esc from a picker
    opened by habit should change nothing.
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss_picker", "Cancel")]

    def __init__(self, projects: list[Project], *, current: str | None = None, error: str = "") -> None:
        super().__init__()
        self._projects = projects
        self._current = current
        self._error = error

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-frame") as frame:
            frame.border_title = "Project"
            yield Label("Which project should new conversations run in?", id="picker-title")
            if self._error:
                yield Label(self._error, id="picker-error", classes="-error")
            yield OptionList(*self._options(), id="picker-list")
            yield Label("↑↓ move · Enter select · Esc cancel", id="picker-help")

    def _options(self) -> list[Option]:
        options: list[Option] = [Option(self._row("No project", "", current=self._current is None), id=_NONE)]
        mine = [p for p in self._projects if p.mine]
        shared = [p for p in self._projects if not p.mine]
        if mine:
            options.append(Option(Text("Mine", style="dim"), disabled=True))
            options.extend(self._project_option(p) for p in mine)
        if shared:
            options.append(Option(Text("Shared with me", style="dim"), disabled=True))
            options.extend(self._project_option(p) for p in shared)
        return options

    def _project_option(self, project: Project) -> Option:
        detail_parts: list[str] = []
        if not project.mine:
            detail_parts.append(project.role)
            detail_parts.append(project.owner_label)
        if project.archived:
            detail_parts.append("archived")
        label = self._row(
            project.name,
            " · ".join(detail_parts),
            current=project.project_id == self._current,
            dim=project.archived,
        )
        return Option(label, id=project.project_id)

    @staticmethod
    def _row(name: str, detail: str, *, current: bool, dim: bool = False) -> Text:
        text = Text()
        text.append("● " if current else "  ")
        text.append(name, style="dim" if dim else "")
        if detail:
            text.append(f"  {detail}", style="dim")
        return text

    def on_mount(self) -> None:
        option_list = self.query_one("#picker-list", OptionList)
        option_list.focus()
        if self._current is None:
            option_list.highlighted = 0
            return
        for index in range(option_list.option_count):
            if option_list.get_option_at_index(index).id == self._current:
                option_list.highlighted = index
                return

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        chosen = event.option.id or ""
        if chosen == _NONE:
            self.dismiss(False)
            return
        for project in self._projects:
            if project.project_id == chosen:
                self.dismiss(project)
                return
        self.dismiss(None)

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
