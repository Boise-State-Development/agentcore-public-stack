"""The slash-command registry.

Every feature of the terminal client is a slash command typed into the
composer; nothing is reached by a function key or a palette (steering:
"UX paradigm"). This module is the single source of truth for what those
commands are. Three things read it and must never disagree:

* the completion popup that opens when the composer's text starts with ``/``,
* ``/help``, which is generated from it rather than hand-written,
* the dispatcher in ``screens/chat.py``, which looks the typed name up here
  and refuses anything the registry does not know.

Pure Python, no Textual, so the registry is testable without an app and the
help text cannot drift from the completion list.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SlashCommand:
    """One command. ``name`` is what the user types after the slash."""

    name: str
    summary: str
    #: Argument hint shown beside the name, e.g. ``"[name]"`` or ``"<path>"``.
    usage: str = ""
    #: Alternative spellings that dispatch to the same command. Never shown in
    #: the popup or ``/help``; they exist so a habit from another tool still
    #: lands somewhere (``/exit`` for ``/quit``).
    aliases: tuple[str, ...] = ()
    #: Shown in the popup only when signed in with a BFF session. Commands that
    #: need the agent dialect (tools, conversations, attachments) set this so
    #: an API-key user is not offered things that cannot work.
    agent_only: bool = False
    #: Extra words the filter matches on, so ``/his`` finds ``/conversations``.
    keywords: tuple[str, ...] = field(default=())
    #: A server skill rather than a client command. Never in the registry:
    #: the popup lists enabled skills after the commands, and choosing one
    #: completes ``/slug `` for a message instead of running anything.
    skill: bool = False

    @property
    def display(self) -> str:
        """``/name usage``, as shown in the popup and in ``/help``."""
        return f"/{self.name} {self.usage}".rstrip()

    def matches(self, prefix: str) -> bool:
        """Whether a typed prefix (without the slash) selects this command.

        A prefix match on the name or a keyword — not on aliases, which are
        habit-catchers for dispatch and would make ``/c`` offer ``/new``. The
        empty prefix matches everything, which is what an empty ``/`` lists.
        """
        needle = prefix.lower()
        if not needle:
            return True
        candidates = (self.name, *self.keywords)
        return any(candidate.startswith(needle) for candidate in candidates)


class CommandRegistry:
    """An ordered set of commands. Order is the order the popup shows."""

    def __init__(self, commands: list[SlashCommand]) -> None:
        self._commands = list(commands)
        self._by_name: dict[str, SlashCommand] = {}
        for command in self._commands:
            for spelling in (command.name, *command.aliases):
                if spelling in self._by_name:
                    raise ValueError(f"slash command spelling {spelling!r} is registered twice")
                self._by_name[spelling] = command

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._commands)

    def __len__(self) -> int:
        return len(self._commands)

    def resolve(self, typed: str) -> SlashCommand | None:
        """The command for a typed name or alias, or None."""
        return self._by_name.get(typed.lower())

    def visible(self, *, agent: bool) -> list[SlashCommand]:
        """Commands the popup and ``/help`` should list for this session kind."""
        return [command for command in self._commands if agent or not command.agent_only]

    def match(self, prefix: str, *, agent: bool) -> list[SlashCommand]:
        """Commands to offer for a partial name, in registry order."""
        return [command for command in self.visible(agent=agent) if command.matches(prefix)]

    def help_lines(self, *, agent: bool) -> list[str]:
        """``/help`` body: one aligned line per visible command."""
        visible = self.visible(agent=agent)
        width = max(len(command.display) for command in visible) + 2
        return [f"{command.display.ljust(width)}{command.summary}" for command in visible]


#: Every command the chat screen understands. Keep this list the user's view
#: of the product: features first in the order someone reaches for them, then
#: the housekeeping commands. Adding a feature means adding a line here and a
#: case in ``ChatScreen._run_slash_command`` — the dispatcher test asserts the
#: two agree.
COMMANDS = CommandRegistry(
    [
        SlashCommand("new", "Start a new conversation", aliases=("clear-conversation",)),
        SlashCommand("model", "Pick a model, or switch straight to one whose name contains `name`", usage="[name]"),
        SlashCommand(
            "tools",
            "Choose which server tools the agent may use; `local on|off` toggles tools that run on this machine",
            usage="[local on|off]",
            agent_only=True,
        ),
        SlashCommand(
            "conversations",
            "Browse, open, rename or delete your saved conversations",
            usage="[search]",
            aliases=("chats", "history"),
            agent_only=True,
            keywords=("sessions",),
        ),
        SlashCommand(
            "project",
            "Run conversations in a Shared Project: pick one, name one, `off` to leave, `info` for details, `shared` for teammates' conversations",
            usage="[name|off|info|shared]",
            agent_only=True,
            keywords=("workspace",),
        ),
        SlashCommand(
            "agent",
            "Talk to one of your agents (own, shared or pinned): pick one, name one, `off` to stop, `info` for details",
            usage="[name|off|info]",
            agent_only=True,
        ),
        SlashCommand(
            "skills",
            "Choose which skills the agent may use; type /<skill> in a message to invoke one",
            agent_only=True,
        ),
        SlashCommand(
            "mode",
            "Set this conversation's mode (a named system prompt), `off` to clear it",
            usage="[name|off]",
            agent_only=True,
        ),
        SlashCommand("attach", "Upload a file and send it with your next message", usage="<path>", agent_only=True),
        SlashCommand("continue", "Let an answer that hit the token limit carry on", agent_only=True),
        SlashCommand("stop", "Stop the turn in flight", aliases=("cancel",)),
        SlashCommand("copy", "Copy the last answer, or `all` for the whole conversation as Markdown", usage="[all]"),
        SlashCommand("clear", "Clear the screen; the conversation continues"),
        SlashCommand("theme", "Switch the colour theme, or list the themes", usage="[name]"),
        SlashCommand("status", "Show where you are signed in, which model is set, and the log file"),
        SlashCommand("screenshot", "Save an SVG snapshot of the screen"),
        SlashCommand("help", "List the commands", aliases=("?",)),
        SlashCommand("quit", "Leave", aliases=("exit", "q")),
    ]
)
