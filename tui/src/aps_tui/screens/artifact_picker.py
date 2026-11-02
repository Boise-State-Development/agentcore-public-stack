"""Modal picker over the artifacts of one conversation (``GET /artifacts``).

Dismisses with the chosen :class:`ArtifactVersion`, or None on cancel. What
happens next (saving the source to disk) is the chat screen's business.
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

from ..client.catalog import ArtifactVersion


class ArtifactPicker(ModalScreen[ArtifactVersion | None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss_picker", "Cancel")]

    def __init__(self, artifacts: list[ArtifactVersion], *, target: str) -> None:
        super().__init__()
        self._artifacts = artifacts
        self._target = target

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-frame") as frame:
            frame.border_title = "Artifacts"
            yield Label(f"Save to {self._target}", id="picker-title")
            yield OptionList(*(Option(self._row(a), id=f"{a.artifact_id}@{a.version}") for a in self._artifacts), id="picker-list")
            yield Label("↑↓ move · Enter save · Esc cancel", id="picker-help")

    @staticmethod
    def _row(artifact: ArtifactVersion) -> Text:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(artifact.title or artifact.artifact_id)
        text.append(f"  v{artifact.version}", style="dim")
        text.append(f"  → {artifact.file_name()}", style="dim")
        return text

    def on_mount(self) -> None:
        option_list = self.query_one("#picker-list", OptionList)
        option_list.focus()
        if option_list.option_count:
            option_list.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        key = event.option.id or ""
        self.dismiss(next((a for a in self._artifacts if f"{a.artifact_id}@{a.version}" == key), None))

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
