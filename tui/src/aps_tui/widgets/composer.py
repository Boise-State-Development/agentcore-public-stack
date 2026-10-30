"""The prompt composer — a multi-line TextArea where Enter sends."""

from __future__ import annotations

from collections.abc import Callable

from textual import events
from textual.message import Message
from textual.widgets import TextArea

#: Keys that insert a literal newline instead of sending.
#:
#: Three aliases on purpose. Ctrl+J is deliberately *not* among them: in a
#: terminal Ctrl+J is LF and Ctrl+M is CR, so binding it would collide with
#: Enter itself on many emulators. shift+enter needs the Kitty keyboard
#: protocol (Kitty, Ghostty, WezTerm, Warp); alt+enter and ctrl+o work
#: essentially everywhere, so at least one is always available.
NEWLINE_KEYS = frozenset({"alt+enter", "shift+enter", "ctrl+o"})

#: Keys the composer offers to its interceptor before handling them itself.
#: These are the ones a completion popup wants: moving its highlight,
#: accepting a completion, dismissing it.
INTERCEPTABLE_KEYS = frozenset({"up", "down", "tab", "escape", "enter"})

#: ``interceptor(key) -> consumed``. Installed by the screen while the slash
#: command menu is open; returning True means the key went to the menu.
KeyInterceptor = Callable[[str], bool]


class Composer(TextArea):
    """Multi-line prompt input.

    Enter submits, because that is what every chat interface has trained people
    to expect. ``NEWLINE_KEYS`` insert a hard newline for multi-line prompts.
    """

    class Submitted(Message):
        """Posted when the user sends a non-empty prompt."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            soft_wrap=True,
            tab_behavior="focus",
            show_line_numbers=False,
            id="composer",
            **kwargs,  # type: ignore[arg-type]
        )
        #: While False, Enter is swallowed — used to block sends mid-turn.
        self.submit_enabled = True
        #: See ``KeyInterceptor``. None when nothing wants the keys.
        self.key_interceptor: KeyInterceptor | None = None

    async def _on_key(self, event: events.Key) -> None:
        """Intercept Enter before TextArea turns it into a newline.

        ``TextArea._on_key`` maps ``enter`` to ``"\\n"`` and inserts it, so this
        has to run first and stop the event; handling it in a public ``on_key``
        would be too late. The interceptor runs first of all, so an open
        command menu gets Up/Down/Tab/Enter/Esc before the editor does.
        """
        if event.key in INTERCEPTABLE_KEYS and self.key_interceptor is not None and self.key_interceptor(event.key):
            event.prevent_default()
            event.stop()
            return

        if event.key in NEWLINE_KEYS:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return

        if event.key == "enter":
            event.prevent_default()
            event.stop()
            if not self.submit_enabled:
                return
            text = self.text.strip()
            if not text:
                return
            self.post_message(self.Submitted(text))
            return

        await super()._on_key(event)

    def clear_prompt(self) -> None:
        """Empty the composer and park the cursor at the start."""
        self.text = ""
        self.move_cursor((0, 0))

    def replace_prompt(self, text: str) -> None:
        """Set the text and put the cursor at its end (used for completion)."""
        self.text = text
        self.move_cursor(self.document.end)
