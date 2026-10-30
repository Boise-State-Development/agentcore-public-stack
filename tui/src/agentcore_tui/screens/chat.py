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
import re
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
from ..client.catalog import (
    Agent,
    CatalogClient,
    ConversationSummary,
    EffortControl,
    HistoryMessage,
    Model,
    Project,
    Skill,
    SystemPromptOption,
)
from ..client.auth import SessionAuth
from ..config import Config, config_path, load_session_from_keyring
from ..conversation import AgentBinding, ConversationStore, Message, ProjectBinding
from ..credentials import CredentialSource
from ..errors import AgentCoreTuiError, ConfigError
from ..local_tools import ApprovalDecision, LocalToolResult, ToolExecutor, tool_specs
from ..turn import (
    FLUSH_INTERVAL,
    INTERRUPT_TYPES,
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
from .agent_picker import AgentPicker
from .mode_picker import ModePicker
from .skill_picker import SkillPicker
from .project_picker import ProjectPicker
from .shared_conversations import SharedConversationList, SharedConversationView
from ..commands import COMMANDS, SlashCommand
from ..widgets.command_menu import CommandMenu
from ..widgets.history import render_history
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
        #: The user's skills (enabled state included). Loaded once on mount when
        #: signed in, and after /skills; feeds the controller and the `/` menu.
        self._skills: list[Skill] = []
        #: The conversation mode, or None. Survives /new like the model does;
        #: opening a conversation adopts that conversation's stored mode.
        self._mode: SystemPromptOption | None = None
        #: ``(model_id, owner name)`` when the bound project or agent pins the
        #: model. The server applies the pin regardless of what the turn sends
        #: (as for the web app), so this only changes what the user is shown.
        self._pinned_model: tuple[str, str] | None = None
        #: Effort level chosen per model id, for this run of the client. Kept
        #: per model as the web app does, so switching models sends that
        #: model's own choice. Nothing is sent for a model with no choice made.
        self._effort_by_model: dict[str, str] = {}
        self._effort_controls: dict[str, EffortControl | None] = {}
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
        if self._agent_mode and self._config.is_complete:
            self._load_skills()

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
        pending = AssistantMessage(self._display_model)
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
        leading = message.text[1:].split(maxsplit=1)[0] if message.text.startswith("/") else ""
        is_skill_prompt = bool(leading) and COMMANDS.resolve(leading) is None and bool(self._invoked_skill_ids(message.text))
        if message.text.startswith("/") and not message.text.startswith("//") and not is_skill_prompt:
            self.composer.clear_prompt()
            await self._run_slash_command(message.text[1:])
            return
        if self._turn.busy or not self._config.is_complete:
            return
        self.composer.clear_prompt()
        # `//` escapes a leading slash in a real prompt.
        await self._start_turn(message.text[1:] if message.text.startswith("//") else message.text)

    async def _rename_command(self, title: str) -> None:
        """`/rename <title>` for the open conversation. Titles are otherwise the
        server's, generated on the first turn."""
        title = " ".join(title.split())
        if not title:
            self.notify("Usage: /rename <title>", severity="warning", timeout=5)
            return
        if self._store.is_empty:
            self.notify("Nothing to rename yet; send a message first.", severity="warning", timeout=5)
            return
        try:
            await self._catalog_or_create().rename(self._store.session_id, title)
        except AgentCoreTuiError as exc:
            self.notify(exc.message, title="Could not rename", severity="error", timeout=8)
            return
        self.sub_title = title
        self.notify(f"Renamed to “{title}”.", timeout=4)

    # -- skills and modes ----------------------------------------------------

    @work(exclusive=True, group="skills")
    async def _load_skills(self) -> None:
        """Best effort: without the list, skills are simply off this session."""
        try:
            skills = await self._catalog_or_create().skills()
        except AgentCoreTuiError as exc:
            logger.info("skills unavailable: %s", exc)
            return
        self._apply_skills(skills or [])

    def _apply_skills(self, skills: list[Skill]) -> None:
        self._skills = skills
        enabled = [skill for skill in skills if skill.enabled]
        self.command_menu.set_skills([(skill.slug, skill.name) for skill in enabled])
        if not self._turn.busy:
            self._rebuild_controller()

    def _invoked_skill_ids(self, text: str) -> list[str]:
        """Skills named with `/slug` in a message, by the web composer's rule:
        the token starts a word and is not followed by another `/`, so paths
        and URLs stay prose. Only enabled skills resolve."""
        by_slug = {skill.slug.lower(): skill.skill_id for skill in self._skills if skill.enabled and skill.slug}
        if not by_slug or "/" not in text:
            return []
        found: list[str] = []
        for match in re.finditer(r"(?:^|\s)/([a-z0-9][a-z0-9-]*)(?![\w\-/])", text, re.IGNORECASE):
            skill_id = by_slug.get(match.group(1).lower())
            if skill_id and skill_id not in found:
                found.append(skill_id)
        return found

    @work
    async def action_choose_skills(self) -> None:
        try:
            skills = await self._catalog_or_create().skills()
        except AgentCoreTuiError as exc:
            self.notify(exc.message, title="Could not load skills", severity="error", timeout=8)
            return
        if skills is None:
            self.notify("Skills aren't enabled on this deployment.", severity="warning", timeout=8)
            return
        owner = self._store.project.name if self._store.project else (self._store.agent.name if self._store.agent else None)
        decisions = await self.app.push_screen_wait(SkillPicker(skills, locked_by=owner))
        if decisions is None:
            return
        updated = [Skill(s.skill_id, s.name, s.description, s.category, decisions.get(s.skill_id, s.enabled), s.slug) for s in skills]
        self._apply_skills(updated)
        try:
            await self._catalog_or_create().save_skill_preferences(decisions)
        except AgentCoreTuiError as exc:
            self.notify(f"Applied for this session, but not saved: {exc.message}", title="Preferences not saved", severity="warning", timeout=8)
        on = [s for s in updated if s.enabled]
        hint = (" Invoke one with " + ", ".join(f"/{s.slug}" for s in on[:3] if s.slug) + ".") if on else ""
        self.notify(f"{len(on)} of {len(updated)} skills on.{hint}", timeout=6)

    async def _mode_command(self, argument: str) -> None:
        """`/mode` picks; `/mode <name>` sets by name; `/mode off` clears."""
        word = argument.strip()
        if word.lower() in {"off", "none", "clear"}:
            await self._apply_mode(None)
            return
        try:
            modes = await self._catalog_or_create().system_prompts()
        except AgentCoreTuiError as exc:
            await self.transcript.mount(Notice(f"Could not load modes: {exc.message}", hint=exc.hint, error=True))
            self.transcript.scroll_end(animate=False)
            return
        if modes is None:
            self.notify("Conversation modes aren't enabled on this deployment.", severity="warning", timeout=8)
            return
        if not modes:
            self.notify("There are no conversation modes to choose from.", timeout=5)
            return
        if word:
            needle = word.casefold()
            matches = [m for m in modes if needle in m.name.casefold()]
            matches = [m for m in matches if m.name.casefold() == needle] or matches
            if len(matches) == 1:
                await self._apply_mode(matches[0])
            elif not matches:
                self.notify(f"No mode matching {word!r}. Modes: " + ", ".join(m.name for m in modes[:8]), severity="warning", timeout=8)
            else:
                self.notify("Several modes match: " + ", ".join(m.name for m in matches[:6]), severity="warning", timeout=8)
            return
        self.action_choose_mode(modes)

    @work
    async def action_choose_mode(self, modes: list[SystemPromptOption]) -> None:
        chosen = await self.app.push_screen_wait(ModePicker(modes, current=self._mode.prompt_id if self._mode else None))
        if chosen is None:
            return
        await self._apply_mode(chosen if isinstance(chosen, SystemPromptOption) else None)

    async def _apply_mode(self, mode: SystemPromptOption | None) -> None:
        """Set the conversation mode for this conversation and the next ones.

        Takes effect on the next turn, mid-conversation included: a mode is a
        system-prompt addition, not a binding. Clearing has to be written to
        the session too, because the server falls back to the stored mode when
        a turn names none.
        """
        if mode is None and self._mode is None:
            self.notify("No mode is set.", timeout=4)
            return
        self._mode = mode
        if isinstance(self._turn, AgentTurnController):
            self._turn.selected_prompt_id = mode.prompt_id if mode else None
        self.status.set_mode(mode.name if mode else None)
        if mode is None and not self._store.is_empty:
            try:
                await self._catalog_or_create().set_conversation_mode(self._store.session_id, None)
            except AgentCoreTuiError as exc:
                self.notify(f"Cleared here, but the conversation may keep its old mode: {exc.message}", severity="warning", timeout=8)
                return
        if self._store.bound_agent_id and mode is not None:
            self.notify(f"Mode set to {mode.name}. It applies outside agents and projects, which bring their own instructions.", timeout=8)
        else:
            self.notify(f"Mode: {mode.name}" if mode else "Mode cleared.", timeout=4)

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
                if command is not None and command.skill:
                    # A skill is invoked inside a message, so Enter completes
                    # the token for the user to finish the sentence.
                    self._complete_command(command)
                    return True
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
            case "agent":
                await self._agent_command(argument)
            case "rename":
                await self._rename_command(argument)
            case "skills":
                self.action_choose_skills()
            case "mode":
                await self._mode_command(argument)
            case "effort":
                await self._effort_command(argument)
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
        if word.lower() == "shared":
            self.action_shared_conversations()
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
        if binding is not None:
            self.status.set_agent(None)
        await self._refresh_pinned_model()
        if binding is None:
            self.notify("Left the project. New conversations run on the default agent.", timeout=6)
        else:
            note = f"New conversations run in {binding.name}: its instructions, model, tools and knowledge apply."
            if project is not None and project.archived:
                note += " This project is archived, so the server will refuse new turns until it is restored."
            self.notify(note, timeout=8)
        self.composer.focus()

    async def _agent_command(self, argument: str) -> None:
        """`/agent` picks; `/agent <name>` binds by name; `off` stops; `info` describes."""
        if not self._agent_mode:
            self.notify("Agents need a signed-in session. Run `agentcore-tui login --sso`.", severity="warning", timeout=8)
            return
        word = argument.strip()
        if word.lower() in {"off", "none", "leave"}:
            await self._apply_agent(None)
            return
        if word.lower() == "info":
            await self._show_agent_info()
            return
        try:
            agents = await self._catalog_or_create().agents()
        except AgentCoreTuiError as exc:
            await self.transcript.mount(Notice(f"Could not load agents: {exc.message}", hint=exc.hint, error=True))
            self.transcript.scroll_end(animate=False)
            return
        if agents is None:
            self.notify("Agents aren't enabled on this deployment.", severity="warning", timeout=8)
            return
        if word:
            needle = word.casefold()
            matches = [a for a in agents if needle in a.name.casefold()]
            exact = [a for a in matches if a.name.casefold() == needle]
            matches = exact or matches
            if len(matches) == 1:
                await self._apply_agent(matches[0])
            elif not matches:
                self.notify(f"No agent matching {word!r}.", severity="warning", timeout=6)
            else:
                self.notify("Several agents match: " + ", ".join(a.name for a in matches[:6]) + ". Be more specific.", severity="warning", timeout=8)
            return
        self.action_choose_agent(agents)

    @work
    async def action_choose_agent(self, agents: list[Agent]) -> None:
        current = self._store.agent.agent_id if self._store.agent else None
        chosen = await self.app.push_screen_wait(AgentPicker(agents, current=current))
        if chosen is None:
            return
        await self._apply_agent(chosen if isinstance(chosen, Agent) else None)

    async def _apply_agent(self, agent: Agent | None) -> None:
        """Talk to an Agent in new conversations (or stop). Leaves any project.

        Same rule as a project: the server binds a conversation on its first
        turn, so a change mid-conversation starts a new one — which is what the
        web app does when an `@`-mention lands in a thread that has messages.
        """
        current = self._store.agent
        if agent is None and current is None:
            self.notify("Not talking to an agent.", timeout=4)
            return
        if agent is not None and current is not None and current.agent_id == agent.agent_id:
            self.notify(f"Already talking to {agent.name}.", timeout=4)
            return
        if self._turn.busy:
            self.notify("Wait for the current turn to finish first.", severity="warning", timeout=5)
            return
        if not self._store.is_empty:
            await self.action_new_conversation()
        binding = AgentBinding(agent.agent_id, agent.name) if agent else None
        self._store.bind_agent(binding)
        self.status.set_agent(binding.name if binding else None)
        if binding is not None:
            self.status.set_project(None)
        await self._refresh_pinned_model()
        if binding is not None:
            note = f"New conversations talk to {binding.name}: its instructions, model, tools and knowledge apply."
            if agent is not None and agent.starters:
                note += " Try: " + " · ".join(f"“{s}”" for s in agent.starters[:3])
            self.notify(note, timeout=10)
        else:
            self.notify("Stopped using the agent. New conversations run on the default agent.", timeout=6)
        self.composer.focus()

    async def _show_agent_info(self) -> None:
        binding = self._store.agent
        if binding is None:
            self.notify("Not talking to an agent. `/agent` picks one.", timeout=5)
            return
        try:
            agent = await self._catalog_or_create().agent(binding.agent_id)
        except AgentCoreTuiError as exc:
            await self.transcript.mount(Notice(f"Could not load {binding.name}: {exc.message}", hint=exc.hint, error=True))
            self.transcript.scroll_end(animate=False)
            return
        lines = [f"Agent     {agent.label}"]
        if agent.owner:
            lines.append(f"Owner     {agent.owner}")
        if agent.permission:
            lines.append(f"Your role {agent.permission}")
        if agent.model_label:
            lines.append(f"Model     {agent.model_label}")
        if agent.summary:
            lines.append(f"About     {agent.summary}")
        if agent.starters:
            lines.append("Starters  " + "\n          ".join(agent.starters[:5]))
        await self.transcript.mount(Notice("\n".join(lines)))
        self.transcript.scroll_end(animate=False)

    @work
    async def action_shared_conversations(self) -> None:
        """`/project shared`: read what teammates shared with this project."""
        binding = self._store.project
        if binding is None:
            self.notify("Not in a project. `/project` picks one first.", timeout=5)
            return
        try:
            shared = await self._catalog_or_create().shared_conversations(binding.project_id)
        except AgentCoreTuiError as exc:
            await self.transcript.mount(Notice(f"Could not load shared conversations: {exc.message}", hint=exc.hint, error=True))
            self.transcript.scroll_end(animate=False)
            return
        chosen = await self.app.push_screen_wait(SharedConversationList(binding.name, shared))
        if chosen is None:
            return
        await self.app.push_screen_wait(
            SharedConversationView(
                chosen,
                self._catalog_or_create,
                on_continue=self._open_forked_conversation,
                model_label=self._display_model,
            )
        )

    async def _open_forked_conversation(self, session_id: str, title: str) -> None:
        """Open the copy a "Continue" just made, exactly as `/conversations` would."""
        catalog = self._catalog_or_create()
        try:
            summary = await catalog.conversation(session_id)
            page = await catalog.history(session_id)
        except AgentCoreTuiError as exc:
            await self.transcript.mount(
                Notice(f"Copied to “{title}”, but could not open it: {exc.message}", hint="Find it in /conversations.", error=True)
            )
            self.transcript.scroll_end(animate=False)
            return
        if self._turn.busy:
            await self.action_cancel_turn()
        messages = [item for item in page.items if isinstance(item, HistoryMessage)]
        project = None
        if summary.project_id and self._store.project and self._store.project.project_id == summary.project_id:
            binding = self._store.project
            project = Project(project_id=binding.project_id, name=binding.name, harness_agent_id=binding.harness_agent_id)
        await self._adopt_conversation(summary, messages, project)
        self.notify(f"Continuing “{summary.title or title}” as your own conversation.", timeout=6)

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
            self._rebuild_controller()
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
            f"Skills     {', '.join(f'/{sk.slug}' for sk in self._skills if sk.enabled and sk.slug) or 'none on'}",
            f"Mode       {self._mode.name if self._mode else 'none'}",
            f"Effort     {self._effort_by_model.get(self._display_model) or 'model default'}",
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
        if isinstance(self._turn, AgentTurnController):
            self._turn.invoke_skills(self._invoked_skill_ids(prompt))
        await self._turn.begin(prompt)

        await self._clear_empty_state()
        await self.transcript.mount(UserMessage(prompt))
        pending = AssistantMessage(self._display_model)
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
        pending = AssistantMessage(self._display_model)
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

        if self._pinned_model and not error:
            error = (
                f"{self._pinned_model[1]} sets the model ({short_model(self._pinned_model[0])}) for its conversations. "
                "Your choice here applies outside it."
            )
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
        agent_binding: AgentBinding | None = None
        if project is not None:
            binding = ProjectBinding(project.project_id, project.name, project.harness_agent_id)
        elif summary.project_id and summary.assistant_id:
            binding = ProjectBinding(summary.project_id, "project", summary.assistant_id)
        elif summary.assistant_id:
            # A conversation with an Agent outside any project. Same wire fact,
            # so it has to be carried or the next turn is refused; the name is
            # display-only and best-effort.
            agent_binding = AgentBinding(summary.assistant_id, await self._agent_name(summary.assistant_id))
        self._store.adopt(summary.session_id, restored, title=summary.title, project=binding, agent=agent_binding)
        self.status.set_project(binding.name if binding else None)
        self.status.set_agent(agent_binding.name if agent_binding else None)
        await self._adopt_mode(summary.selected_prompt_id)
        await self._refresh_pinned_model()

        await self.transcript.remove_children()
        self._tool_widgets.clear()
        self._pending = None

        rendered = await render_history(self.transcript, messages, model_label=self._display_model)
        self._tool_widgets.update(rendered.tool_widgets)
        records = rendered.records

        self.sub_title = summary.title
        self.status.set_turns(self._store.turns)
        self.status.set_state("Ready")
        self.transcript.scroll_end(animate=False)
        logger.info("adopted conversation %s with %d message(s), %d tool call(s)", summary.session_id, len(restored), len(records))
        await self._restore_pending(summary.session_id)

    async def _adopt_mode(self, prompt_id: str | None) -> None:
        """Take on the opened conversation's stored mode (or none)."""
        mode: SystemPromptOption | None = None
        if prompt_id:
            try:
                modes = await self._catalog_or_create().system_prompts() or []
            except AgentCoreTuiError as exc:
                logger.info("modes unavailable: %s", exc)
                modes = []
            mode = next((m for m in modes if m.prompt_id == prompt_id), None) or SystemPromptOption(prompt_id, "custom")
        self._mode = mode
        if isinstance(self._turn, AgentTurnController):
            self._turn.selected_prompt_id = prompt_id
        self.status.set_mode(mode.name if mode else None)

    def _inference_params(self) -> dict[str, Any] | None:
        """The effort override for the model that will answer, if any."""
        level = self._effort_by_model.get(self._display_model)
        if not level:
            return None
        control = self._effort_controls.get(self._display_model)
        return {control.key if control else "effort": level}

    async def _effort_control_for(self, model_id: str) -> EffortControl | None:
        """The effort control of a model, from the catalogue. None when the
        model offers no choice (the common case) or is unknown."""
        if model_id in self._effort_controls:
            return self._effort_controls[model_id]
        for model in await self._catalog_or_create().models():
            self._effort_controls[model.model_id] = model.effort
        return self._effort_controls.get(model_id)

    async def _effort_command(self, argument: str) -> None:
        """`/effort` shows the choice; `/effort high` sets; `/effort off` clears."""
        model_id = self._display_model
        if not model_id:
            self.notify("Pick a model first (/model); the system default has no effort choice.", severity="warning", timeout=8)
            return
        try:
            control = await self._effort_control_for(model_id)
        except AgentCoreTuiError as exc:
            self.notify(f"Could not load models: {exc.message}", severity="error", timeout=8)
            return
        word = argument.strip().casefold()
        if control is None:
            self.notify(f"{short_model(model_id)} does not offer an effort choice.", timeout=6)
            return
        if word in {"off", "default", "none", "clear"}:
            self._effort_by_model.pop(model_id, None)
            self._refresh_effort()
            default = f" (default: {control.default})" if control.default else ""
            self.notify(f"Effort back to the model's default{default}.", timeout=5)
            return
        levels = " · ".join(f"[{lvl}]" if lvl == self._effort_by_model.get(model_id) else lvl for lvl in control.levels)
        if not word:
            current = self._effort_by_model.get(model_id) or (f"default ({control.default})" if control.default else "default")
            self.notify(f"Effort for {short_model(model_id)}: {current}. Levels: {levels}", timeout=10)
            return
        if word not in control.levels:
            self.notify(f"{short_model(model_id)} offers: {levels}", severity="warning", timeout=8)
            return
        self._effort_by_model[model_id] = word
        self._refresh_effort()
        self.notify(f"Effort: {word} for {short_model(model_id)}. Applies from the next turn.", timeout=5)

    def _refresh_effort(self) -> None:
        """Push the current model's effort to the controller and status bar."""
        self.status.set_effort(self._effort_by_model.get(self._display_model))
        if isinstance(self._turn, AgentTurnController):
            self._turn.inference_params = self._inference_params()

    @property
    def _display_model(self) -> str:
        """The model that will actually answer, for labels."""
        return self._pinned_model[0] if self._pinned_model else self._config.model_id

    async def _refresh_pinned_model(self) -> None:
        """Ask the bound project or agent whether it pins a model. Best effort:
        on failure the user's own model is shown, as before."""
        pinned: tuple[str, str] | None = None
        try:
            if self._store.project is not None:
                model_id = await self._catalog_or_create().project_model(self._store.project.project_id)
                if model_id:
                    pinned = (model_id, self._store.project.name)
            elif self._store.agent is not None:
                agent = await self._catalog_or_create().agent(self._store.agent.agent_id)
                if agent.model_id:
                    pinned = (agent.model_id, self._store.agent.name)
        except AgentCoreTuiError as exc:
            logger.info("pinned model unavailable: %s", exc)
        self._pinned_model = pinned
        if pinned:
            self.status.set_model(pinned[0], set_by=pinned[1])
        else:
            self.status.set_model(self._config.model_id or "System Default")
        self._refresh_effort()

    async def _agent_name(self, agent_id: str) -> str:
        try:
            return (await self._catalog_or_create().agent(agent_id)).name
        except AgentCoreTuiError as exc:
            logger.info("agent %s name unavailable: %s", agent_id, exc)
            return "agent"

    async def _restore_pending(self, session_id: str) -> None:
        """Re-offer the prompts of a turn that paused while nobody was here.

        Without this, a conversation whose turn paused (you quit at an approval
        prompt, the connection dropped, or a browser tab never answered) opens
        looking idle while the server holds the turn, and the next message is
        refused because the session is busy. Best-effort: if the fetch fails,
        the conversation still opens and says so.
        """
        turn = self._turn
        if not isinstance(turn, AgentTurnController) or turn.busy:
            return
        try:
            stored = await self._catalog_or_create().pending_interrupts(session_id)
        except AgentCoreTuiError as exc:
            logger.warning("could not check for a paused turn in %s: %s", session_id, exc)
            return
        interrupts = [event for event in stored if isinstance(event, INTERRUPT_TYPES)]
        if not interrupts or self._store.session_id != session_id:
            return
        await self.transcript.mount(
            Notice(
                f"This conversation paused on {len(interrupts)} question{'s' if len(interrupts) != 1 else ''} that never got an answer.",
                hint="Answer below to let the turn finish, or press Esc to leave it paused.",
            )
        )
        self.composer.submit_enabled = False
        self.composer.add_class("-busy")
        self._set_composer_chrome(busy=True)
        self.refresh_bindings()
        await turn.restore_paused(interrupts)

    async def set_model(self, choice: ModelChoice | str) -> None:
        """Switch models for subsequent turns.

        Accepts a bare id for callers that predate the provider pairing, but a
        :class:`ModelChoice` is what the picker returns and what carries the
        provider the inference API needs.
        """
        if isinstance(choice, str):
            choice = ModelChoice(model_id=choice, provider=None)
        self._config = self._config.with_model(choice.model_id, choice.provider)
        self._rebuild_controller()
        await self._discard_client()
        if self._pinned_model:
            self.status.set_model(self._pinned_model[0], set_by=self._pinned_model[1])
        else:
            self.status.set_model(choice.model_id or "System Default")
        self._refresh_effort()
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
                enabled_skills=[skill.skill_id for skill in self._skills if skill.enabled],
                selected_prompt_id=self._mode.prompt_id if self._mode else None,
                inference_params=self._inference_params(),
            )
        return TurnController(
            self._store,
            self,
            client_supplier=self._supplied_client or self._client_or_create,
            max_tokens=self._config.max_tokens,
        )

    def _rebuild_controller(self) -> None:
        """Rebuild the controller after a settings change (model, tools,
        skills, local tools). Only reachable while no turn is running."""
        self._turn = self._build_controller()

    def _set_enabled_tools(self, tool_ids: list[str] | None) -> None:
        """Apply a tool selection to subsequent turns.

        ``None`` restores "every tool my role grants"; a list narrows it, and an
        empty list means none. Rebuilds the controller because the selection is
        constructor state — cheap, since a controller holds only in-flight data
        and no turn is running when this is reachable.
        """
        self._enabled_tools = tool_ids
        self._rebuild_controller()

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
