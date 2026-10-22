"""The one-line status bar under the composer."""

from __future__ import annotations

from textual.containers import Horizontal
from textual.timer import Timer
from textual.widgets import Static

from ..usage import Usage

#: Braille spinner, one frame per tick while a turn is in flight. Deliberately
#: not a Textual LoadingIndicator: that is a block-level widget, and this needs
#: to live inside one status line.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_INTERVAL = 0.08

#: Separator between the segments on the right-hand side.
SEPARATOR = "  ·  "


def short_model(model_id: str) -> str:
    """Condense a Bedrock model ID for a status line.

    ``us.anthropic.claude-haiku-4-5-20251001-v1:0`` -> ``claude-haiku-4-5``.
    Shared with the transcript label so the two never disagree about a name.

    Only alphabetic dotted prefixes (a region, a vendor) are dropped. A dot
    inside a version — ``gpt-5.4`` — is part of the name; splitting on the last
    dot used to show that model as ``4``.
    """
    parts = model_id.split(".")
    while len(parts) > 1 and parts[0].isalpha():
        parts.pop(0)
    name = ".".join(parts) or model_id
    keep: list[str] = []
    for part in name.split("-"):
        # Stop at the date stamp / version suffix.
        if part.isdigit() and len(part) == 8:
            break
        if part.startswith("v") and part[1:].split(":")[0].isdigit():
            break
        keep.append(part)
    return "-".join(keep) or name


def format_usage(usage: Usage | None) -> str:
    """Render token counts, including cache hits when the model reports them."""
    if usage is None:
        return ""
    parts = [f"{usage.input_tokens:,} in", f"{usage.output_tokens:,} out"]
    if usage.cache_read_input_tokens:
        parts.append(f"{usage.cache_read_input_tokens:,} cached")
    if usage.cache_write_input_tokens:
        parts.append(f"{usage.cache_write_input_tokens:,} cache-write")
    return " · ".join(parts)


class StatusBar(Horizontal):
    """Shows the current state on the left; model, turns and token usage on the right.

    Cost is intentionally absent: pricing lives server-side (the endpoint records
    it via ``CostCalculator``) and this client has no pricing table, so any
    number shown here would be invented.
    """

    def __init__(self) -> None:
        super().__init__(id="status-bar")
        self._model_id = ""
        self._state = "Ready"
        self._usage_text = ""
        self._turns = 0
        self._line = ""
        self._busy = False
        self._spinner_timer: Timer | None = None
        self._spinner_index = 0
        self._state_widget = Static("", id="status-state", markup=False)
        self._meta_widget = Static("", id="status-meta", markup=False)

    def compose(self):  # type: ignore[no-untyped-def]
        yield self._state_widget
        yield self._meta_widget

    def on_mount(self) -> None:
        self.refresh_line()

    @property
    def line(self) -> str:
        """The text currently displayed. Public so tests need no private access."""
        return self._line

    def _meta_text(self) -> str:
        segments: list[str] = []
        if self._model_id:
            segments.append(short_model(self._model_id))
        if self._turns:
            segments.append(f"{self._turns} turn{'s' if self._turns != 1 else ''}")
        if self._usage_text:
            segments.append(self._usage_text)
        return SEPARATOR.join(segments)

    def _state_text(self) -> str:
        if self._busy:
            return f"{SPINNER_FRAMES[self._spinner_index]} {self._state}"
        return self._state

    def refresh_line(self) -> None:
        meta = self._meta_text()
        self._line = f"{self._state}{SEPARATOR}{meta}" if meta else self._state
        self._state_widget.update(self._state_text())
        self._meta_widget.update(meta)

    def _tick(self) -> None:
        self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
        self._state_widget.update(self._state_text())

    def set_model(self, model_id: str) -> None:
        self._model_id = model_id
        self.refresh_line()

    def set_state(self, state: str, *, busy: bool = False, error: bool = False) -> None:
        self._state = state
        self._busy = busy
        self.set_class(busy, "-busy")
        self.set_class(error, "-error")
        if busy and self._spinner_timer is None and self.is_attached:
            self._spinner_timer = self.set_interval(SPINNER_INTERVAL, self._tick)
        elif not busy and self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self.refresh_line()

    def set_usage(self, usage: Usage | None) -> None:
        self._usage_text = format_usage(usage)
        self.refresh_line()

    def set_turns(self, turns: int) -> None:
        self._turns = turns
        self.refresh_line()
