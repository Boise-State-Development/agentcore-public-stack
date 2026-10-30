"""Modal Agent picker: your own Agents, those shared with you, and your pins.

The same scope as the web app's ``@`` menu, for the same reason: it is a way to
reach Agents you already know, not a store browser. The store is the web app's
job. Choosing one starts conversations with it; the chat screen decides what
that means for the current conversation.
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

from ..client.catalog import Agent

_NONE = "__none__"
_GROUPS = (("own", "Mine"), ("shared", "Shared with me"), ("pinned", "Pinned"))


class AgentPicker(ModalScreen[Agent | None | bool]):
    """Dismisses with an :class:`Agent`, ``False`` for "no agent", or ``None`` on cancel."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss_picker", "Cancel")]

    def __init__(self, agents: list[Agent], *, current: str | None = None) -> None:
        super().__init__()
        self._agents = agents
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-frame") as frame:
            frame.border_title = "Agent"
            yield Label("Which agent should new conversations talk to?", id="picker-title")
            if not self._agents:
                yield Label("You have no agents yet. Make or pin one in the web app.", id="picker-error")
            yield OptionList(*self._options(), id="picker-list")
            yield Label("↑↓ move · Enter select · Esc cancel", id="picker-help")

    def _options(self) -> list[Option]:
        options: list[Option] = [Option(self._row("No agent", "", current=self._current is None), id=_NONE)]
        for source, heading in _GROUPS:
            group = [a for a in self._agents if a.source == source]
            if not group:
                continue
            options.append(Option(Text(heading, style="dim"), disabled=True))
            for agent in group:
                detail = agent.owner if source != "own" else ""
                options.append(
                    Option(self._row(agent.label, detail, current=agent.agent_id == self._current, summary=agent.summary), id=agent.agent_id)
                )
        return options

    @staticmethod
    def _row(name: str, detail: str, *, current: bool, summary: str = "") -> Text:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append("● " if current else "  ")
        text.append(name)
        if detail:
            text.append(f"  {detail}", style="dim")
        if summary:
            text.append(f"  {summary}", style="dim italic")
        return text

    def on_mount(self) -> None:
        option_list = self.query_one("#picker-list", OptionList)
        option_list.focus()
        option_list.highlighted = 0
        if self._current is None:
            return
        for index in range(option_list.option_count):
            if option_list.get_option_at_index(index).id == self._current:
                option_list.highlighted = index
                return

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == _NONE:
            self.dismiss(False)
            return
        for agent in self._agents:
            if agent.agent_id == event.option.id:
                self.dismiss(agent)
                return
        self.dismiss(None)

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
