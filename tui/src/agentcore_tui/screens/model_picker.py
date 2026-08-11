"""Modal model picker.

The model list comes from configuration, not from the server: ``GET /models`` is
cookie-session authenticated, so an API-key client cannot enumerate the catalog.
Set ``models = [...]`` in the config file to match your deployment.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option


class ModelPicker(ModalScreen[str | None]):
    """Choose the model for subsequent turns. Dismisses with the model ID."""

    BINDINGS = [("escape", "dismiss_picker", "Cancel")]

    def __init__(self, models: tuple[str, ...], current: str) -> None:
        super().__init__()
        self._models = models
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-frame"):
            yield Label("Select a model", id="picker-title")
            yield OptionList(
                *(Option(self._label_for(model), id=model) for model in self._models),
                id="picker-list",
            )
            yield Label("Enter to select · Esc to cancel", id="picker-help")

    def _label_for(self, model_id: str) -> str:
        marker = "> " if model_id == self._current else "  "
        return f"{marker}{model_id}"

    def on_mount(self) -> None:
        option_list = self.query_one("#picker-list", OptionList)
        option_list.focus()
        if self._current in self._models:
            option_list.highlighted = self._models.index(self._current)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
