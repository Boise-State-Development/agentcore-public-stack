"""The AgentCore chat application.

Streaming design: SSE deltas arrive far faster than a terminal can usefully
repaint, so the worker appends them to a buffer and a timer flushes that buffer
into the Markdown widget every :data:`FLUSH_INTERVAL` seconds. That keeps the
parse/mount cost proportional to elapsed time rather than to token count, and
still reads as live typing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer, Header

from .client import ApiConverseClient, ChatMessage
from .client.events import ErrorEvent, Metadata, ReasoningDelta, TextDelta, TurnAccumulator
from .config import Config, config_path
from .errors import AgentCoreTuiError
from .logging_setup import active_log_path, redact
from .screens import ModelPicker
from .widgets import AssistantMessage, Composer, Notice, StatusBar, UserMessage

logger = logging.getLogger(__name__)

#: How often buffered stream deltas are flushed to the transcript.
FLUSH_INTERVAL = 0.08

ClientFactory = Callable[[Config], ApiConverseClient]

WELCOME = "Ask anything. Enter sends; Alt+Enter (or Ctrl+O) starts a new line."


class ChatApp(App[None]):
    """Terminal chat client for the AgentCore platform."""

    CSS_PATH = "app.tcss"
    TITLE = "AgentCore"

    #: Key that opens the command palette.
    COMMAND_PALETTE_BINDING = "f1"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+n", "new_conversation", "New chat"),
        Binding("f2", "choose_model", "Model"),
        # show=False because the Footer already renders a palette hint.
        Binding("f1", "command_palette", "Palette", show=False, priority=True),
        # priority so it fires while the composer has focus; `check_action`
        # withdraws it when idle so Esc keeps its normal behaviour then.
        Binding("escape", "cancel_turn", "Stop", priority=True),
    ]

    def __init__(self, config: Config, *, client_factory: ClientFactory | None = None) -> None:
        super().__init__()
        self._config = config
        self._client_factory = client_factory or ApiConverseClient
        self._client: ApiConverseClient | None = None
        self._history: list[ChatMessage] = []
        self._pending: AssistantMessage | None = None
        self._accumulator: TurnAccumulator | None = None
        self._text_buffer = ""
        self._reasoning_buffer = ""
        self._flush_timer: Timer | None = None
        self._busy = False

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="transcript")
        with Vertical(id="composer-area"):
            yield Composer()
            yield StatusBar()
        yield Footer()

    @property
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    @property
    def composer(self) -> Composer:
        return self.query_one(Composer)

    @property
    def status(self) -> StatusBar:
        return self.query_one(StatusBar)

    async def on_mount(self) -> None:
        self.status.set_model(self._config.model_id)
        self.sub_title = self._config.base_url or "not configured"

        if not self._config.is_complete:
            await self._show_setup_help()
            return

        if self._config.api_key_from_plaintext_file:
            self.notify(
                f"Your API key is stored in plain text in {config_path()}. Prefer `agentcore-tui login`, which uses the OS keyring.",
                title="Key stored in plain text",
                severity="warning",
                timeout=12,
            )

        self.composer.focus()
        await self.transcript.mount(Notice(WELCOME))
        self.status.set_state("Ready")

    async def _show_setup_help(self) -> None:
        """Explain exactly what is missing and how to supply it."""
        missing = " and ".join(self._config.missing())
        self.composer.submit_enabled = False
        self.composer.disabled = True
        await self.transcript.mount(
            Notice(
                f"Not configured — no {missing}.",
                hint=(
                    "Create an API key in the web app under Settings -> API Keys, then run:\n"
                    "  agentcore-tui login --base-url https://your-host/api\n"
                    f"Config file: {config_path()}"
                ),
                error=True,
            )
        )
        self.status.set_state(f"No {missing}", error=True)

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- actions -------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide Stop unless a turn is actually in flight."""
        if action == "cancel_turn":
            return self._busy
        return True

    def action_cancel_turn(self) -> None:
        """Abandon the in-flight turn."""
        if not self._busy:
            return
        self.workers.cancel_group(self, "turn")
        self._finish_turn("Stopped")
        self.notify("Turn cancelled.", severity="warning")

    async def action_new_conversation(self) -> None:
        """Clear history and transcript, keeping the configured model."""
        if self._busy:
            self.action_cancel_turn()
        self._history.clear()
        await self.transcript.remove_children()
        await self.transcript.mount(Notice(WELCOME))
        self.status.set_turns(0)
        self.status.set_usage(None)
        self.status.set_state("Ready")
        self.composer.focus()

    @work
    async def action_choose_model(self) -> None:
        """Open the model picker and adopt the selection."""
        chosen = await self.push_screen_wait(ModelPicker(self._config.models, self._config.model_id))
        if not chosen or chosen == self._config.model_id:
            return
        self._config = self._config.with_model(chosen)
        # The client caches config, so rebuild it against the new model.
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.status.set_model(chosen)
        self.notify(f"Model set to {chosen}", timeout=5)

    # -- command palette -----------------------------------------------------

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Curate the palette: this app's own actions first, then useful builtins.

        Textual's defaults include "Maximize", which maximizes the *focused*
        widget — always the composer here, so it fills the screen with an input
        box and hides the transcript. Its help panel is also unreadable at chat
        widths, and the footer already lists the bindings. Both are dropped.
        """
        yield SystemCommand("New conversation", "Clear the transcript and start over", self.action_new_conversation)
        yield SystemCommand("Change model", "Pick which model answers the next turn", self.action_choose_model)

        if self._busy:
            yield SystemCommand("Stop response", "Abandon the turn in flight", self.action_cancel_turn)

        if self._last_answer:
            yield SystemCommand("Copy last response", "Copy the most recent answer to the clipboard", self._copy_last_answer)

        yield SystemCommand("Copy transcript", "Copy the whole conversation as Markdown", self._copy_transcript, discover=False)
        yield SystemCommand("Show log file", "Report where diagnostics are being written", self._show_log_path, discover=False)
        yield SystemCommand("Theme", "Change the colour theme", self.action_change_theme)
        yield SystemCommand("Screenshot", "Save an SVG snapshot of the screen", lambda: self.set_timer(0.1, self.deliver_screenshot), discover=False)
        yield SystemCommand("Quit", "Leave the application", self.action_quit)

    @property
    def _last_answer(self) -> str | None:
        for message in reversed(self._history):
            if message.role == "assistant":
                return message.content
        return None

    def _copy_last_answer(self) -> None:
        answer = self._last_answer
        if not answer:
            self.notify("No response to copy yet.", severity="warning")
            return
        self.copy_to_clipboard(answer)
        self.notify(f"Copied {len(answer):,} characters.")

    def _copy_transcript(self) -> None:
        if not self._history:
            self.notify("Nothing to copy yet.", severity="warning")
            return
        rendered = "\n\n".join(f"## {'You' if m.role == 'user' else 'Assistant'}\n\n{m.content}" for m in self._history)
        self.copy_to_clipboard(rendered)
        self.notify(f"Copied {len(self._history)} messages.")

    def _show_log_path(self) -> None:
        path = active_log_path()
        self.notify(str(path) if path else "Logging is not configured.", title="Log file", timeout=15)

    # -- turn lifecycle ------------------------------------------------------

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        if self._busy or not self._config.is_complete:
            return
        self.composer.clear_prompt()
        await self._start_turn(message.text)

    async def _start_turn(self, prompt: str) -> None:
        logger.info("turn start model=%s history_turns=%d prompt_chars=%d", self._config.model_id, len(self._history), len(prompt))
        logger.debug("prompt: %s", redact(prompt))
        self._history.append(ChatMessage(role="user", content=prompt))
        await self.transcript.mount(UserMessage(prompt))

        pending = AssistantMessage(self._config.model_id)
        await self.transcript.mount(pending)
        self._pending = pending
        self._accumulator = TurnAccumulator()
        self._text_buffer = ""
        self._reasoning_buffer = ""

        self._busy = True
        self.composer.submit_enabled = False
        self.composer.add_class("-busy")
        self.status.set_state("Thinking...", busy=True)
        self.refresh_bindings()

        self._flush_timer = self.set_interval(FLUSH_INTERVAL, self._flush_buffers)
        self.transcript.scroll_end(animate=False)
        self._stream_turn()

    def _client_or_create(self) -> ApiConverseClient:
        if self._client is None:
            self._client = self._client_factory(self._config)
        return self._client

    @work(exclusive=True, group="turn")
    async def _stream_turn(self) -> None:
        """Consume the SSE stream for one turn.

        Exceptions are handled here rather than escaping to the worker's default
        error handling, which would tear the app down mid-conversation.
        """
        accumulator = self._accumulator
        if accumulator is None:
            return

        try:
            client = self._client_or_create()
            async for event in client.stream(self._history):
                accumulator.apply(event)
                match event:
                    case TextDelta(text=chunk):
                        self._text_buffer += chunk
                    case ReasoningDelta(text=chunk):
                        self._reasoning_buffer += chunk
                    case Metadata(usage=usage):
                        self.status.set_usage(usage)
                    case ErrorEvent(message=detail):
                        await self._report(detail)
                    case _:
                        pass
        except AgentCoreTuiError as exc:
            logger.warning("turn aborted: %s: %s", type(exc).__name__, exc.message)
            await self._report(exc.message, hint=exc.hint)
            self._finish_turn("Failed", error=True)
            return
        except Exception as exc:  # unexpected: keep the app alive, show the cause
            logger.exception("unexpected error during turn")
            await self._report(f"Unexpected {type(exc).__name__}: {exc}", hint="This is a bug in the client.")
            self._finish_turn("Failed", error=True)
            return

        await self._finalise(accumulator)

    async def _finalise(self, accumulator: TurnAccumulator) -> None:
        """Flush the tail of the stream and record the turn in history."""
        await self._flush_buffers()

        # Layout metrics: if the transcript is not scrollable while holding more
        # content than fits, message widgets are being clipped. That bug shipped
        # once (containers default to height: 1fr) and presented as answers
        # truncating mid-sentence, so it is worth a line in every turn's log.
        transcript = self.transcript
        clipped = transcript.virtual_size.height > transcript.size.height and transcript.max_scroll_y <= 0
        logger.info(
            "turn rendered chars=%d reasoning=%d viewport_h=%d content_h=%d max_scroll_y=%d scroll_y=%.0f%s",
            len(accumulator.text),
            len(accumulator.reasoning),
            transcript.size.height,
            transcript.virtual_size.height,
            transcript.max_scroll_y,
            transcript.scroll_y,
            " CLIPPED" if clipped else "",
        )
        if clipped:
            logger.error("transcript is not scrollable but content exceeds the viewport — message widgets are being clipped")

        if accumulator.error:
            logger.warning("turn failed with stream error: %s", accumulator.error)
            self._finish_turn("Failed", error=True)
            return

        if accumulator.ok:
            self._history.append(ChatMessage(role="assistant", content=accumulator.text))
            self.status.set_turns(sum(1 for message in self._history if message.role == "user"))
        elif not accumulator.text:
            logger.warning("model returned an empty response stop_reason=%s", accumulator.stop_reason)
            await self._report("The model returned an empty response.", hint="Try rephrasing, or switch models with F2.")

        if accumulator.truncated:
            logger.warning("response truncated at max_tokens=%d", self._config.max_tokens)
            await self._report(
                "Response cut off at the token limit.",
                hint=f"Raise `max_tokens` in {config_path()} (currently {self._config.max_tokens:,}).",
            )

        self._finish_turn("Ready")

    async def _flush_buffers(self) -> None:
        """Move buffered deltas into the transcript."""
        if self._pending is None:
            return
        if self._reasoning_buffer:
            chunk, self._reasoning_buffer = self._reasoning_buffer, ""
            await self._pending.append_reasoning(chunk)
        if self._text_buffer:
            chunk, self._text_buffer = self._text_buffer, ""
            await self._pending.append_text(chunk)
            self.status.set_state("Responding...", busy=True)
        self.transcript.scroll_end(animate=False)

    def _finish_turn(self, state: str, *, error: bool = False) -> None:
        """Return the UI to an idle, ready-to-send state."""
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None
        self._busy = False
        self._pending = None
        self._accumulator = None
        self.composer.submit_enabled = True
        self.composer.remove_class("-busy")
        self.status.set_state(state, error=error)
        self.refresh_bindings()
        self.composer.focus()

    async def _report(self, message: str, *, hint: str = "") -> None:
        """Surface a problem inline in the transcript and as a toast."""
        await self.transcript.mount(Notice(message, hint=hint, error=True))
        self.transcript.scroll_end(animate=False)
        self.notify(message, title="Error", severity="error", timeout=10)
