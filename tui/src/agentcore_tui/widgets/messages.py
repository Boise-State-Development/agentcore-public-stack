"""Transcript widgets: user turns, streaming assistant turns, inline notices."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Collapsible, Label, Markdown, Static

from .status import short_model


class UserMessage(Vertical):
    """A single user turn.

    Rendered as plain text rather than Markdown: echoing the user's own input
    through a Markdown parser would mangle pasted code and stray asterisks.
    """

    def __init__(self, text: str) -> None:
        super().__init__(classes="message user-message")
        self._text = text

    def compose(self) -> ComposeResult:
        yield Label("You", classes="message-role")
        # markup=False so a prompt containing square brackets is not parsed as
        # Textual console markup.
        yield Static(self._text, markup=False)


class AssistantMessage(Vertical):
    """An assistant turn whose body is appended to as the stream arrives.

    Extended-thinking output goes in a collapsed pane above the answer so it is
    available without dominating the transcript.
    """

    def __init__(self, model_id: str) -> None:
        super().__init__(classes="message assistant-message")
        self._model_id = model_id
        self._body: Markdown | None = None
        self._reasoning_body: Markdown | None = None
        self._reasoning_pane: Collapsible | None = None

    def compose(self) -> ComposeResult:
        yield Label(self._short_model_name(), classes="message-role")
        reasoning_body = Markdown()
        self._reasoning_body = reasoning_body
        pane = Collapsible(reasoning_body, title="Reasoning", collapsed=True, classes="reasoning")
        # Hidden until the model actually emits reasoning content.
        pane.display = False
        self._reasoning_pane = pane
        yield pane
        body = Markdown()
        self._body = body
        yield body

    def _short_model_name(self) -> str:
        """The model's display name, trimmed of its region and version stamp."""
        return short_model(self._model_id)

    async def append_text(self, chunk: str) -> None:
        """Append a chunk of answer markdown."""
        if self._body is not None and chunk:
            await self._body.append(chunk)

    async def append_reasoning(self, chunk: str) -> None:
        """Append a chunk of reasoning, revealing the pane on first content."""
        if self._reasoning_body is None or not chunk:
            return
        if self._reasoning_pane is not None and not self._reasoning_pane.display:
            self._reasoning_pane.display = True
        await self._reasoning_body.append(chunk)

    async def set_text(self, markdown: str) -> None:
        """Replace the whole body — used for the non-streaming path."""
        if self._body is not None:
            await self._body.update(markdown)


class Notice(Vertical):
    """An inline transcript notice: an error, or a piece of guidance."""

    def __init__(self, message: str, *, hint: str = "", error: bool = False) -> None:
        classes = "notice -error" if error else "notice"
        super().__init__(classes=classes)
        self._message = message
        self._hint = hint

    def compose(self) -> ComposeResult:
        yield Static(self._message, markup=False)
        if self._hint:
            yield Static(self._hint, classes="notice-hint", markup=False)


class EmptyState(Vertical):
    """What an empty conversation shows: a wordmark, one line of intent, the keys.

    Replaces a welcome *notice*. A notice has a coloured rule and sits in the
    transcript's flow, so the first thing a new user saw was something that
    looked like a warning. This is centred, quiet, and removed by the first
    message.
    """

    def __init__(self, *, agent: bool) -> None:
        super().__init__(id="empty-state")
        self._agent = agent

    def compose(self) -> ComposeResult:
        yield Static("AgentCore", classes="wordmark", markup=False)
        if self._agent:
            yield Static("Ask anything — tools, memory and your saved conversations are here.", classes="tagline", markup=False)
        else:
            yield Static("Ask anything.", classes="tagline", markup=False)
        yield Static(
            "[$primary]Enter[/] send   [$primary]Alt+Enter[/] new line   [$primary]F2[/] model   [$primary]F3[/] tools   [$primary]F4[/] chats   [$primary]F1[/] palette",
            classes="hints",
        )
