"""The chat screen.

Previously this was ``ChatApp.compose`` plus most of a 418-line App. Making it a
real :class:`~textual.screen.Screen` is what allows a second screen to exist at
all: a conversation list, a history browser and an assistant preview each need
their own layout while sharing the conversation store.

The screen owns the *view* and nothing else. Conversation contents live in
:class:`~agentcore_tui.conversation.ConversationStore`; the in-flight turn lives
in :class:`~agentcore_tui.turn.TurnController`. This class implements
:class:`~agentcore_tui.turn.TurnSink`, so the controller drives it without
knowing what a widget is.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, ClassVar

from textual import work
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer, Header

from ..client import AgentStreamClient, ApiConverseClient
from ..client.agent_events import ToolCallRecord
from ..client.auth import SessionAuth
from ..config import Config, config_path, load_session_from_keyring
from ..conversation import ConversationStore
from ..credentials import CredentialSource
from ..errors import ConfigError
from ..turn import FLUSH_INTERVAL, AgentClientSupplier, AgentTurnController, BaseTurnController, ClientSupplier, TurnController
from ..usage import Usage
from ..widgets import AssistantMessage, Composer, Notice, StatusBar, ToolCall, UserMessage
from .model_picker import ModelPicker

logger = logging.getLogger(__name__)

WELCOME = "Ask anything. Enter sends; Alt+Enter (or Ctrl+O) starts a new line."

#: Shown instead of WELCOME when the agent is reachable, because the difference
#: is the whole point of signing in.
WELCOME_AGENT = "Ask anything — tools, memory and your saved conversations are available. Enter sends; Alt+Enter starts a new line."


class ChatScreen(Screen[None]):
    """Streaming chat against one conversation."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+n", "new_conversation", "New chat"),
        Binding("f2", "choose_model", "Model"),
        # priority so it fires while the composer has focus; `check_action`
        # withdraws it when idle so Esc keeps its normal behaviour then.
        Binding("escape", "cancel_turn", "Stop", priority=True),
    ]

    def __init__(
        self,
        config: Config,
        *,
        store: ConversationStore | None = None,
        client_supplier: ClientSupplier | None = None,
        agent_client_supplier: AgentClientSupplier | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        # `store if store is not None` rather than `store or ...`: the store
        # defines __len__, so an empty one is falsy and `or` would silently
        # replace the App's shared store with a private copy on every launch.
        self._store = store if store is not None else ConversationStore()
        self._client: ApiConverseClient | AgentStreamClient | None = None
        self._supplied_client = client_supplier
        self._pending: AssistantMessage | None = None
        self._flush_timer: Timer | None = None
        self._tool_widgets: dict[str, ToolCall] = {}

        # Which controller depends on which credential is held, because the two
        # endpoints accept different ones and neither accepts both. A session
        # reaches the agent; an API key reaches only the stateless Converse
        # wrapper. Either supplier can be injected, so tests drive both dialects
        # without a credential or a keyring.
        self._agent_mode = agent_client_supplier is not None or (client_supplier is None and config.credential_source is CredentialSource.BFF_SESSION)
        self._turn: BaseTurnController[Any]
        if self._agent_mode:
            self._turn = AgentTurnController(
                self._store,
                self,
                client_supplier=agent_client_supplier or self._agent_client_or_create,
                max_tokens=config.max_tokens,
            )
        else:
            self._turn = TurnController(
                self._store,
                self,
                client_supplier=client_supplier or self._client_or_create,
                max_tokens=config.max_tokens,
            )

    # -- accessors -----------------------------------------------------------

    @property
    def store(self) -> ConversationStore:
        """The conversation. Public so other screens can share it."""
        return self._store

    @property
    def turn(self) -> BaseTurnController[Any]:
        """The turn controller. Public so tests need no private access."""
        return self._turn

    @property
    def config(self) -> Config:
        return self._config

    @property
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    @property
    def composer(self) -> Composer:
        return self.query_one(Composer)

    @property
    def status(self) -> StatusBar:
        return self.query_one(StatusBar)

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="transcript")
        with Vertical(id="composer-area"):
            yield Composer()
            yield StatusBar()
        yield Footer()

    async def on_mount(self) -> None:
        self.status.set_model(self._config.model_id)

        if not self._config.is_complete:
            await self._show_setup_help()
            return

        # `is_complete` only says a credential exists. Whether *this screen* can
        # chat is a narrower question: the API-key transport needs a key, and
        # asking it later produced a screen that said "Ready" and then failed on
        # the first message telling the user to run a login they had already run.
        #
        # Skipped when a supplier was injected, which is a test providing its own
        # transport and therefore its own answer to "can we chat".
        if not self._agent_mode and self._supplied_client is None and not self._config.api_key:
            await self._show_no_chat_transport()
            return

        if self._config.api_key_from_plaintext_file:
            self.notify(
                f"Your API key is stored in plain text in {config_path()}. Prefer `agentcore-tui login`, which uses the OS keyring.",
                title="Key stored in plain text",
                severity="warning",
                timeout=12,
            )

        self.composer.focus()
        await self.transcript.mount(Notice(WELCOME_AGENT if self._agent_mode else WELCOME))
        self.status.set_state("Ready")

    async def _show_no_chat_transport(self) -> None:
        """Signed in, but this screen cannot use a session to chat.

        Reachable when a credential resolves to something the selected transport
        cannot present. Says so plainly instead of blaming the credential the
        user already supplied.
        """
        self.composer.submit_enabled = False
        self.composer.disabled = True
        await self.transcript.mount(
            Notice(
                f"Signed in, but chat needs a credential this build can send ({self._config.credential_source.label}).",
                hint=(
                    "Run `agentcore-tui login --sso` for a session, or store an API key with "
                    "`agentcore-tui login`.\n"
                    f"Config file: {config_path()}"
                ),
                error=True,
            )
        )
        self.status.set_state("No chat transport", error=True)

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

    # -- client --------------------------------------------------------------

    def _client_or_create(self) -> ApiConverseClient:
        if self._client is None:
            self._client = ApiConverseClient(self._config)
        assert isinstance(self._client, ApiConverseClient)
        return self._client

    def _agent_client_or_create(self) -> AgentStreamClient:
        """Build the agent transport, reading the session at first use.

        The sealed session is read from the keyring here rather than held on
        Config, so it is fetched once per client and never travels through the
        config object that `status` prints.
        """
        if self._client is None:
            sealed, unavailable = load_session_from_keyring(self._config.base_url)
            if not sealed:
                raise ConfigError(
                    "No stored session",
                    hint=f"Run `agentcore-tui login --sso`. ({unavailable})" if unavailable else "Run `agentcore-tui login --sso`.",
                )
            self._client = AgentStreamClient(self._config, auth=SessionAuth(sealed))
        assert isinstance(self._client, AgentStreamClient)
        return self._client

    async def _discard_client(self) -> None:
        """Drop the cached client so the next turn rebuilds it.

        The client captures config at construction, so a model change has to
        invalidate it.
        """
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- TurnSink ------------------------------------------------------------
    #
    # Called by TurnController while the stream worker is running.

    async def on_text(self, chunk: str) -> None:
        if self._pending is not None:
            await self._pending.append_text(chunk)
            self.transcript.scroll_end(animate=False)

    async def on_reasoning(self, chunk: str) -> None:
        if self._pending is not None:
            await self._pending.append_reasoning(chunk)
            self.transcript.scroll_end(animate=False)

    async def on_usage(self, usage: Usage | None) -> None:
        self.status.set_usage(usage)

    async def on_state(self, state: str, *, busy: bool = False, error: bool = False) -> None:
        self.status.set_state(state, busy=busy, error=error)
        if not busy:
            self._end_of_turn_ui()

    async def on_notice(self, message: str, *, hint: str = "", error: bool = False) -> None:
        await self.transcript.mount(Notice(message, hint=hint, error=error))
        self.transcript.scroll_end(animate=False)
        if error:
            self.notify(message, title="Error", severity="error", timeout=10)

    async def on_tool(self, record: ToolCallRecord) -> None:
        """Mount a widget the first time a tool is seen, then refresh it.

        Keyed on ``tool_use_id``: the controller reports the same record several
        times as the call progresses, and mounting per call would stack
        duplicates of a single invocation.
        """
        widget = self._tool_widgets.get(record.tool_use_id)
        if widget is None:
            widget = ToolCall(record)
            self._tool_widgets[record.tool_use_id] = widget
            await self.transcript.mount(widget, before=self._pending)
        else:
            widget.refresh_from_record()
        self.transcript.scroll_end(animate=False)

    async def on_title(self, title: str) -> None:
        self.sub_title = title

    # -- turn lifecycle ------------------------------------------------------

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        if self._turn.busy or not self._config.is_complete:
            return
        self.composer.clear_prompt()
        await self._start_turn(message.text)

    async def _start_turn(self, prompt: str) -> None:
        await self._turn.begin(prompt)

        await self.transcript.mount(UserMessage(prompt))
        pending = AssistantMessage(self._config.model_id)
        await self.transcript.mount(pending)
        self._pending = pending

        self.composer.submit_enabled = False
        self.composer.add_class("-busy")
        self.refresh_bindings()

        self._flush_timer = self.set_interval(FLUSH_INTERVAL, self._turn.flush)
        self.transcript.scroll_end(animate=False)
        self._stream_turn()

    @work(exclusive=True, group="turn")
    async def _stream_turn(self) -> None:
        await self._turn.stream()
        self._log_layout_metrics()

    def _end_of_turn_ui(self) -> None:
        """Return the composer and bindings to an idle state."""
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None
        self._pending = None
        self.composer.submit_enabled = True
        self.composer.remove_class("-busy")
        self.status.set_turns(self._store.turns)
        self.refresh_bindings()
        if self.is_attached:
            self.composer.focus()

    def _log_layout_metrics(self) -> None:
        """Record whether the transcript can actually reach its own content.

        If the transcript holds more than fits but is not scrollable, message
        widgets are being clipped. That bug shipped once (Textual containers
        default to ``height: 1fr``) and presented as answers truncating
        mid-sentence, so it is worth a line in every turn's log.
        """
        transcript = self.transcript
        clipped = transcript.virtual_size.height > transcript.size.height and transcript.max_scroll_y <= 0
        logger.info(
            "turn rendered viewport_h=%d content_h=%d max_scroll_y=%d scroll_y=%.0f%s",
            transcript.size.height,
            transcript.virtual_size.height,
            transcript.max_scroll_y,
            transcript.scroll_y,
            " CLIPPED" if clipped else "",
        )
        if clipped:
            logger.error("transcript is not scrollable but content exceeds the viewport — message widgets are being clipped")

    # -- actions -------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide Stop unless a turn is actually in flight."""
        if action == "cancel_turn":
            return self._turn.busy
        return True

    async def action_cancel_turn(self) -> None:
        if not self._turn.busy:
            return
        self.workers.cancel_group(self, "turn")
        await self._turn.cancel()
        self.notify("Turn cancelled.", severity="warning")

    async def action_new_conversation(self) -> None:
        """Clear the conversation, keeping the configured model."""
        if self._turn.busy:
            await self.action_cancel_turn()
        self._store.reset()
        await self.transcript.remove_children()
        await self.transcript.mount(Notice(WELCOME))
        self.status.set_turns(0)
        self.status.set_usage(None)
        self.status.set_state("Ready")
        self.composer.focus()

    @work
    async def action_choose_model(self) -> None:
        """Open the model picker and adopt the selection."""
        chosen = await self.app.push_screen_wait(ModelPicker(self._config.models, self._config.model_id))
        if not chosen or chosen == self._config.model_id:
            return
        await self.set_model(chosen)
        self.notify(f"Model set to {chosen}", timeout=5)

    async def set_model(self, model_id: str) -> None:
        """Switch models for subsequent turns."""
        self._config = self._config.with_model(model_id)
        self._turn = TurnController(
            self._store,
            self,
            client_supplier=self._supplied_client or self._client_or_create,
            max_tokens=self._config.max_tokens,
        )
        await self._discard_client()
        self.status.set_model(model_id)

    # -- palette -------------------------------------------------------------

    def system_commands(self) -> Iterable[SystemCommand]:
        """Chat-specific palette entries, contributed to the App's palette."""
        yield SystemCommand("New conversation", "Clear the transcript and start over", self.action_new_conversation)
        yield SystemCommand("Change model", "Pick which model answers the next turn", self.action_choose_model)

        if self._turn.busy:
            yield SystemCommand("Stop response", "Abandon the turn in flight", self.action_cancel_turn)

        if self._store.last_assistant is not None:
            yield SystemCommand("Copy last response", "Copy the most recent answer to the clipboard", self._copy_last_answer)

        yield SystemCommand("Copy transcript", "Copy the whole conversation as Markdown", self._copy_transcript, discover=False)

    def _copy_last_answer(self) -> None:
        message = self._store.last_assistant
        if message is None or not message.content:
            self.notify("No response to copy yet.", severity="warning")
            return
        self.app.copy_to_clipboard(message.content)
        self.notify(f"Copied {len(message.content):,} characters.")

    def _copy_transcript(self) -> None:
        if self._store.is_empty:
            self.notify("Nothing to copy yet.", severity="warning")
            return
        self.app.copy_to_clipboard(self._store.to_markdown())
        self.notify(f"Copied {len(self._store)} messages.")
