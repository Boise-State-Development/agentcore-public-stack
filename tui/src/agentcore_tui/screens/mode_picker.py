"""Modal conversation-mode picker (``GET /system-prompts/``).

A mode is a named system prompt an administrator or the user defined, added to
the agent's instructions for this conversation. Dismisses with the chosen
:class:`SystemPromptOption`, ``False`` for "no mode", or ``None`` on cancel.
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

from ..client.catalog import SystemPromptOption

_NONE = "__none__"


class ModePicker(ModalScreen[SystemPromptOption | None | bool]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss_picker", "Cancel")]

    def __init__(self, modes: list[SystemPromptOption], *, current: str | None = None) -> None:
        super().__init__()
        self._modes = sorted(modes, key=lambda m: m.name.casefold())
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-frame") as frame:
            frame.border_title = "Mode"
            yield Label("How should the agent approach this conversation?", id="picker-title")
            options = [Option(self._row("No mode", "", self._current is None), id=_NONE)]
            options += [Option(self._row(m.name, m.description, m.prompt_id == self._current), id=m.prompt_id) for m in self._modes]
            yield OptionList(*options, id="picker-list")
            yield Label("↑↓ move · Enter select · Esc cancel", id="picker-help")

    @staticmethod
    def _row(name: str, description: str, current: bool) -> Text:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("● " if current else "  ")
        text.append(name)
        if description:
            text.append(f"  {description}", style="dim")
        return text

    def on_mount(self) -> None:
        option_list = self.query_one("#picker-list", OptionList)
        option_list.focus()
        option_list.highlighted = 0
        for index in range(option_list.option_count):
            if option_list.get_option_at_index(index).id == self._current:
                option_list.highlighted = index

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == _NONE:
            self.dismiss(False)
            return
        self.dismiss(next((m for m in self._modes if m.prompt_id == event.option.id), None))

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
