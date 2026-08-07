"""One assistant turn, from prompt to stored message.

Extracted from the App for two reasons. It was half of a 418-line class that
also owned layout, palette commands and startup; and the event handling was a
four-case ``match`` that has to grow to roughly thirty-five when the agent
stream lands.

Two design choices worth knowing:

**The view is a protocol, not a widget.** :class:`TurnSink` is five sync
methods, so the whole lifecycle can be driven in a test with a list-appending
fake and no Textual app at all. The screen implements it.

**Dispatch is a registry, not a match statement.** :attr:`TurnController._handlers`
maps event type to handler. Adding an event is one dict entry and one method,
and an unhandled event type is a lookup miss rather than a silently-taken
``case _`` branch.

Buffering is preserved from the original design: SSE deltas arrive far faster
than a terminal can usefully repaint, so text accumulates here and the screen's
interval timer calls :meth:`flush`. That keeps render cost proportional to
elapsed time rather than to token count. The timer stays in the screen because
it is Textual's; the buffer stays here because it is state.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .client import ApiConverseClient
from .client.events import ConverseEvent, ErrorEvent, Metadata, MessageStop, ReasoningDelta, TextDelta, TurnAccumulator
from .conversation import ConversationStore, Message
from .errors import AgentCoreTuiError
from .logging_setup import redact
from .usage import Usage

logger = logging.getLogger(__name__)

#: How often the screen should call :meth:`TurnController.flush`.
FLUSH_INTERVAL = 0.08


class TurnSink(Protocol):
    """What a turn needs from its view.

    Uniformly async. Two of these unavoidably await — appending to a Textual
    ``Markdown`` widget is a coroutine — and a protocol where some methods are
    awaited and others are not is a trap for every future implementer.
    """

    async def on_text(self, chunk: str) -> None:
        """Append assistant prose to the in-flight message."""

    async def on_reasoning(self, chunk: str) -> None:
        """Append extended-thinking content to the in-flight message."""

    async def on_usage(self, usage: Usage | None) -> None:
        """Report token counts for the turn."""

    async def on_state(self, state: str, *, busy: bool = False, error: bool = False) -> None:
        """Report a lifecycle label such as ``Thinking...`` or ``Ready``."""

    async def on_notice(self, message: str, *, hint: str = "", error: bool = False) -> None:
        """Surface a message that is not model output — an error or a warning."""


ClientSupplier = Callable[[], ApiConverseClient]
EventHandler = Callable[[Any], Awaitable[None]]


class TurnController:
    """Runs one turn at a time against a conversation.

    Owns only in-flight state. Everything that outlives a turn belongs to the
    :class:`~agentcore_tui.conversation.ConversationStore`.
    """

    def __init__(
        self,
        store: ConversationStore,
        sink: TurnSink,
        *,
        client_supplier: ClientSupplier,
        max_tokens: int | None = None,
    ) -> None:
        self._store = store
        self._sink = sink
        self._client_supplier = client_supplier
        self._max_tokens = max_tokens

        self._busy = False
        self._accumulator: TurnAccumulator | None = None
        self._text_buffer = ""
        self._reasoning_buffer = ""

        # Registry rather than a match statement: this is the list that grows to
        # ~35 entries for the agent dialect. Keyed on concrete type, so a new
        # event is inert until it is registered here.
        self._handlers: dict[type[ConverseEvent], EventHandler] = {
            TextDelta: self._handle_text,
            ReasoningDelta: self._handle_reasoning,
            Metadata: self._handle_metadata,
            MessageStop: self._handle_message_stop,
            ErrorEvent: self._handle_error_event,
        }

    # -- state ---------------------------------------------------------------

    @property
    def busy(self) -> bool:
        """True while a turn is in flight. Public: the UI gates on it."""
        return self._busy

    @property
    def accumulator(self) -> TurnAccumulator | None:
        """The fold for the in-flight turn, or None when idle."""
        return self._accumulator

    # -- running a turn ------------------------------------------------------

    async def begin(self, prompt: str) -> Message:
        """Record the user's message and enter the busy state.

        Separate from :meth:`stream` so the screen can mount widgets between
        the two without the request having already started.
        """
        logger.info(
            "turn start turns=%d prompt_chars=%d session=%s",
            self._store.turns,
            len(prompt),
            self._store.session_id,
        )
        logger.debug("prompt: %s", redact(prompt))

        message = self._store.append_user(prompt)
        self._accumulator = TurnAccumulator()
        self._text_buffer = ""
        self._reasoning_buffer = ""
        self._busy = True
        await self._sink.on_state("Thinking...", busy=True)
        return message

    async def stream(self) -> None:
        """Consume the event stream for the turn started by :meth:`begin`.

        Never raises. A failed turn is a reported, recoverable state — letting
        an exception escape would tear down the app mid-conversation.
        """
        accumulator = self._accumulator
        if accumulator is None:
            logger.warning("stream() called with no turn in flight")
            return

        try:
            client = self._client_supplier()
            async for event in client.stream(self._store.messages):
                accumulator.apply(event)
                handler = self._handlers.get(type(event))
                if handler is not None:
                    await handler(event)
        except AgentCoreTuiError as exc:
            logger.warning("turn aborted: %s: %s", type(exc).__name__, exc.message)
            await self._sink.on_notice(exc.message, hint=exc.hint, error=True)
            await self.finish("Failed", error=True)
            return
        except Exception as exc:  # unexpected: keep the app alive, show the cause
            logger.exception("unexpected error during turn")
            await self._sink.on_notice(f"Unexpected {type(exc).__name__}: {exc}", hint="This is a bug in the client.", error=True)
            await self.finish("Failed", error=True)
            return

        await self.complete(accumulator)

    # -- event handlers ------------------------------------------------------

    async def _handle_text(self, event: TextDelta) -> None:
        self._text_buffer += event.text

    async def _handle_reasoning(self, event: ReasoningDelta) -> None:
        self._reasoning_buffer += event.text

    async def _handle_metadata(self, event: Metadata) -> None:
        # Note for the agent dialect: `metadata` fires per LLM call, so a
        # tool-using turn emits several. Only the cumulative summary is a valid
        # whole-turn total there.
        await self._sink.on_usage(event.usage)

    async def _handle_message_stop(self, event: MessageStop) -> None:
        logger.debug("message_stop reason=%s", event.stop_reason)

    async def _handle_error_event(self, event: ErrorEvent) -> None:
        await self._sink.on_notice(event.message, error=True)

    # -- draining ------------------------------------------------------------

    async def flush(self) -> bool:
        """Push buffered deltas to the sink. True if anything was written."""
        wrote = False
        if self._reasoning_buffer:
            chunk, self._reasoning_buffer = self._reasoning_buffer, ""
            await self._sink.on_reasoning(chunk)
            wrote = True
        if self._text_buffer:
            chunk, self._text_buffer = self._text_buffer, ""
            await self._sink.on_text(chunk)
            await self._sink.on_state("Responding...", busy=True)
            wrote = True
        return wrote

    # -- finishing -----------------------------------------------------------

    async def complete(self, accumulator: TurnAccumulator) -> None:
        """Flush the tail, store the answer, and report how the turn ended."""
        await self.flush()

        if accumulator.error:
            logger.warning("turn failed with stream error: %s", accumulator.error)
            await self.finish("Failed", error=True)
            return

        if accumulator.ok:
            self._store.append_assistant(
                accumulator.text,
                reasoning=accumulator.reasoning,
                usage=accumulator.usage,
            )
        elif not accumulator.text:
            logger.warning("model returned an empty response stop_reason=%s", accumulator.stop_reason)
            await self._sink.on_notice(
                "The model returned an empty response.",
                hint="Try rephrasing, or switch models with F2.",
                error=True,
            )

        if accumulator.truncated:
            limit = f" (currently {self._max_tokens:,})" if self._max_tokens else ""
            logger.warning("response truncated at max_tokens=%s", self._max_tokens)
            await self._sink.on_notice(
                "Response cut off at the token limit.",
                hint=f"Raise `max_tokens` in your config file{limit}.",
                error=True,
            )

        await self.finish("Ready")

    async def cancel(self) -> None:
        """Abandon the in-flight turn.

        Local only. Against the agent endpoint this must also
        ``POST /sessions/{id}/interrupt`` — that request is the authoritative
        carrier of stop intent, and without it the server keeps generating and
        holds the session lease, locking the user out of their own conversation.
        The call belongs here, not in the screen.
        """
        if not self._busy:
            return
        logger.info("turn cancelled session=%s", self._store.session_id)
        await self.finish("Stopped")

    async def finish(self, state: str, *, error: bool = False) -> None:
        """Return to idle. Idempotent, because cancel and completion can race."""
        self._busy = False
        self._accumulator = None
        self._text_buffer = ""
        self._reasoning_buffer = ""
        await self._sink.on_state(state, error=error)
