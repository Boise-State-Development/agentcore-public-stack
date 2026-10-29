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
from dataclasses import replace
from typing import Any, ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer, Header

from ..client import AgentStreamClient, ApiConverseClient
from ..client.agent_events import ToolCallRecord
from ..client.catalog import CatalogClient, ConversationSummary, HistoryMessage, Model, Project
from ..client.auth import SessionAuth
from ..config import Config, config_path, load_session_from_keyring
from ..conversation import ConversationStore, Message, ProjectBinding
from ..credentials import CredentialSource
from ..errors import AgentCoreTuiError, ConfigError
from ..local_tools import ApprovalDecision, LocalToolResult, ToolExecutor, tool_specs
from ..turn import (
    FLUSH_INTERVAL,
    AgentClientSupplier,
    AgentTurnController,
    BaseTurnController,
    ClientSupplier,
    InterruptEvent,
    TurnController,
)
from ..usage import Usage
from ..widgets import AssistantMessage, Composer, EmptyState, Notice, StatusBar, ToolCall, UserMessage, short_model
from ..widgets.prompts import (
    BrowserLoginPrompt,
    ClientToolPrompt,
    InterruptAnswered,
    LocalToolApproval,
    OAuthPrompt,
    ToolApprovalPrompt,
    UserQuestionPrompt,
    _Prompt,
)
from .conversations import ConversationList
from .model_picker import ModelChoice, ModelPicker
from .project_picker import ProjectPicker
from ..commands import COMMANDS, SlashCommand
from ..widgets.command_menu import CommandMenu
from .tool_picker import ToolPicker

logger = logging.getLogger(__name__)

#: Composer hints. Shown in the frame's bottom edge so they are always visible
#: without taking a transcript row.
COMPOSER_HINT = "Enter to send · Alt+Enter for a new line · / for commands"
COMPOSER_HINT_BUSY = "Esc to stop"


class ChatScreen(Screen[None]):
    """Streaming chat against one conversation."""

    #: The only key on this screen. Features are slash commands (steering: "UX
    #: paradigm"); a binding that opens one is a review rejection. priority so
    #: it fires while the composer has focus; `check_action` withdraws it when
    #: idle so Esc keeps its normal behaviour then.
    BINDINGS: ClassVar[list[BindingType]] = [
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
        self._catalog: CatalogClient | None = None
        self._agent_supplier = agent_client_supplier
        #: None means "every tool my role grants". Narrowed by the tool picker.
        self._enabled_tools: list[str] | None = None
        #: Runs the local tools and remembers "always" approvals for the session.
        self._executor = ToolExecutor()
        #: Prompts still waiting on the user, by interrupt id. The turn resumes
        #: when this empties.
        self._open_prompts: dict[str, _Prompt] = {}
        self._answers: list[dict[str, Any]] = []

        # Which controller depends on which credential is held, because the two
        # endpoints accept different ones and neither accepts both. A session
        # reaches the agent; an API key reaches only the stateless Converse
        # wrapper. Either supplier can be injected, so tests drive both dialects
        # without a credential or a keyring.
        self._agent_mode = agent_client_supplier is not None or (client_supplier is None and config.credential_source is CredentialSource.BFF_SESSION)
        self._turn: BaseTurnController[Any] = self._build_controller()

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

    @property
    def command_menu(self) -> CommandMenu:
        return self.query_one(CommandMenu)

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="transcript")
        with Vertical(id="composer-area"):
            yield CommandMenu(agent=self._agent_mode)
            yield Composer()
            yield StatusBar()
        yield Footer()

    async def on_mount(self) -> None:
        self.status.set_model(self._config.model_id)
        self.composer.key_interceptor = self._menu_key

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
        self._set_composer_chrome()
        await self.transcript.mount(EmptyState(agent=self._agent_mode, local_tools=self._agent_mode and self._config.local_tools))
        self.status.set_state("Ready")

    def _set_composer_chrome(self, *, busy: bool = False) -> None:
        """Model in the top edge of the composer, the key hint in the bottom edge."""
        composer = self.composer
        composer.border_title = short_model(self._config.model_id) if self._config.model_id else "System Default"
        composer.border_subtitle = COMPOSER_HINT_BUSY if busy else COMPOSER_HINT

    async def _clear_empty_state(self) -> None:
        for widget in self.query("#empty-state"):
            await widget.remove()

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
        if self._catalog is not None:
            await self._catalog.aclose()
            self._catalog = None

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
            self._client = AgentStreamClient(self._config, auth=SessionAuth(self._sealed_session()))
        assert isinstance(self._client, AgentStreamClient)
        return self._client

    def _catalog_or_create(self) -> CatalogClient:
        """The JSON API client, for catalogues and conversations.

        Separate from the stream client and separately cached: they have
        different timeouts (a turn may be quiet for minutes; a list should not
        be) and a model change invalidates only the stream client.
        """
        if self._catalog is None:
            self._catalog = CatalogClient(self._config.base_url, auth=SessionAuth(self._sealed_session()))
        return self._catalog

    def _sealed_session(self) -> str:
        """The stored session, or a ConfigError naming the fix."""
        sealed, unavailable = load_session_from_keyring(self._config.base_url)
        if not sealed:
            raise ConfigError(
                "No stored session",
                hint=f"Run `agentcore-tui login --sso`. ({unavailable})" if unavailable else "Run `agentcore-tui login --sso`.",
            )
        return sealed

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

    async def on_context(self, used_tokens: int | None, window: int | None) -> None:
        self.status.set_context(used_tokens, window)

    async def on_elapsed(self, seconds: float | None) -> None:
        self.status.set_elapsed(seconds)

    async def on_truncated(self) -> None:
        """The answer hit the token limit. Offer the web app's Continue."""
        limit = f" (currently {self._config.max_tokens:,})" if self._config.max_tokens else ""
        await self.transcript.mount(
            Notice(
                "The answer was cut off at the token limit.",
                hint=f"Type /continue to let it carry on, or raise `max_tokens` in your config{limit}.",
            )
        )
        self.transcript.scroll_end(animate=False)

    async def on_state(self, state: str, *, busy: bool = False, error: bool = False) -> None:
        self.status.set_state(state, busy=busy, error=error)
        if not busy:
            self._end_of_turn_ui()
        elif state == "Waiting for you":
            # Paused: stop repainting, keep the composer locked. The resume
            # restarts the flush timer against a fresh assistant message.
            if self._flush_timer is not None:
                self._flush_timer.stop()
                self._flush_timer = None
            self._set_composer_chrome(busy=True)

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

    async def on_interrupt(self, event: InterruptEvent) -> None:
        """Mount a prompt for one paused interrupt and move focus to it.

        The transcript's `_pending` assistant message stays put: the model's
        pre-pause text is already stored, and the resumed stream mounts a fresh
        message when its text starts.
        """
        prompt = self._prompt_for(event)
        self._open_prompts[event.interrupt_id] = prompt
        await self.transcript.mount(prompt)
        self.transcript.scroll_end(animate=False)
        self.refresh_bindings()

        # A local tool the user has already said "always" to runs straight away.
        if isinstance(prompt, ClientToolPrompt) and self._executor.is_pre_approved(prompt.call.tool_name):
            self.post_message(LocalToolApproval(prompt.call, ApprovalDecision.APPROVE))
            return
        prompt.focus()

    def _prompt_for(self, event: InterruptEvent) -> _Prompt:
        from ..client.agent_events import (
            BrowserLoginRequired,
            ClientToolCall,
            OAuthRequired,
            ToolApprovalRequired,
            UserQuestionRequired,
        )

        match event:
            case ClientToolCall():
                return ClientToolPrompt(event)
            case ToolApprovalRequired():
                return ToolApprovalPrompt(event)
            case UserQuestionRequired():
                return UserQuestionPrompt(event)
            case OAuthRequired():
                return OAuthPrompt(event)
            case BrowserLoginRequired():
                return BrowserLoginPrompt(event)
        raise TypeError(f"no prompt for {type(event).__name__}")  # pragma: no cover - the union is closed

    # -- answering interrupts ------------------------------------------------

    async def on_local_tool_approval(self, message: LocalToolApproval) -> None:
        """Run (or refuse) a local tool, then answer its interrupt."""
        prompt = self._open_prompts.get(message.call.interrupt_id)
        call = message.call
        if message.decision is ApprovalDecision.DECLINE:
            result = LocalToolResult(declined=True)
            summary = f"✗ Declined · {call.tool_name}"
        else:
            if message.decision is ApprovalDecision.ALWAYS:
                self._executor.remember(call.tool_name)
            self.status.set_state(f"Running {call.tool_name}...", busy=True)
            if prompt is not None:
                prompt.settle(f"… running {call.tool_name}")
            result = await self._executor.execute(call)
            glyph = "✗" if result.is_error else "✓"
            first_line = result.output.strip().splitlines()[0] if result.output.strip() else "(no output)"
            summary = f"{glyph} {call.tool_name} · {first_line[:100]}"
            logger.info("local tool %s ran error=%s chars=%d", call.tool_name, result.is_error, len(result.output))
        await self._record_answer(call.interrupt_id, result.to_wire(), summary)

    async def on_interrupt_answered(self, message: InterruptAnswered) -> None:
        await self._record_answer(message.interrupt_id, message.response, message.summary)

    async def _record_answer(self, interrupt_id: str, response: Any, summary: str) -> None:
        prompt = self._open_prompts.pop(interrupt_id, None)
        if prompt is not None:
            prompt.settle(summary)
        self._answers.append({"interruptId": interrupt_id, "response": response})
        if self._open_prompts:
            # More to answer; focus the next one.
            next(iter(self._open_prompts.values())).focus()
            return
        answers, self._answers = self._answers, []
        self.refresh_bindings()
        await self._resume_turn(answers)

    async def _resume_turn(self, answers: list[dict[str, Any]]) -> None:
        if not isinstance(self._turn, AgentTurnController) or not self._turn.paused:
            logger.warning("answers collected but no paused agent turn to resume")
            return
        pending = AssistantMessage(self._config.model_id)
        await self.transcript.mount(pending)
        self._pending = pending
        self._flush_timer = self.set_interval(FLUSH_INTERVAL, self._turn.flush)
        self.transcript.scroll_end(animate=False)
        self._resume_stream(answers)

    @work(exclusive=True, group="turn")
    async def _resume_stream(self, answers: list[dict[str, Any]]) -> None:
        assert isinstance(self._turn, AgentTurnController)
        await self._turn.resume(answers)
        self._log_layout_metrics()

    # -- turn lifecycle ------------------------------------------------------

    async def on_composer_submitted(self, message: Composer.Submitted) -> None:
        self.command_menu.hide()
        if message.text.startswith("/") and not message.text.startswith("//"):
            self.composer.clear_prompt()
            await self._run_slash_command(message.text[1:])
            return
        if self._turn.busy or not self._config.is_complete:
            return
        self.composer.clear_prompt()
        # `//` escapes a leading slash in a real prompt.
        await self._start_turn(message.text[1:] if message.text.startswith("//") else message.text)

    # -- slash commands ------------------------------------------------------

    def on_text_area_changed(self, event: Composer.Changed) -> None:
        """Every keystroke in the composer drives the command menu."""
        if event.text_area is self.composer:
            self.command_menu.update_for(self.composer.text)

    def _menu_key(self, key: str) -> bool:
        """Route a navigation key to the open command menu. True if consumed."""
        menu = self.command_menu
        if not menu.is_open:
            return False
        match key:
            case "up":
                menu.move(-1)
            case "down":
                menu.move(1)
            case "escape":
                menu.hide()
            case "tab":
                self._complete_command(menu.current)
            case "enter":
                command = menu.current
                menu.hide()
                if command is None:
                    return False
                # Enter on a highlighted command runs it, through the same
                # submit path as typing it in full. The menu only shows before
                # the first space, so there are no arguments yet.
                self.composer.post_message(Composer.Submitted(f"/{command.name}"))
            case _:
                return False
        return True

    def _complete_command(self, command: SlashCommand | None) -> None:
        """Tab: fill the composer with the highlighted command, ready for arguments."""
        if command is None:
            return
        self.composer.replace_prompt(f"/{command.name}" + (" " if command.usage else ""))
        self.command_menu.hide()

    async def _run_slash_command(self, line: str) -> None:
        """Dispatch `/command args`. Unknown commands say so and point at /help."""
        typed, _, argument = line.strip().partition(" ")
        argument = argument.strip()
        command = COMMANDS.resolve(typed)
        if command is None:
            await self.transcript.mount(
                Notice(
                    f"Unknown command /{typed}.",
                    hint="Type / to see the commands, or /help. Start a prompt with // to send a literal slash.",
                )
            )
            self.transcript.scroll_end(animate=False)
            return
        logger.info("slash command %s", command.name)
        match command.name:
            case "new":
                await self.action_new_conversation()
            case "model":
                if argument:
                    await self._switch_model_by_name(argument)
                else:
                    self.action_choose_model()
            case "tools":
                await self._tools_command(argument)
            case "conversations":
                self.action_conversations(argument or None)
            case "project":
                await self._project_command(argument)
            case "attach":
                await self._attach_file(argument)
            case "copy":
                if argument in {"all", "transcript"}:
                    self._copy_transcript()
                else:
                    self._copy_last_answer()
            case "clear":
                await self.transcript.remove_children()
                self._tool_widgets.clear()
                self.notify("Screen cleared. The conversation continues; /new starts over.", timeout=5)
            case "continue":
                await self.action_continue()
            case "stop":
                await self.action_cancel_turn()
            case "theme":
                self._theme_command(argument)
            case "status":
                await self._show_status()
            case "screenshot":
                self.app.set_timer(0.1, self.app.deliver_screenshot)
            case "help":
                await self._show_help()
            case "quit":
                await self.app.action_quit()
            case _:  # pragma: no cover - test_commands keeps this unreachable
                raise AssertionError(f"registered command without a handler: {command.name}")

    async def _project_command(self, argument: str) -> None:
        """`/project` picks; `/project <name>` binds by name; `off` leaves; `info` describes."""
        if not self._agent_mode:
            self.notify("Projects need a signed-in session. Run `agentcore-tui login --sso`.", severity="warning", timeout=8)
            return
        word = argument.strip()
        if word.lower() in {"off", "none", "leave"}:
            await self._apply_project(None)
            return
        if word.lower() == "info":
            await self._show_project_info()
            return
        projects = await self._load_projects()
        if projects is None:
            return
        if word:
            needle = word.casefold()
            matches = [p for p in projects if needle in p.name.casefold()]
            exact = [p for p in matches if p.name.casefold() == needle]
            if exact:
                matches = exact
            if len(matches) == 1:
                await self._apply_project(matches[0])
                return
            if not matches:
                self.notify(f"No project matching {word!r}.", severity="warning", timeout=6)
            else:
                self.notify(
                    "Several projects match: " + ", ".join(p.name for p in matches[:6]) + ". Be more specific.",
                    severity="warning",
                    timeout=8,
                )
            return
        self.action_choose_project(projects)

    async def _load_projects(self) -> list[Project] | None:
        """The user's projects, or None after telling the user why not."""
        try:
            projects = await self._catalog_or_create().projects()
        except AgentCoreTuiError as exc:
            await self.transcript.mount(Notice(f"Could not load projects: {exc.message}", hint=exc.hint, error=True))
            self.transcript.scroll_end(animate=False)
            return None
        if projects is None:
            self.notify("Projects aren't enabled on this deployment.", severity="warning", timeout=8)
            return None
        return projects

    @work
    async def action_choose_project(self, projects: list[Project]) -> None:
        current = self._store.project.project_id if self._store.project else None
        chosen = await self.app.push_screen_wait(ProjectPicker(projects, current=current))
        if chosen is None:
            return  # cancelled: nothing changes
        await self._apply_project(chosen if isinstance(chosen, Project) else None)

    async def _apply_project(self, project: Project | None) -> None:
        """Bind subsequent conversations to a project (or to none).

        The server binds a conversation to its Agent on the first turn and
        refuses to change it afterwards, so a change mid-conversation starts a
        new one — the same thing the web app does when you open a project.
        """
        current = self._store.project
        if project is None and current is None:
            self.notify("Not in a project.", timeout=4)
            return
        if project is not None and current is not None and current.project_id == project.project_id:
            self.notify(f"Already in {project.name}.", timeout=4)
            return
        if self._turn.busy:
            self.notify("Wait for the current turn to finish first.", severity="warning", timeout=5)
            return
        binding = ProjectBinding(project.project_id, project.name, project.harness_agent_id) if project else None
        if not self._store.is_empty:
            await self.action_new_conversation()
        self._store.bind_project(binding)
        self.status.set_project(binding.name if binding else None)
        if binding is None:
            self.notify("Left the project. New conversations run on the default agent.", timeout=6)
        else:
            note = f"New conversations run in {binding.name}: its instructions, model, tools and knowledge apply."
            if project is not None and project.archived:
                note += " This project is archived, so the server will refuse new turns until it is restored."
            self.notify(note, timeout=8)
        self.composer.focus()

    async def _show_project_info(self) -> None:
        binding = self._store.project
        if binding is None:
            self.notify("Not in a project. `/project` picks one.", timeout=5)
            return
        try:
            project = await self._catalog_or_create().project(binding.project_id)
        except AgentCoreTuiError as exc:
            await self.transcript.mount(Notice(f"Could not load {binding.name}: {exc.message}", hint=exc.hint, error=True))
            self.transcript.scroll_end(animate=False)
            return
        lines = [
            f"Project   {project.name}" + ("  (archived)" if project.archived else ""),
            f"Owner     {project.owner_label}",
            f"Your role {project.role}",
            f"Members   {project.member_count + 1}",
        ]
        if project.description:
            lines.append(f"About     {project.description}")
        lines.append("")
        lines.append("Its instructions, model, tools, skills, knowledge and memory apply to conversations here.")
        lines.append("Members, files and memory are managed in the web app.")
        await self.transcript.mount(Notice("\n".join(lines)))
        self.transcript.scroll_end(animate=False)

    async def _tools_command(self, argument: str) -> None:
        """`/tools` opens the server-tool picker; `/tools local on|off` is the local switch.

        Local tools are not catalogue entries, so they cannot live in the
        picker: an administrator cannot grant or revoke "run a command on the
        user's machine". This is the one place they are switched.
        """
        words = argument.lower().split()
        if not words:
            self.action_choose_tools()
            return
        if words[0] == "local" and len(words) == 2 and words[1] in {"on", "off"}:
            if not self._agent_mode:
                self.notify("Local tools need a signed-in session.", severity="warning", timeout=5)
                return
            if self._turn.busy:
                self.notify("Wait for the current turn to finish first.", severity="warning", timeout=5)
                return
            self._config = replace(self._config, local_tools=words[1] == "on")
            self._turn = self._build_controller()
            if self._config.local_tools:
                self.notify("Local tools on for this session: the agent may ask to run commands and read files here.", timeout=6)
            else:
                self.notify("Local tools off for this session.", timeout=4)
            return
        self.notify("Usage: /tools, or /tools local on|off", severity="warning", timeout=5)

    def _theme_command(self, argument: str) -> None:
        """`/theme <name>` switches; `/theme` lists what is available."""
        names = sorted(self.app.available_themes)
        if not argument:
            current = self.app.theme
            listing = "  ".join(f"[b]{name}[/b]" if name == current else name for name in names)
            self.notify(f"{listing}\nUse /theme <name>.", title="Themes", timeout=10)
            return
        wanted = argument.strip().lower()
        if wanted not in self.app.available_themes:
            self.notify(f"No theme called {wanted!r}. One of: {', '.join(names)}", severity="warning", timeout=8)
            return
        self.app.theme = wanted
        self.notify(f"Theme: {wanted}", timeout=3)

    async def _show_status(self) -> None:
        """Where am I, as whom, on what: the facts a bug report needs."""
        from ..logging_setup import active_log_path

        source = getattr(self._config.credential_source, "value", str(self._config.credential_source))
        tools = "all granted" if self._enabled_tools is None else f"{len(self._enabled_tools)} selected"
        if self._agent_mode:
            tools += f" · local tools {'on' if self._config.local_tools else 'off'}"
        lines = [
            f"Server     {self._config.base_url or 'not configured'}",
            f"Signed in  {source}",
            f"Model      {self._config.model_id}",
            f"Tools      {tools}",
            f"Session    {self._store.session_id or '(none yet)'}",
            f"Log        {active_log_path() or 'not configured'}",
        ]
        await self.transcript.mount(Notice("\n".join(lines)))
        self.transcript.scroll_end(animate=False)

    async def _show_help(self) -> None:
        body = "\n".join(COMMANDS.help_lines(agent=self._agent_mode))
        keys = "Enter send · Alt+Enter newline · Esc stop · Ctrl+C quit · Tab completes a command"
        await self.transcript.mount(Notice(body, hint=keys))
        self.transcript.scroll_end(animate=False)

    async def _attach_file(self, raw_path: str) -> None:
        """Upload a local file into the conversation for the next turn."""
        from pathlib import Path

        turn = self._turn
        if not isinstance(turn, AgentTurnController):
            self.notify("Attachments need a signed-in session. Run `agentcore-tui login --sso`.", severity="warning", timeout=8)
            return
        if not raw_path:
            self.notify("Usage: /attach <path>", severity="warning", timeout=5)
            return
        if turn.busy:
            self.notify("Wait for the current turn to finish before attaching.", severity="warning", timeout=5)
            return
        path = Path(raw_path.strip().strip("'\"")).expanduser()
        if not path.is_file():
            await self.transcript.mount(Notice(f"No such file: {path}", error=True))
            self.transcript.scroll_end(animate=False)
            return
        self.status.set_state(f"Uploading {path.name}...", busy=True)
        try:
            uploaded = await self._catalog_or_create().upload_file(self._store.session_id, path)
        except AgentCoreTuiError as exc:
            self.status.set_state("Ready")
            await self.transcript.mount(Notice(f"Could not attach {path.name}: {exc.message}", hint=exc.hint, error=True))
            self.transcript.scroll_end(animate=False)
            return
        turn.attach(uploaded.upload_id)
        self.status.set_state("Ready")
        size = f"{uploaded.size_bytes / 1024:,.0f} KB" if uploaded.size_bytes >= 1024 else f"{uploaded.size_bytes} B"
        await self.transcript.mount(Notice(f"Attached {uploaded.filename} ({size}). It goes with your next message."))
        self.transcript.scroll_end(animate=False)
        self.composer.focus()

    async def _switch_model_by_name(self, needle: str) -> None:
        """`/model haiku` without opening the picker.

        Matches against the catalogue when signed in, else the configured list;
        an ambiguous match lists the candidates rather than guessing.
        """
        needle_cf = needle.casefold()
        candidates: list[ModelChoice] = []
        labels: list[str] = []
        if self._agent_mode:
            try:
                for model in await self._catalog_or_create().models():
                    if needle_cf in model.model_id.casefold() or needle_cf in model.label.casefold():
                        candidates.append(ModelChoice(model_id=model.model_id, provider=model.provider))
                        labels.append(f"{model.label} ({model.model_id})")
            except AgentCoreTuiError as exc:
                self.notify(f"Could not load models: {exc.message}", severity="error", timeout=8)
                return
        else:
            for model_id in self._config.models:
                if needle_cf in model_id.casefold():
                    candidates.append(ModelChoice(model_id=model_id, provider=None))
                    labels.append(model_id)
        if not candidates:
            self.notify(f"No model matches '{needle}'.", severity="warning", timeout=6)
            return
        if len(candidates) > 1:
            await self.transcript.mount(Notice(f"'{needle}' matches several models:", hint="\n".join(labels)))
            self.transcript.scroll_end(animate=False)
            return
        await self.set_model(candidates[0])
        self.notify(f"Model set to {candidates[0].model_id}", timeout=5)

    async def _start_turn(self, prompt: str) -> None:
        await self._turn.begin(prompt)

        await self._clear_empty_state()
        await self.transcript.mount(UserMessage(prompt))
        pending = AssistantMessage(self._config.model_id)
        await self.transcript.mount(pending)
        self._pending = pending

        self.composer.submit_enabled = False
        self.composer.add_class("-busy")
        self._set_composer_chrome(busy=True)
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
        self._set_composer_chrome()
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
        for prompt in self._open_prompts.values():
            prompt.settle("Cancelled")
        self._open_prompts.clear()
        self._answers.clear()
        await self._turn.cancel()
        self.notify("Turn cancelled.", severity="warning")

    async def action_continue(self) -> None:
        """Continue a truncated answer. Only the agent dialect can."""
        turn = self._turn
        if not isinstance(turn, AgentTurnController) or not turn.can_continue:
            self.notify("Nothing to continue.", severity="warning", timeout=5)
            return
        pending = AssistantMessage(self._config.model_id)
        await self.transcript.mount(pending)
        self._pending = pending
        self.composer.submit_enabled = False
        self.composer.add_class("-busy")
        self._set_composer_chrome(busy=True)
        self.refresh_bindings()
        self._flush_timer = self.set_interval(FLUSH_INTERVAL, turn.flush)
        self.transcript.scroll_end(animate=False)
        self._continue_stream()

    @work(exclusive=True, group="turn")
    async def _continue_stream(self) -> None:
        assert isinstance(self._turn, AgentTurnController)
        await self._turn.continue_truncated()
        self._log_layout_metrics()

    async def action_new_conversation(self) -> None:
        """Clear the conversation, keeping the configured model."""
        if self._turn.busy:
            await self.action_cancel_turn()
        self._store.reset()  # keeps the project binding; /project off leaves
        self._open_prompts.clear()
        self._answers.clear()
        self._executor.forget_all()
        await self.transcript.remove_children()
        self._tool_widgets.clear()
        await self.transcript.mount(EmptyState(agent=self._agent_mode, local_tools=self._agent_mode and self._config.local_tools))
        self.status.set_turns(0)
        self.status.set_usage(None)
        self.status.set_context(None, None)
        self.status.set_elapsed(None)
        self.status.set_state("Ready")
        self.composer.focus()

    @work
    async def action_choose_model(self) -> None:
        """Open the model picker, populated from the server's catalogue."""
        models: list[Model] = []
        error = ""
        if self._agent_mode:
            try:
                models = await self._catalog_or_create().models()
            except AgentCoreTuiError as exc:
                # The picker still opens: "System Default" is a working choice
                # and is more useful than a dead keybinding.
                error = f"Could not load models: {exc.message}"
                logger.warning("model catalogue unavailable: %s", exc)
        else:
            error = "Sign in with `agentcore-tui login --sso` to see your deployment's models."

        chosen = await self.app.push_screen_wait(ModelPicker(models, current=self._config.model_id, error=error))
        if chosen is None:
            return
        if chosen.model_id == self._config.model_id and chosen.provider == self._config.provider:
            return
        await self.set_model(chosen)
        self.notify(f"Model set to {chosen.model_id or 'System Default'}", timeout=5)

    @work
    async def action_choose_tools(self) -> None:
        """Open the tool picker, then apply and persist the choice."""
        if not self._agent_mode:
            self.notify(
                "Tools need a signed-in session. Run `agentcore-tui login --sso`.",
                title="Not available",
                severity="warning",
                timeout=8,
            )
            return

        try:
            tools = await self._catalog_or_create().tools()
        except AgentCoreTuiError as exc:
            self.notify(exc.message, title="Could not load tools", severity="error", timeout=8)
            return

        decisions = await self.app.push_screen_wait(ToolPicker(tools))
        if decisions is None:
            return

        enabled = sorted(tool_id for tool_id, on in decisions.items() if on)
        # Apply first, persist second: the turn the user is about to take matters
        # more than the record of the preference, and a failed save should not
        # discard a choice they can see.
        self._set_enabled_tools(enabled)
        try:
            await self._catalog_or_create().save_tool_preferences(decisions)
        except AgentCoreTuiError as exc:
            self.notify(
                f"Applied for this session, but not saved: {exc.message}",
                title="Preferences not saved",
                severity="warning",
                timeout=8,
            )
        self.notify(f"{len(enabled)} of {len(decisions)} tools enabled", timeout=5)

    @work
    async def action_conversations(self, query: str | None = None) -> None:
        """Browse the user's conversations and open one. `query` pre-fills the filter."""
        if not self._agent_mode:
            self.notify(
                "Saved conversations need a signed-in session. Run `agentcore-tui login --sso`.",
                title="Not available",
                severity="warning",
                timeout=8,
            )
            return
        await self.app.push_screen_wait(ConversationList(self._catalog_or_create, on_open=self._adopt_conversation, initial_query=query))

    async def _adopt_conversation(self, summary: ConversationSummary, messages: list[HistoryMessage], project: Project | None = None) -> None:
        """Replace the transcript with a stored conversation.

        The store's id changes with its contents, so the next turn continues the
        conversation the user is now looking at rather than the one they left.

        Tool calls are rendered with the same widget a live turn uses: a stored
        ``toolUse`` opens a record and the matching ``toolResult`` (which Bedrock
        stores on the *next* user message) closes it. Attachments are named.
        Only block kinds nobody here can draw are counted and reported.
        """
        restored: list[Message] = []
        for message in messages:
            # Only the two roles the store models, and only messages with prose:
            # a tool-result-only user message is not something the user typed.
            if message.role == "user" and message.text.strip():
                restored.append(Message(role="user", content=message.text))
            elif message.role == "assistant" and message.text.strip():
                restored.append(Message(role="assistant", content=message.text, reasoning=message.reasoning))
        # A project conversation stays in its project: the server will refuse a
        # turn that drops or changes the binding, so the harness id travels
        # with the history. The name is for the status bar; the harness id
        # from the project record is preferred, the session's own stored
        # assistantId is the fallback when the project is not listable.
        binding: ProjectBinding | None = None
        if project is not None:
            binding = ProjectBinding(project.project_id, project.name, project.harness_agent_id)
        elif summary.project_id and summary.assistant_id:
            binding = ProjectBinding(summary.project_id, "project", summary.assistant_id)
        self._store.adopt(summary.session_id, restored, title=summary.title, project=binding)
        self.status.set_project(binding.name if binding else None)

        await self.transcript.remove_children()
        self._tool_widgets.clear()
        self._pending = None

        records: dict[str, ToolCallRecord] = {}
        unrendered = 0
        for message in messages:
            for result in message.tool_results:
                record = records.get(result.tool_use_id)
                if record is not None:
                    record.result = result.text
                    record.is_error = result.is_error
                    widget = self._tool_widgets.get(record.tool_use_id)
                    if widget is not None:
                        widget.refresh_from_record()
            if message.attachments:
                names = ", ".join(a.name or a.kind for a in message.attachments)
                await self.transcript.mount(Notice(f"Attached: {names}"))
            if message.role == "user" and message.text.strip():
                await self.transcript.mount(UserMessage(message.text))
            for use in message.tool_uses:
                record = ToolCallRecord(tool_use_id=use.tool_use_id, name=use.name, arguments=use.arguments)
                records[use.tool_use_id] = record
                widget = ToolCall(record)
                self._tool_widgets[use.tool_use_id] = widget
                await self.transcript.mount(widget)
            if message.role == "assistant" and message.text.strip():
                assistant = AssistantMessage(self._config.model_id)
                await self.transcript.mount(assistant)
                if message.reasoning:
                    await assistant.append_reasoning(message.reasoning)
                await assistant.append_text(message.text)
            unrendered += len(message.other_blocks)

        if unrendered:
            await self.transcript.mount(Notice(f"{unrendered} stored item(s) of a kind this terminal cannot show were skipped."))

        self.sub_title = summary.title
        self.status.set_turns(self._store.turns)
        self.status.set_state("Ready")
        self.transcript.scroll_end(animate=False)
        logger.info("adopted conversation %s with %d message(s), %d tool call(s)", summary.session_id, len(restored), len(records))

    async def set_model(self, choice: ModelChoice | str) -> None:
        """Switch models for subsequent turns.

        Accepts a bare id for callers that predate the provider pairing, but a
        :class:`ModelChoice` is what the picker returns and what carries the
        provider the inference API needs.
        """
        if isinstance(choice, str):
            choice = ModelChoice(model_id=choice, provider=None)
        self._config = self._config.with_model(choice.model_id, choice.provider)
        self._turn = self._build_controller()
        await self._discard_client()
        self.status.set_model(choice.model_id or "System Default")
        if self.is_attached and self._config.is_complete:
            self._set_composer_chrome(busy=self._turn.busy)

    def _build_controller(self) -> BaseTurnController[Any]:
        """The one place a controller is constructed.

        `set_model` used to build a `TurnController` unconditionally, which would
        silently swap the agent for the Converse path on a model change. Both
        call sites go through here so the dialect is decided once.
        """
        if self._agent_mode:
            return AgentTurnController(
                self._store,
                self,
                client_supplier=self._agent_supplier or self._agent_client_or_create,
                max_tokens=self._config.max_tokens,
                enabled_tools=self._enabled_tools,
                client_tools=tool_specs() if self._config.local_tools else None,
            )
        return TurnController(
            self._store,
            self,
            client_supplier=self._supplied_client or self._client_or_create,
            max_tokens=self._config.max_tokens,
        )

    def _set_enabled_tools(self, tool_ids: list[str] | None) -> None:
        """Apply a tool selection to subsequent turns.

        ``None`` restores "every tool my role grants"; a list narrows it, and an
        empty list means none. Rebuilds the controller because the selection is
        constructor state — cheap, since a controller holds only in-flight data
        and no turn is running when this is reachable.
        """
        self._enabled_tools = tool_ids
        self._turn = self._build_controller()

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
