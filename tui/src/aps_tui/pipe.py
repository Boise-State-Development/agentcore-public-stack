"""Pipe mode: one turn from the shell, answer on stdout, no Textual app.

``aps-tui -p "prompt"`` (or ``cat file | aps-tui -p "prompt"``) runs a single
turn through the same controllers the chat screen uses, writes the answer to
stdout as it streams, and exits. Everything that is not the answer (tool
progress, notices, errors) goes to stderr, so ``> out.md`` captures only the
answer.

Rules that make it safe to script:

* **Nothing waits for a person.** No local tools are offered, so the turn
  cannot pause on a client tool call. Any other pause (server tool approval,
  OAuth consent, a question, a browser login) ends the run with
  :data:`EXIT_PAUSED`. The server keeps the paused turn, and opening the
  conversation from ``/conversations`` in the TUI answers it.
* **A new conversation every run.** Sessions are created server-side and show
  up in ``/conversations`` and the web app like any other.
* **Cancelling reaches the server.** Ctrl+C and a closed stdout both go
  through :meth:`BaseTurnController.cancel`, which POSTs the interrupt, so an
  abandoned turn does not keep generating and holding the session lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, TextIO

from .client import AgentStreamClient, ApiConverseClient
from .client.agent_events import (
    BrowserLoginRequired,
    ClientToolCall,
    OAuthRequired,
    ToolApprovalRequired,
    ToolCallRecord,
    UserQuestionRequired,
)
from .client.auth import SessionAuth
from .config import Config, load_session_from_keyring
from .conversation import ConversationStore
from .credentials import CredentialSource
from .errors import ConfigError
from .turn import (
    FLUSH_INTERVAL,
    AgentClientSupplier,
    AgentTurnController,
    BaseTurnController,
    ClientSupplier,
    InterruptEvent,
    TurnController,
)
from .usage import Usage

logger = logging.getLogger(__name__)

#: The answer was written in full.
EXIT_OK = 0
#: The turn failed: a transport error, a server error event, or a quota block.
EXIT_FAILED = 1
#: Nothing was sent: no prompt, a bad flag combination, or no configuration.
EXIT_USAGE = 2
#: The turn is paused on the server, waiting for an answer only a person can give.
EXIT_PAUSED = 3
#: Stdout closed before the answer finished (``| head``). 128 + SIGPIPE, as a
#: shell reports a process killed by a broken pipe.
EXIT_BROKEN_PIPE = 141


def compose_prompt(argument: str | None, piped: str | None) -> str:
    """The prompt to send: the argument, then whatever came in on stdin.

    Either may be absent. Both together read as an instruction followed by the
    material it is about, which is how ``cat log | aps-tui -p "why?"`` is
    meant: the argument is placed first so the model reads the question
    before the material.
    """
    parts = [part.strip("\n") for part in (argument, piped) if part and part.strip()]
    return "\n\n".join(parts)


class PipeSink:
    """A :class:`~aps_tui.turn.TurnSink` that writes to two streams.

    The answer goes to ``out`` and nothing else does. Reasoning, usage, titles
    and timings are dropped: they are chat-screen chrome, and putting them on
    stdout would corrupt the answer for whatever reads it.
    """

    def __init__(self, out: TextIO, err: TextIO) -> None:
        self._out = out
        self._err = err
        self._wrote_text = False
        self._ends_with_newline = True
        self._seen_tools: set[str] = set()
        #: Set when the turn ended in a reported error state.
        self.failed = False
        #: Set when ``out`` is closed (``| head``); the driver then cancels.
        self.broken = False
        #: Every interrupt the turn paused on, in arrival order.
        self.interrupts: list[InterruptEvent] = []

    # -- answer --------------------------------------------------------------

    async def on_text(self, chunk: str) -> None:
        if self.broken or not chunk:
            return
        try:
            self._out.write(chunk)
            self._out.flush()
        except BrokenPipeError:
            self.broken = True
            return
        self._wrote_text = True
        self._ends_with_newline = chunk.endswith("\n")

    def end(self) -> None:
        """Finish the answer with a newline, so the shell prompt starts clean."""
        if self.broken or not self._wrote_text or self._ends_with_newline:
            return
        try:
            self._out.write("\n")
            self._out.flush()
        except BrokenPipeError:
            self.broken = True

    # -- everything else goes to stderr --------------------------------------

    def _say(self, text: str) -> None:
        # stderr is for a person; if it is gone too there is no one to tell.
        with contextlib.suppress(OSError, ValueError):
            print(text, file=self._err, flush=True)

    async def on_notice(self, message: str, *, hint: str = "", error: bool = False) -> None:
        self._say(f"{'error' if error else 'note'}: {message}")
        if hint:
            self._say(f"  {hint}")

    async def on_tool(self, record: ToolCallRecord) -> None:
        # Once per call: the record is re-reported on every progress event.
        if record.tool_use_id in self._seen_tools:
            return
        self._seen_tools.add(record.tool_use_id)
        self._say(f"tool: {record.name or 'tool'}")

    async def on_truncated(self) -> None:
        self._say("note: the answer stopped at the token limit.")
        self._say("  Open the conversation in aps-tui and run /continue to let it carry on.")

    async def on_state(self, state: str, *, busy: bool = False, error: bool = False) -> None:
        if error:
            self.failed = True

    async def on_interrupt(self, event: InterruptEvent) -> None:
        self.interrupts.append(event)

    # -- chat-screen chrome, not wanted here ---------------------------------

    async def on_reasoning(self, chunk: str) -> None:
        return None

    async def on_usage(self, usage: Usage | None) -> None:
        return None

    async def on_title(self, title: str) -> None:
        return None

    async def on_context(self, used_tokens: int | None, window: int | None) -> None:
        return None

    async def on_elapsed(self, seconds: float | None) -> None:
        return None


def describe_interrupt(event: InterruptEvent) -> str:
    """What the paused turn is waiting for, in one line."""
    if isinstance(event, ToolApprovalRequired):
        return f"approval to run {event.tool_name or 'a tool'}"
    if isinstance(event, OAuthRequired):
        return f"you to connect your {event.provider_id or 'account'} account"
    if isinstance(event, UserQuestionRequired):
        return "an answer to a question it asked you"
    if isinstance(event, BrowserLoginRequired):
        return "you to sign in to a site in its browser"
    if isinstance(event, ClientToolCall):
        # Unreachable while pipe mode offers no local tools; named so a future
        # change that offers them fails loudly here rather than vaguely.
        return f"this machine to run {event.tool_name or 'a local tool'}"
    return "an answer from you"


def _sealed_session(base_url: str) -> str:
    sealed, unavailable = load_session_from_keyring(base_url)
    if not sealed:
        hint = "Run `aps-tui login --sso`."
        raise ConfigError("No stored session", hint=f"{hint} ({unavailable})" if unavailable else hint)
    return sealed


async def run_pipe(
    config: Config,
    prompt: str,
    *,
    out: TextIO,
    err: TextIO,
    agent_client_supplier: AgentClientSupplier | None = None,
    client_supplier: ClientSupplier | None = None,
) -> int:
    """Run one turn and return the process exit code.

    The dialect is chosen as the chat screen chooses it: a session reaches the
    agent over ``/chat/stream``, an API key reaches ``/chat/api-converse``.
    Either supplier can be injected, which is how the tests run without a
    socket or a credential.
    """
    sink = PipeSink(out, err)
    store = ConversationStore()
    agent_mode = agent_client_supplier is not None or (client_supplier is None and config.credential_source is CredentialSource.BFF_SESSION)
    owned: AgentStreamClient | ApiConverseClient | None = None
    turn: BaseTurnController[Any]

    if agent_mode:
        if agent_client_supplier is None:
            agent_client = AgentStreamClient(config, auth=SessionAuth(_sealed_session(config.base_url)))
            owned = agent_client
            agent_client_supplier = lambda: agent_client  # noqa: E731 - one closed-over client
        # No client tools: nothing here can approve one, and an unapproved
        # local tool is not something to run.
        turn = AgentTurnController(store, sink, client_supplier=agent_client_supplier, max_tokens=config.max_tokens, client_tools=None)
    else:
        if client_supplier is None:
            converse_client = ApiConverseClient(config)
            owned = converse_client
            client_supplier = lambda: converse_client  # noqa: E731 - one closed-over client
        turn = TurnController(store, sink, client_supplier=client_supplier, max_tokens=config.max_tokens)

    logger.info("pipe turn start agent=%s session=%s", agent_mode, store.session_id)
    try:
        return await _drive(turn, store, sink, prompt, err)
    finally:
        if owned is not None:
            await owned.aclose()


async def _drive(turn: BaseTurnController[Any], store: ConversationStore, sink: PipeSink, prompt: str, err: TextIO) -> int:
    await turn.begin(prompt)
    stream_task = asyncio.create_task(turn.stream())
    try:
        # The chat screen flushes on a timer; so does this. Writing per delta
        # would cost a syscall per token for no visible difference.
        while not stream_task.done():
            await asyncio.wait({stream_task}, timeout=FLUSH_INTERVAL)
            await turn.flush()
            if sink.broken:
                logger.info("pipe stdout closed; cancelling turn session=%s", store.session_id)
                await _stop(turn, stream_task)
                return EXIT_BROKEN_PIPE
    except asyncio.CancelledError:
        # Ctrl+C: asyncio.run cancels this task. Stop the server-side turn too,
        # then let the cancellation finish so the runner raises KeyboardInterrupt.
        await _stop(turn, stream_task)
        raise

    await stream_task  # never raises: stream() reports failures through the sink
    sink.end()
    if sink.broken:
        return EXIT_BROKEN_PIPE

    if isinstance(turn, AgentTurnController) and turn.paused:
        waiting = "; ".join(describe_interrupt(event) for event in sink.interrupts) or "an answer from you"
        print(f"paused: the agent is waiting for {waiting}.", file=err)
        print(f"  Open conversation {store.session_id} with /conversations in aps-tui to answer and finish it.", file=err)
        return EXIT_PAUSED

    return EXIT_FAILED if sink.failed else EXIT_OK


async def _stop(turn: BaseTurnController[Any], stream_task: asyncio.Task[None]) -> None:
    stream_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stream_task
    await turn.cancel()
