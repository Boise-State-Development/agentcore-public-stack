"""Modal skill picker.

Skills are opt-in (the reverse of tools): an untouched skill is off, and a
turn carries only the ones switched on. Closing with Save applies the set to
the next turn *and* persists it with ``PUT /skills/preferences``, so the
choice follows the user into the web app. Each row shows the ``/slug`` that
invokes the skill by name in a message.
"""

from __future__ import annotations

from typing import ClassVar

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, SelectionList
from textual.widgets.selection_list import Selection

from ..client.catalog import Skill


class SkillPicker(ModalScreen[dict[str, bool] | None]):
    """Dismisses with ``{skill_id: enabled}`` for every skill, or None on cancel."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+d", "select_none", "None"),
    ]

    def __init__(self, skills: list[Skill], *, locked_by: str | None = None) -> None:
        super().__init__()
        self._skills = sorted(skills, key=lambda s: ((s.category or "").lower(), s.name.lower()))
        self._locked_by = locked_by

    def compose(self) -> ComposeResult:
        with Vertical(id="tool-picker-frame") as frame:
            frame.border_title = "Skills"
            yield Label("Skills the agent may use. Off unless you switch them on.", id="picker-title")
            if self._locked_by:
                yield Label(
                    f"This conversation runs in {escape(self._locked_by)}, which brings its own skills; " "these apply to conversations outside it.",
                    id="picker-error",
                )
            if not self._skills:
                yield Label("No skills are available to your account.", id="tool-picker-empty")
            else:
                with VerticalScroll(id="tool-picker-body"):
                    yield SelectionList[str](*self._selections(), id="tool-list")
            yield Label(f"Space toggle · Ctrl+S save · Ctrl+D none · Esc cancel   ({len(self._skills)} available)", id="picker-help")

    def _selections(self) -> list[Selection[str]]:
        width = max((len(s.slug) for s in self._skills), default=0) + 1
        rows = []
        for skill in self._skills:
            slug = f"/{skill.slug}".ljust(width + 1) if skill.slug else " " * (width + 1)
            label = f"[dim]{escape(slug)}[/] [b]{escape(skill.name)}[/]"
            if skill.description:
                label += f"  [dim]{escape(skill.description[:70])}[/]"
            rows.append(Selection(label, skill.skill_id, skill.enabled))
        return rows

    def on_mount(self) -> None:
        if self._skills:
            self.query_one("#tool-list", SelectionList).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        if not self._skills:
            self.dismiss({})
            return
        selected = set(self.query_one("#tool-list", SelectionList).selected)
        self.dismiss({s.skill_id: s.skill_id in selected for s in self._skills})

    def action_select_none(self) -> None:
        if self._skills:
            self.query_one("#tool-list", SelectionList).deselect_all()
