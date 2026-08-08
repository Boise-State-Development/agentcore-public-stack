"""Modal tool picker.

The web UI's Settings → Tools panel, which decides what the agent can reach on
the next turn. Two things happen when you close it, and they are different:

* the chosen set becomes ``enabled_tools`` on subsequent turns, which takes
  effect immediately;
* the same set is persisted with ``PUT /tools/preferences``, so the choice
  outlives the process and follows the user into the web app.

The server's ``isEnabled`` is the resolved answer — it already folds in the
role's default and any stored user override — so this screen shows that and
writes back an explicit decision for every tool. Sending only the enabled ids
would make "I turned this off" indistinguishable from "I did not mention it",
which is exactly the case a user cares about for a tool their role enables by
default.

Selection state lives here rather than in the parent screen because it is
transient: dismissing with Esc must discard it, and that is easier to guarantee
if the parent never sees it.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, SelectionList
from textual.widgets.selection_list import Selection

from ..client.catalog import Tool


class ToolPicker(ModalScreen[dict[str, bool] | None]):
    """Choose which tools the agent may use.

    Dismisses with a **map of tool id to enabled state** — the shape
    ``PUT /tools/preferences`` wants, and the shape that can express "off".
    ``None`` means the user cancelled.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+a", "select_all", "All"),
        Binding("ctrl+d", "select_none", "None"),
    ]

    def __init__(self, tools: list[Tool], *, error: str = "") -> None:
        super().__init__()
        # Sorted by category then name so related tools sit together, which is
        # how the web UI groups them and how users think about them.
        self._tools = sorted(tools, key=lambda tool: (tool.category.lower(), tool.name.lower()))
        self._error = error

    def compose(self) -> ComposeResult:
        with Vertical(id="tool-picker-frame"):
            yield Label("Tools the agent may use", id="picker-title")
            if self._error:
                yield Label(self._error, id="picker-error", classes="-error")
            if not self._tools:
                yield Label(
                    "No tools are available to your account.",
                    id="tool-picker-empty",
                )
            else:
                with VerticalScroll(id="tool-picker-body"):
                    yield SelectionList[str](*self._selections(), id="tool-list")
            yield Label(self._help_text(), id="picker-help")
            with Horizontal(id="tool-picker-actions"):
                yield Button("Save", id="tool-save", variant="primary")
                yield Button("Cancel", id="tool-cancel")

    def _selections(self) -> list[Selection[str]]:
        selections: list[Selection[str]] = []
        current_category = ""
        for tool in self._tools:
            if tool.category and tool.category != current_category:
                current_category = tool.category
                # A disabled-looking header row: SelectionList has no group
                # concept, so the category is carried as an unselectable label
                # prefix on the first tool of each group.
                selections.append(Selection(f"── {current_category} ──", f"__header__{current_category}", False, disabled=True))
            selections.append(Selection(self._label(tool), tool.tool_id, tool.enabled))
        return selections

    def _label(self, tool: Tool) -> str:
        parts = [tool.name]
        if not tool.available:
            # Surfaced rather than hidden: a tool the server reports unhealthy
            # can still be selected, and the turn will simply not use it.
            parts.append(f"[{tool.status}]")
        if tool.requires_oauth_provider:
            # Worth flagging here: choosing it may pause a turn for consent,
            # which the terminal cannot complete on its own.
            parts.append("(needs sign-in)")
        if tool.description:
            parts.append(f"— {tool.description[:60]}")
        return " ".join(parts)

    def _help_text(self) -> str:
        return f"Space toggles · Ctrl+A all · Ctrl+D none · Ctrl+S save · Esc cancel   ({len(self._tools)} available)"

    def on_mount(self) -> None:
        if self._tools:
            self.query_one("#tool-list", SelectionList).focus()

    # -- actions -------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self.dismiss(self._decisions())

    def action_select_all(self) -> None:
        if self._tools:
            self.query_one("#tool-list", SelectionList).select_all()

    def action_select_none(self) -> None:
        if self._tools:
            self.query_one("#tool-list", SelectionList).deselect_all()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tool-save":
            self.action_save()
        else:
            self.action_cancel()

    def _decisions(self) -> dict[str, bool]:
        """An explicit on/off for every tool, headers excluded."""
        if not self._tools:
            return {}
        selected = set(self.query_one("#tool-list", SelectionList).selected)
        return {tool.tool_id: tool.tool_id in selected for tool in self._tools}
