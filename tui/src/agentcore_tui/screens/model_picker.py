"""Modal model picker, driven by the server's catalogue.

It used to read a hand-maintained list from the config file, because ``GET
/models`` is session-authenticated and the client only had an API key. A
signed-in client can ask, so it asks — a config list cannot know what a
deployment has enabled, which models a user's roles allow, or which provider
serves each one.

That last point is why this returns a :class:`ModelChoice` rather than a string.
``model_id`` and ``provider`` are one decision — this deployment serves Claude
through ``bedrock`` and Gemma and GPT through ``mantle`` — and carrying the pair
means the turn sends what the catalogue actually said. The server would resolve a
missing provider from its registry, so this is precision rather than a fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from ..client.catalog import Model


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """A selected model, with the provider that serves it."""

    model_id: str
    provider: str | None = None

    @property
    def is_system_default(self) -> bool:
        """True for the "let the server decide" option, which sends neither."""
        return not self.model_id


#: Offered first, and sends no `model_id` and no `provider` at all — matching the
#: web UI's "System Default", where the deployment's own default applies.
SYSTEM_DEFAULT = ModelChoice(model_id="", provider=None)


class ModelPicker(ModalScreen[ModelChoice | None]):
    """Choose the model for subsequent turns."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss_picker", "Cancel")]

    def __init__(
        self,
        models: list[Model],
        *,
        current: str = "",
        error: str = "",
    ) -> None:
        super().__init__()
        self._models = models
        self._current = current
        self._error = error

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-frame"):
            yield Label("Select a model", id="picker-title")
            if self._error:
                # A failed catalogue fetch still shows the picker, because
                # "System Default" remains selectable and is a working choice.
                yield Label(self._error, id="picker-error", classes="-error")
            yield OptionList(*self._options(), id="picker-list")
            yield Label("Enter to select · Esc to cancel", id="picker-help")

    def _options(self) -> list[Option]:
        options = [Option(self._label("System Default", "", ""), id="")]
        for model in self._models:
            options.append(Option(self._label(model.label, model.provider_name or model.provider, model.model_id), id=model.model_id))
        return options

    def _label(self, name: str, provider: str, model_id: str) -> str:
        marker = "> " if model_id == self._current else "  "
        # The provider is shown, not hidden, because it is the difference between
        # two models with similar names and it is what actually gets sent.
        suffix = f"  ({provider})" if provider else ""
        return f"{marker}{name}{suffix}"

    def on_mount(self) -> None:
        option_list = self.query_one("#picker-list", OptionList)
        option_list.focus()
        for index, model in enumerate(self._models, start=1):
            if model.model_id == self._current:
                option_list.highlighted = index
                break

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        chosen = event.option.id or ""
        if not chosen:
            self.dismiss(SYSTEM_DEFAULT)
            return
        for model in self._models:
            if model.model_id == chosen:
                self.dismiss(ModelChoice(model_id=model.model_id, provider=model.provider))
                return
        # Unreachable through the UI, but a stale list must not silently send a
        # model without its provider.
        self.dismiss(ModelChoice(model_id=chosen, provider=None))

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
