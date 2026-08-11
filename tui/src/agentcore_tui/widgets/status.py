"""The one-line status bar under the composer."""

from __future__ import annotations

from textual.widgets import Static

from ..client.events import Usage


def _short_model(model_id: str) -> str:
    """Condense a Bedrock model ID for a status line."""
    return model_id.rsplit(".", 1)[-1] or model_id


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


class StatusBar(Static):
    """Shows the active model, current state, and last turn's token usage.

    Cost is intentionally absent: pricing lives server-side (the endpoint records
    it via ``CostCalculator``) and this client has no pricing table, so any
    number shown here would be invented.
    """

    def __init__(self) -> None:
        super().__init__("", id="status-bar", markup=False)
        self._model_id = ""
        self._state = "Ready"
        self._usage_text = ""
        self._turns = 0
        self._line = ""

    @property
    def line(self) -> str:
        """The text currently displayed. Public so tests need no private access."""
        return self._line

    def _render_line(self) -> str:
        segments = [self._state]
        if self._model_id:
            segments.append(_short_model(self._model_id))
        if self._turns:
            segments.append(f"{self._turns} turn{'s' if self._turns != 1 else ''}")
        if self._usage_text:
            segments.append(self._usage_text)
        return "  |  ".join(segment for segment in segments if segment)

    def refresh_line(self) -> None:
        self._line = self._render_line()
        self.update(self._line)

    def set_model(self, model_id: str) -> None:
        self._model_id = model_id
        self.refresh_line()

    def set_state(self, state: str, *, busy: bool = False, error: bool = False) -> None:
        self._state = state
        self.set_class(busy, "-busy")
        self.set_class(error, "-error")
        self.refresh_line()

    def set_usage(self, usage: Usage | None) -> None:
        self._usage_text = format_usage(usage)
        self.refresh_line()

    def set_turns(self, turns: int) -> None:
        self._turns = turns
        self.refresh_line()
