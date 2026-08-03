"""Transcript widgets: user turns, streaming assistant turns, inline notices."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Collapsible, Label, Markdown, Static


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
        """Trim a Bedrock model ID down to something readable in a label.

        ``us.anthropic.claude-haiku-4-5-20251001-v1:0`` -> ``claude-haiku-4-5``
        """
        name = self._model_id.rsplit(".", 1)[-1]
        parts = name.split("-")
        keep: list[str] = []
        for part in parts:
            # Stop at the date stamp / version suffix.
            if part.isdigit() and len(part) == 8:
                break
            if part.startswith("v") and part[1:].split(":")[0].isdigit():
                break
            keep.append(part)
        return "-".join(keep) or name

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
