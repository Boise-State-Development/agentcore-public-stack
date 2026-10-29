"""Prompts for a paused turn: the user answers in the transcript, the turn resumes.

Each prompt is a focused widget with its own keys, mounted where the answer
will later appear. All of them post one :class:`InterruptAnswered` message
carrying the ``{"interruptId", "response"}`` entry the server wants; the screen
collects one per outstanding interrupt and resumes once it has them all.

The response shapes are the SPA's, verbatim, because the server parses them
tolerantly but the SPA's are the ones that are tested:

* client tool  → ``{"output": str, "isError": bool}`` or ``{"declined": true}``
* tool approval → ``"approved"`` or ``"declined"``
* user question → ``{"answers": {header: {"selected": [...], "freeText": str}}}``
  or ``{"skipped": true}``
* OAuth consent → ``"consented"`` after the user signs in, in a browser
* browser login → ``{"skipped": true}``; the live view is browser-only
"""

from __future__ import annotations

import json
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, Label, Static

from ..client.agent_events import (
    BrowserLoginRequired,
    ClientToolCall,
    OAuthRequired,
    ToolApprovalRequired,
    UserQuestionRequired,
)
from ..local_tools import ApprovalDecision, LocalToolCall, describe_call


class InterruptAnswered(Message):
    """The user answered one prompt. ``entry`` is the resume payload entry."""

    def __init__(self, interrupt_id: str, response: Any, *, summary: str) -> None:
        super().__init__()
        self.interrupt_id = interrupt_id
        self.response = response
        #: One line for the prompt to collapse into once answered.
        self.summary = summary

    @property
    def entry(self) -> dict[str, Any]:
        return {"interruptId": self.interrupt_id, "response": self.response}


class LocalToolApproval(Message):
    """The user decided about a local tool call. The screen runs it (or not)
    and posts the result as the interrupt's answer."""

    def __init__(self, call: LocalToolCall, decision: ApprovalDecision) -> None:
        super().__init__()
        self.call = call
        self.decision = decision


class _Prompt(Vertical, can_focus=True):
    """Shared chrome: a title, a body, a key line; collapses to one line when done."""

    DEFAULT_CLASSES = "notice interrupt-notice prompt"

    def __init__(self, interrupt_id: str) -> None:
        super().__init__()
        self.interrupt_id = interrupt_id
        self.answered = False
        self._outcome = ""

    def settle(self, summary: str) -> None:
        """Replace the prompt with its outcome.

        May be called more than once — first with "running", then with the
        result — and the latest summary wins. The swap is a worker so removal
        is awaited before the outcome mounts; mounting first would land the
        new line above the old children for a frame and then lose it.
        """
        self.answered = True
        self.remove_class("prompt")
        self.add_class("-settled")
        self.can_focus = False
        self._outcome = summary
        self.run_worker(self._swap_in_outcome(), exclusive=True, group="settle")

    async def _swap_in_outcome(self) -> None:
        await self.remove_children()
        await self.mount(Static(self._outcome, markup=False, classes="prompt-outcome"))


class ClientToolPrompt(_Prompt):
    """Approve or decline a tool that would run on this machine.

    ``y`` runs it, ``n`` declines, ``a`` runs it and every later call to the
    same tool this session. Nothing runs on Enter alone, so an accidental
    keypress while reading cannot execute a command.
    """

    BINDINGS = [
        Binding("y", "approve", "Run"),
        Binding("n", "decline", "Decline"),
        Binding("a", "always", "Always"),
    ]

    def __init__(self, event: ClientToolCall) -> None:
        super().__init__(event.interrupt_id)
        self.call = LocalToolCall(
            interrupt_id=event.interrupt_id,
            tool_use_id=event.tool_use_id,
            tool_name=event.tool_name,
            arguments=event.arguments,
        )

    def compose(self) -> ComposeResult:
        headline, detail = describe_call(self.call)
        yield Label(f"Run on this machine · {self.call.tool_name}", classes="message-role")
        yield Static(headline, markup=False, classes="prompt-command")
        if detail:
            yield Static(detail, markup=False, classes="notice-hint")
        yield Static("[b]y[/] run   [b]n[/] decline   [b]a[/] always allow this tool", classes="prompt-keys")

    def action_approve(self) -> None:
        self.post_message(LocalToolApproval(self.call, ApprovalDecision.APPROVE))

    def action_decline(self) -> None:
        self.post_message(LocalToolApproval(self.call, ApprovalDecision.DECLINE))

    def action_always(self) -> None:
        self.post_message(LocalToolApproval(self.call, ApprovalDecision.ALWAYS))


class ToolApprovalPrompt(_Prompt):
    """The server's per-tool approval gate (an admin marked an MCP tool as
    requiring consent). Same keys as the local prompt, minus "always": the
    server decides that policy, not this client."""

    BINDINGS = [
        Binding("y", "approve", "Approve"),
        Binding("n", "decline", "Decline"),
    ]

    def __init__(self, event: ToolApprovalRequired) -> None:
        super().__init__(event.interrupt_id)
        self.event = event

    def compose(self) -> ComposeResult:
        yield Label(f"Approval needed · {self.event.tool_name or 'tool'}", classes="message-role")
        yield Static(self.event.message or f"The agent wants to run {self.event.tool_name or 'a tool'}.", markup=False)
        if self.event.tool_input:
            yield Static(_pretty(self.event.tool_input), markup=False, classes="notice-hint")
        yield Static("[b]y[/] approve   [b]n[/] decline", classes="prompt-keys")

    def action_approve(self) -> None:
        self.post_message(InterruptAnswered(self.interrupt_id, "approved", summary=f"✓ Approved {self.event.tool_name}"))

    def action_decline(self) -> None:
        self.post_message(InterruptAnswered(self.interrupt_id, "declined", summary=f"✗ Declined {self.event.tool_name}"))


class UserQuestionPrompt(_Prompt):
    """The agent's multiple-choice questions, one at a time.

    Number keys pick an option; for multi-select they toggle and Enter
    confirms. ``o`` opens a free-text "Other" field, ``s`` skips the question.
    The answer shape is the SPA's.
    """

    BINDINGS = [
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("o", "other", "Other"),
        Binding("s", "skip", "Skip"),
        Binding("x", "skip_all", "Skip all"),
    ]

    def __init__(self, event: UserQuestionRequired) -> None:
        super().__init__(event.interrupt_id)
        self.event = event
        self._index = 0
        self._selected: dict[str, set[int]] = {}
        self._free_text: dict[str, str] = {}
        self._skipped: set[str] = set()
        self._other_input: Input | None = None

    # -- rendering -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Label("", classes="message-role", id="uq-title")
        yield Static("", markup=False, id="uq-question")
        yield Static("", id="uq-options")
        yield Static("", id="uq-keys", classes="prompt-keys")

    def on_mount(self) -> None:
        self._paint()

    def _paint(self) -> None:
        question = self.event.questions[self._index]
        total = len(self.event.questions)
        self.query_one("#uq-title", Label).update(f"Question {self._index + 1} of {total} · {question.header}")
        self.query_one("#uq-question", Static).update(question.question)
        picked = self._selected.get(question.header, set())
        lines = []
        for n, option in enumerate(question.options, start=1):
            mark = "●" if (n - 1) in picked else "○"
            desc = f"  [dim]{option.description}[/]" if option.description else ""
            lines.append(f"[b]{n}[/] {mark} {option.label}{desc}")
        self.query_one("#uq-options", Static).update("\n".join(lines))
        if question.multi_select:
            keys = "[b]1-9[/] toggle   [b]Enter[/] confirm   [b]o[/] other   [b]s[/] skip   [b]x[/] skip all"
        else:
            keys = "[b]1-9[/] choose   [b]o[/] other   [b]s[/] skip   [b]x[/] skip all"
        self.query_one("#uq-keys", Static).update(keys)

    # -- keys ----------------------------------------------------------------

    async def _on_key(self, event: events.Key) -> None:
        if self._other_input is not None:
            return
        if event.character and event.character.isdigit() and event.character != "0":
            event.stop()
            event.prevent_default()
            self._pick(int(event.character) - 1)

    def _pick(self, option_index: int) -> None:
        question = self.event.questions[self._index]
        if option_index >= len(question.options):
            return
        chosen = self._selected.setdefault(question.header, set())
        if question.multi_select:
            chosen.symmetric_difference_update({option_index})
            self._paint()
            return
        chosen.clear()
        chosen.add(option_index)
        self._advance()

    def action_confirm(self) -> None:
        if self._other_input is not None:
            return
        question = self.event.questions[self._index]
        if question.multi_select:
            self._advance()

    def action_other(self) -> None:
        if self._other_input is not None:
            return
        field = Input(placeholder="Type your answer and press Enter", id="uq-other")
        self._other_input = field
        self.mount(field, after=self.query_one("#uq-options"))
        field.focus()

    def on_input_submitted(self, message: Input.Submitted) -> None:
        message.stop()
        question = self.event.questions[self._index]
        text = message.value.strip()
        if text:
            self._free_text[question.header] = text
        if self._other_input is not None:
            self._other_input.remove()
            self._other_input = None
        self._advance()

    def action_skip(self) -> None:
        if self._other_input is not None:
            return
        self._skipped.add(self.event.questions[self._index].header)
        self._advance()

    def action_skip_all(self) -> None:
        self.post_message(InterruptAnswered(self.interrupt_id, {"skipped": True}, summary="Skipped the agent's questions"))

    # -- progression ---------------------------------------------------------

    def _advance(self) -> None:
        if self._index + 1 < len(self.event.questions):
            self._index += 1
            self.focus()
            self._paint()
            return
        self.post_message(InterruptAnswered(self.interrupt_id, self._response(), summary=self._summary()))

    def _response(self) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for question in self.event.questions:
            if question.header in self._skipped and question.header not in self._free_text:
                answers[question.header] = {"skipped": True}
                continue
            labels = [question.options[i].label for i in sorted(self._selected.get(question.header, set()))]
            entry: dict[str, Any] = {"selected": labels}
            if question.header in self._free_text:
                entry["freeText"] = self._free_text[question.header]
            answers[question.header] = entry
        return {"answers": answers}

    def _summary(self) -> str:
        parts = []
        for question in self.event.questions:
            if question.header in self._free_text:
                parts.append(f"{question.header}: {self._free_text[question.header]}")
            elif question.header in self._skipped:
                parts.append(f"{question.header}: skipped")
            else:
                labels = [question.options[i].label for i in sorted(self._selected.get(question.header, set()))]
                parts.append(f"{question.header}: {', '.join(labels) or 'skipped'}")
        return "Answered · " + " · ".join(parts)


class OAuthPrompt(_Prompt):
    """Sign in to an external service in a browser, then tell the agent to go on.

    The terminal cannot complete the consent itself; it shows the URL
    unwrapped so a copy-paste or a click-to-open picks it up whole.
    """

    BINDINGS = [
        Binding("enter", "done", "Signed in"),
        Binding("y", "done", "Signed in", show=False),
        Binding("n", "decline", "Skip"),
    ]

    def __init__(self, event: OAuthRequired) -> None:
        super().__init__(event.interrupt_id)
        self.event = event

    def compose(self) -> ComposeResult:
        name = self.event.provider_id or "an external service"
        yield Label(f"Sign-in needed · {name}", classes="message-role")
        yield Static(f"A tool needs your permission to use {name}. Open this URL, grant access, then come back:", markup=False)
        yield Static(self.event.authorization_url, markup=False, classes="interrupt-url")
        yield Static("[b]Enter[/] I've signed in   [b]n[/] skip this tool", classes="prompt-keys")

    def action_done(self) -> None:
        self.post_message(InterruptAnswered(self.interrupt_id, "consented", summary=f"✓ Signed in to {self.event.provider_id}"))

    def action_decline(self) -> None:
        self.post_message(InterruptAnswered(self.interrupt_id, "declined", summary=f"✗ Skipped sign-in to {self.event.provider_id}"))


class BrowserLoginPrompt(_Prompt):
    """The agent's browser needs a human to log in; only the web app can show it."""

    BINDINGS = [
        Binding("enter", "skip", "Skip"),
    ]

    def __init__(self, event: BrowserLoginRequired) -> None:
        super().__init__(event.interrupt_id)
        self.event = event

    def compose(self) -> ComposeResult:
        yield Label("Browser login needed", classes="message-role")
        yield Static(
            self.event.message or "The agent's browser reached a login page. Taking over the browser is only possible in the web app.",
            markup=False,
        )
        yield Static("[b]Enter[/] skip and let the agent continue without it", classes="prompt-keys")

    def action_skip(self) -> None:
        self.post_message(InterruptAnswered(self.interrupt_id, {"skipped": True}, summary="Skipped the browser login"))


def _pretty(tool_input: str) -> str:
    try:
        return json.dumps(json.loads(tool_input), indent=2, ensure_ascii=False)[:600]
    except (TypeError, ValueError):
        return tool_input[:600]
