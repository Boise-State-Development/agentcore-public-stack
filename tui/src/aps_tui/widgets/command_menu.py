"""The ``/`` completion popup.

Sits directly above the composer and appears when the composer's text is a
slash followed by (part of) a command name. It never takes focus: the
composer keeps it, and the chat screen forwards Up, Down, Tab, Enter and
Escape here while the menu is visible, so typing continues to filter the
list and the user's hands never leave the prompt.

What it is not: a palette. It lists only slash commands, which is the whole
feature surface (steering: "UX paradigm"), so the list *is* the documentation.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from ..commands import COMMANDS, CommandRegistry, SlashCommand


class CommandMenu(OptionList):
    """Filtered list of slash commands, driven from outside."""

    #: Most rows the menu grows to before it scrolls.
    MAX_ROWS = 8

    def __init__(self, registry: CommandRegistry = COMMANDS, *, agent: bool) -> None:
        super().__init__(id="command-menu")
        self._registry = registry
        self._agent = agent
        self._shown: list[SlashCommand] = []
        #: Enabled skills, offered after the commands. Set by the chat screen.
        self._skills: list[SlashCommand] = []
        self.can_focus = False
        self.display = False

    # -- state ---------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    @property
    def commands(self) -> list[SlashCommand]:
        """The commands currently listed, in display order."""
        return list(self._shown)

    @property
    def current(self) -> SlashCommand | None:
        """The highlighted command, if the menu is showing one."""
        if not self._shown or self.highlighted is None:
            return None
        return self._shown[self.highlighted]

    def set_skills(self, skills: list[tuple[str, str]]) -> None:
        """``(slug, display name)`` for each enabled skill. A slug that is also a
        command name is left out: commands win, so it could never be invoked
        from the start of a message anyway."""
        self._skills = [
            SlashCommand(slug, f"skill · {name}", usage="<message>", skill=True)
            for slug, name in skills
            if slug and self._registry.resolve(slug) is None
        ]

    def set_agent(self, agent: bool) -> None:
        """Signing in or out changes which commands are offered."""
        self._agent = agent

    # -- driving -------------------------------------------------------------

    def update_for(self, text: str) -> None:
        """Show, filter, or hide the menu for the composer's current text.

        Visible only while the text is ``/`` plus a partial *name* — no space
        yet and a single line. Once the user has typed a space they are on to
        arguments, and once they have a newline they are writing a prompt.
        """
        if not text.startswith("/") or text.startswith("//") or " " in text or "\n" in text:
            self.hide()
            return
        prefix = text[1:]
        matches = self._registry.match(prefix, agent=self._agent)
        if self._agent:
            matches += [skill for skill in self._skills if skill.matches(prefix)]
        if not matches:
            self.hide()
            return
        previous = self.current
        self._shown = matches
        self.clear_options()
        width = max(len(command.display) for command in matches) + 2
        self.add_options([self._option_for(command, width) for command in matches])
        # Keep the highlight on the same command through a keystroke when it
        # survives the filter; otherwise the first match. Exact name match wins
        # so `/clear` highlights `clear` even though `clear-conversation` is
        # also listed.
        index = 0
        for position, command in enumerate(matches):
            if command.name == prefix.lower():
                index = position
                break
        else:
            if previous is not None and previous in matches:
                index = matches.index(previous)
        self.highlighted = index
        self.styles.height = min(len(matches), self.MAX_ROWS) + 2  # + border
        self.display = True

    def hide(self) -> None:
        self._shown = []
        self.display = False

    def move(self, delta: int) -> None:
        """Move the highlight, wrapping at the ends."""
        if not self._shown:
            return
        current = self.highlighted or 0
        self.highlighted = (current + delta) % len(self._shown)

    @staticmethod
    def _option_for(command: SlashCommand, width: int) -> Option:
        """One row: the command in a fixed-width column, then its summary."""
        label = Text(no_wrap=True, overflow="ellipsis")
        label.append(command.display.ljust(width), style="bold")
        label.append(command.summary, style="dim")
        return Option(label, id=command.name)
