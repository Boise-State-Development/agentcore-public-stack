"""One assistant turn, from prompt to stored message.

Extracted from the App for two reasons. It was half of a 418-line class that
also owned layout, palette commands and startup; and the event handling was a
four-case ``match`` that has to grow to roughly thirty-five when the agent
stream lands.

Two design choices worth knowing:

**The view is a protocol, not a widget.** :class:`TurnSink` is a handful of async
methods, so the whole lifecycle can be driven in a test with a list-appending
fake and no Textual app at all. The screen implements it.

**Dispatch is a registry, not a match statement.** ``_handlers`` maps event type
to handler. Adding an event is one dict entry and one method, and an unhandled
event type is a lookup miss rather than a silently-taken ``case _`` branch.

Buffering is preserved from the original design: SSE deltas arrive far faster
than a terminal can usefully repaint, so text accumulates here and the screen's
interval timer calls :meth:`BaseTurnController.flush`. That keeps render cost
proportional to elapsed time rather than to token count. The timer stays in the
screen because it is Textual's; the buffer stays here because it is state.

**Two dialects, one machinery.** There are two endpoints and they disagree about
more than event names: ``/chat/api-converse`` takes the whole transcript and
returns one LLM call, while ``/chat/stream`` takes one message against a
server-side session and may run several calls and any number of tools. Their
accumulators even disagree about what ``.text`` means — the agent's is the *last*
assistant message, because each tool round trip closes one and opens another,
whereas concatenating is right for converse.

So the shared parts live in :class:`BaseTurnController` — buffering, the busy
flag, ``begin``/``finish``, and the ``stream()`` skeleton that must never raise —
and each dialect gets a subclass that owns only what genuinely differs: which
accumulator to build, which handlers to register, how to open the stream, and
what a finished turn means. The alternative shapes were both worse: one class
with conditionals would grow a branch per difference, and two independent classes
would duplicate the buffer/flush logic, which is the part most likely to drift
unnoticed.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Generic, Protocol, TypeVar

from .client import AgentStreamClient, ApiConverseClient
from .client.agent_events import AgentTurnAccumulator, ToolCallRecord
from .client.agent_events import Compaction as AgentCompaction
from .client.agent_events import ErrorEvent as AgentErrorEvent
from .client.agent_events import Metadata as AgentMetadata
from .client.agent_events import MetadataSummary as AgentMetadataSummary
from .client.agent_events import Reasoning as AgentReasoning
from .client.agent_events import SessionTitle
from .client.agent_events import TextDelta as AgentTextDelta
from .client.agent_events import ToolProgress, ToolResult, ToolUse
from .client.events import ConverseEvent, ErrorEvent, Metadata, MessageStop, ReasoningDelta, TextDelta, TurnAccumulator
from .conversation import ConversationStore, Message
from .errors import AgentCoreTuiError
from .logging_setup import redact
from .usage import Usage

logger = logging.getLogger(__name__)

#: How often the screen should call :meth:`BaseTurnController.flush`.
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

    async def on_tool(self, record: ToolCallRecord) -> None:
        """A tool invocation started, progressed, or finished.

        Called repeatedly for the same ``record``, which the accumulator mutates
        in place. Implementations mount a widget on first sight and refresh it
        afterwards; re-mounting per call would stack duplicates.
        """

    async def on_title(self, title: str) -> None:
        """The server named this conversation. May arrive after the answer."""


ClientSupplier = Callable[[], ApiConverseClient]
AgentClientSupplier = Callable[[], AgentStreamClient]
EventHandler = Callable[[Any], Awaitable[None]]

#: Either dialect's fold. The base controller only ever holds one opaquely.
AccumulatorT = TypeVar("AccumulatorT")


class BaseTurnController(ABC, Generic[AccumulatorT]):
    """Runs one turn at a time against a conversation.

    Owns only in-flight state. Everything that outlives a turn belongs to the
    :class:`~agentcore_tui.conversation.ConversationStore`.
    """

    def __init__(
        self,
        store: ConversationStore,
        sink: TurnSink,
        *,
        max_tokens: int | None = None,
    ) -> None:
        self._store = store
        self._sink = sink
        self._max_tokens = max_tokens

        self._busy = False
        self._accumulator: AccumulatorT | None = None
        self._text_buffer = ""
        self._reasoning_buffer = ""
        self._handlers: dict[type, EventHandler] = self._build_handlers()

    # -- per-dialect hooks ---------------------------------------------------

    @abstractmethod
    def _new_accumulator(self) -> AccumulatorT:
        """A fresh fold for one turn."""

    @abstractmethod
    def _build_handlers(self) -> dict[type, EventHandler]:
        """Event type to handler. Keyed on concrete type, so a new event is
        inert until registered here."""

    @abstractmethod
    def _events(self, prompt: str) -> AsyncIterator[Any]:
        """Open the stream for this turn."""

    @abstractmethod
    async def complete(self, accumulator: AccumulatorT) -> None:
        """Flush the tail, store the answer, and report how the turn ended."""

    async def _interrupt_server(self) -> bool:
        """Tell the server to stop. False when there is nothing to tell.

        Default is a no-op: ``/chat/api-converse`` holds no server-side state,
        so dropping the local stream is the whole of cancelling there.
        """
        return False

    # -- state ---------------------------------------------------------------

    @property
    def busy(self) -> bool:
        """True while a turn is in flight. Public: the UI gates on it."""
        return self._busy

    @property
    def accumulator(self) -> AccumulatorT | None:
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
        self._accumulator = self._new_accumulator()
        self._text_buffer = ""
        self._reasoning_buffer = ""
        self._busy = True
        await self._sink.on_state("Thinking...", busy=True)
        return message

    async def stream(self) -> None:
        """Consume the event stream for the turn started by :meth:`begin`.

        Never raises. A failed turn is a reported, recoverable state — letting
        an exception escape would tear down the app mid-conversation.

        Deliberately no retry, for either dialect. Against the agent a reopen
        re-runs the turn: the prompt is already in memory and tools may have
        executed, so a second attempt double-runs them.
        """
        accumulator = self._accumulator
        if accumulator is None:
            logger.warning("stream() called with no turn in flight")
            return

        prompt = self._store.messages[-1].content if self._store.messages else ""

        try:
            async for event in self._events(prompt):
                self._apply(accumulator, event)
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

    @staticmethod
    def _apply(accumulator: AccumulatorT, event: Any) -> None:
        """Fold one event. Both accumulators expose ``apply``."""
        accumulator.apply(event)  # type: ignore[attr-defined]

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

    async def cancel(self) -> None:
        """Abandon the in-flight turn.

        Tells the server first. Cancelling only the local stream leaves it
        generating, burning tokens, and holding the session lease — which locks
        the user out of their own conversation until it expires.
        """
        if not self._busy:
            return
        logger.info("turn cancelled session=%s", self._store.session_id)
        await self._sink.on_state("Stopping...", busy=True)
        interrupted = await self._interrupt_server()
        if not interrupted:
            logger.debug("no server-side interrupt for this dialect, or it failed")
        await self.finish("Stopped")

    async def finish(self, state: str, *, error: bool = False) -> None:
        """Return to idle. Idempotent, because cancel and completion can race."""
        self._busy = False
        self._accumulator = None
        self._text_buffer = ""
        self._reasoning_buffer = ""
        await self._sink.on_state(state, error=error)

    # -- handlers shared by both dialects ------------------------------------

    async def _handle_text(self, event: Any) -> None:
        self._text_buffer += event.text

    async def _handle_reasoning(self, event: Any) -> None:
        self._reasoning_buffer += event.text


class TurnController(BaseTurnController[TurnAccumulator]):
    """The ``/chat/api-converse`` dialect: one LLM call, no tools, no session."""

    def __init__(
        self,
        store: ConversationStore,
        sink: TurnSink,
        *,
        client_supplier: ClientSupplier,
        max_tokens: int | None = None,
    ) -> None:
        self._client_supplier = client_supplier
        super().__init__(store, sink, max_tokens=max_tokens)

    def _new_accumulator(self) -> TurnAccumulator:
        return TurnAccumulator()

    def _build_handlers(self) -> dict[type, EventHandler]:
        return {
            TextDelta: self._handle_text,
            ReasoningDelta: self._handle_reasoning,
            Metadata: self._handle_metadata,
            MessageStop: self._handle_message_stop,
            ErrorEvent: self._handle_error_event,
        }

    def _events(self, prompt: str) -> AsyncIterator[ConverseEvent]:
        # This endpoint is stateless, so the whole transcript goes every time.
        return self._client_supplier().stream(self._store.messages)

    async def _handle_metadata(self, event: Metadata) -> None:
        await self._sink.on_usage(event.usage)

    async def _handle_message_stop(self, event: MessageStop) -> None:
        logger.debug("message_stop reason=%s", event.stop_reason)

    async def _handle_error_event(self, event: ErrorEvent) -> None:
        await self._sink.on_notice(event.message, error=True)

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
            await self._warn_truncated()

        await self.finish("Ready")

    async def _warn_truncated(self) -> None:
        limit = f" (currently {self._max_tokens:,})" if self._max_tokens else ""
        logger.warning("response truncated at max_tokens=%s", self._max_tokens)
        await self._sink.on_notice(
            "Response cut off at the token limit.",
            hint=f"Raise `max_tokens` in your config file{limit}.",
            error=True,
        )


class AgentTurnController(BaseTurnController[AgentTurnAccumulator]):
    """The ``/chat/stream`` dialect: the tool-using agent.

    Differences from its sibling that are not just event names:

    * the prompt is sent alone, against ``store.session_id``, because history
      lives in AgentCore Memory rather than in the request;
    * a turn can *pause* rather than finish, when a tool needs OAuth consent or
      approval. That is not a failure, and reporting it as one would tell the
      user their turn broke when it is waiting for them;
    * a turn can be *blocked* by quota before it runs at all, in which case
      there is no assistant message to store;
    * cancelling must reach the server.
    """

    def __init__(
        self,
        store: ConversationStore,
        sink: TurnSink,
        *,
        client_supplier: AgentClientSupplier,
        max_tokens: int | None = None,
        enabled_tools: Sequence[str] | None = None,
    ) -> None:
        self._client_supplier = client_supplier
        # None means "every tool my role grants"; [] means "none". Passed
        # through untouched — see AgentStreamClient._payload.
        self._enabled_tools = enabled_tools
        # The title can arrive as an event *or* only be visible on the finished
        # accumulator, and `complete()` checks the latter. Tracking it here stops
        # a turn that got the event from reporting the same title twice.
        self._title_reported = False
        super().__init__(store, sink, max_tokens=max_tokens)

    def _new_accumulator(self) -> AgentTurnAccumulator:
        self._title_reported = False
        return AgentTurnAccumulator()

    def _build_handlers(self) -> dict[type, EventHandler]:
        return {
            AgentTextDelta: self._handle_text,
            AgentReasoning: self._handle_reasoning,
            ToolUse: self._handle_tool,
            ToolResult: self._handle_tool,
            ToolProgress: self._handle_tool,
            AgentMetadata: self._handle_metadata,
            AgentMetadataSummary: self._handle_metadata_summary,
            SessionTitle: self._handle_title,
            AgentCompaction: self._handle_compaction,
            AgentErrorEvent: self._handle_error_event,
        }

    def _events(self, prompt: str) -> AsyncIterator[Any]:
        return self._client_supplier().stream(
            session_id=self._store.session_id,
            message=prompt,
            enabled_tools=self._enabled_tools,
        )

    async def _interrupt_server(self) -> bool:
        """``POST /sessions/{id}/interrupt`` — the real carrier of stop intent."""
        return await self._client_supplier().interrupt(self._store.session_id)

    # -- handlers ------------------------------------------------------------

    async def _handle_tool(self, event: Any) -> None:
        """Report the *record*, not the event.

        The accumulator has already folded this event into a mutable
        ``ToolCallRecord``, and the widget layer holds that same object. Handing
        the sink the record rather than the event means there is no second copy
        of the state to drift.
        """
        accumulator = self._accumulator
        if accumulator is None:
            return
        tool_use_id = getattr(event, "tool_use_id", "")
        for record in accumulator.tool_calls:
            if record.tool_use_id == tool_use_id:
                # Deltas are buffered, but tool state is not: a tool call is one
                # event, not hundreds, and seeing it appear promptly is the whole
                # point of showing it.
                await self.flush()
                await self._sink.on_tool(record)
                await self._sink.on_state(f"Running {record.name or 'tool'}...", busy=True)
                return

    async def _handle_metadata(self, event: AgentMetadata) -> None:
        # Per LLM call: a tool-using turn emits several, and the last one is the
        # current context size rather than the turn's total. Correct for a
        # context gauge, wrong as a "tokens billed" figure.
        await self._sink.on_usage(event.usage)

    async def _handle_metadata_summary(self, event: AgentMetadataSummary) -> None:
        # Unreachable against today's server, which swallows this event on
        # purpose. Registered so a future server that does send one is handled
        # rather than logged as unknown.
        await self._sink.on_usage(event.usage)

    async def _handle_title(self, event: SessionTitle) -> None:
        self._title_reported = True
        await self._sink.on_title(event.title)

    async def _handle_compaction(self, event: AgentCompaction) -> None:
        logger.info("compaction summarized=%s", event.summarized_turns)

    async def _handle_error_event(self, event: AgentErrorEvent) -> None:
        await self._sink.on_notice(event.message, error=True)

    # -- finishing -----------------------------------------------------------

    async def complete(self, accumulator: AgentTurnAccumulator) -> None:  # noqa: C901 - one branch per way a turn ends
        """Flush the tail, store the answer, and report how the turn ended."""
        await self.flush()

        # Only when the event was not already seen: a title may arrive mid-turn
        # or only be readable off the finished accumulator, and reporting both
        # would set it twice.
        if accumulator.title and not self._title_reported:
            await self._sink.on_title(accumulator.title)

        if accumulator.error:
            logger.warning("turn failed with stream error: %s", accumulator.error)
            await self.finish("Failed", error=True)
            return

        # Quota first: the turn never ran, so there is no answer and no point
        # complaining about an empty response.
        if accumulator.blocked:
            logger.warning("turn blocked by quota")
            await self._sink.on_notice(
                "This turn was blocked by your usage quota.",
                hint="Quotas reset on a schedule set by your administrator. Check Settings in the web app.",
                error=True,
            )
            await self.finish("Quota exceeded", error=True)
            return

        # A paused turn is waiting for the user, not broken. Store whatever the
        # model said before pausing so the transcript is not silently lossy.
        if accumulator.interrupted:
            logger.info("turn paused awaiting consent or approval")
            if accumulator.text:
                self._store.append_assistant(accumulator.text, reasoning=accumulator.reasoning, usage=accumulator.usage)
            await self._sink.on_notice(
                "This turn is paused: a tool needs your approval or a sign-in.",
                hint="Continue this conversation in the web app — the terminal cannot present the consent screen.",
            )
            await self.finish("Paused")
            return

        if accumulator.ok:
            self._store.append_assistant(
                accumulator.text,
                reasoning=accumulator.reasoning,
                usage=accumulator.usage,
            )
        elif not accumulator.text:
            logger.warning("agent returned an empty response stop_reason=%s", accumulator.stop_reason)
            await self._sink.on_notice(
                "The agent returned an empty response.",
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

        for citation_count in ([len(accumulator.citations)] if accumulator.citations else []):
            # Batched deliberately: one notice per turn, not one per citation.
            names = ", ".join(sorted({c.file_name for c in accumulator.citations if c.file_name}))
            await self._sink.on_notice(
                f"Answered using {citation_count} knowledge-base excerpt(s).",
                hint=names or "",
            )

        for artifact in accumulator.artifacts:
            await self._sink.on_notice(
                f"Created an artifact: {artifact.title or artifact.artifact_id}",
                hint="Open it in the web app; a terminal cannot render it.",
            )

        await self.finish("Ready")
