"""
Base Agent - Abstract base class for all agent types

Provides shared initialization for model config, system prompt, tool registry,
session management, and streaming. Subclasses implement _create_agent() and
stream_async() for their specific agent type.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, List, Optional

from agents.main_agent.core import ModelConfig, SystemPromptBuilder, AgentFactory
from agents.main_agent.session import SessionFactory
from agents.main_agent.session.hooks import (
    AgentStatusHook,
    ContextLedgerHook,
    ToolCensusHook,
    DisplayTextHook,
    McpAppAwaitingSubmissionHook,
    SteeringHook,
    StopHook,
    OAuthConsentHook,
    MCPExternalApprovalHook,
    MCPSessionHealHook,
    ContextAttributionHook,
    PrefixFingerprintHook,
    WebSearchDefaultsHook,
)
from agents.main_agent.tools import (
    create_default_registry,
    ToolFilter,
    GatewayIntegration,
)
from agents.main_agent.multimodal import PromptBuilder
from agents.main_agent.streaming import StreamCoordinator
from apis.shared.tools.scoped_ids import base_tool_id

from apis.shared.observability.build_stages import mark_stage, record_detail

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Abstract base class for all agent types.

    Handles shared concerns:
    - Model configuration (multi-provider: Bedrock, OpenAI, Gemini)
    - System prompt building
    - Tool registry and filtering
    - Gateway and external MCP integration
    - Session management (cloud or preview)
    - Streaming coordination

    Subclasses implement:
    - _create_agent(): Build the specific Strands agent type
    - stream_async(): Stream responses for their protocol (text, voice, etc.)
    """

    def __init__(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        enabled_tools: Optional[List[str]] = None,
        model_id: Optional[str] = None,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        caching_enabled: Optional[bool] = None,
        provider: Optional[str] = None,
        max_tokens: Optional[int] = None,
        inference_params: Optional[Dict[str, Any]] = None,
        mantle_api_mode: Optional[str] = None,
        mantle_region: Optional[str] = None,
        skip_persistence: bool = False,
        extra_tools: Optional[List[Any]] = None,
        memory_context: Optional[str] = None,
        text_only_model: bool = False,
        memory_retrieval: bool = True,
        memory_extraction: bool = True,
        knowledge_context: Optional[str] = None,
        client_surface: Optional[str] = None,
    ):
        """
        Initialize base agent with shared infrastructure.

        ``memory_context`` is the rendered Memory-Space block (reference data,
        not instructions). It is kept apart from ``system_prompt`` so it lands
        OUTSIDE the ``<user_instructions>`` wrapper and behind its own prompt
        cache point (Shared Projects 2.2).

        Args:
            session_id: Session identifier for message persistence
            user_id: User identifier for cross-session preferences (defaults to session_id)
            auth_token: Raw OIDC token for forwarding to external MCP tools (optional)
            enabled_tools: List of tool IDs to enable. If None, all tools are enabled.
            model_id: Model ID to use (format depends on provider)
            temperature: Legacy. Folded into ``inference_params['temperature']`` if set.
            system_prompt: System prompt text
            caching_enabled: Whether to enable prompt caching (Bedrock only)
            provider: LLM provider ("bedrock", "openai", or "gemini")
            max_tokens: Legacy. Folded into ``inference_params['max_tokens']`` if set.
            inference_params: Canonical-name -> value map for inference params
                (temperature, top_p, top_k, max_tokens, thinking, ...). Wins over
                the legacy ``temperature``/``max_tokens`` kwargs when both are set.
            skip_persistence: If True, don't persist messages (for preview sessions)
            text_only_model: The model's catalog row declares TEXT input only, so
                it sees history with media blocks replaced by text.
            memory_retrieval / memory_extraction: the two halves of long-term
                memory (``SessionFactory.create_session_manager``), set by
                ``get_agent`` from the effective memory mode. Both are False
                for a conversation with an Agent.
            knowledge_context: The assistant's whole knowledge base in
                full-context mode (``apis.shared.assistants.kb_full_context``).
                Sent at the end of the system prompt, inside the system cache
                segment. Not snapshotted: see ``get_agent``.
        """
        # Basic state
        self.session_id = session_id
        self.user_id = user_id or session_id
        self.auth_token = auth_token
        self.enabled_tools = enabled_tools
        self.extra_tools = extra_tools or []
        self.memory_context = memory_context or None
        self.knowledge_context = knowledge_context or None
        self.agent = None

        # Merge legacy temperature/max_tokens into the canonical dict. Explicit
        # ``inference_params`` values win over the positional kwargs so callers
        # migrating to the new shape get predictable precedence.
        resolved_params: Dict[str, Any] = dict(inference_params or {})
        if temperature is not None:
            resolved_params.setdefault("temperature", temperature)
        if max_tokens is not None:
            resolved_params.setdefault("max_tokens", max_tokens)

        self.model_config = ModelConfig.from_params(
            model_id=model_id,
            caching_enabled=caching_enabled,
            provider=provider,
            inference_params=resolved_params,
            mantle_api_mode=mantle_api_mode,
            mantle_region=mantle_region,
            text_only=text_only_model,
        )

        # Frozen snapshot of agent-construction params, used when the turn
        # pauses on OAuth consent so the resume request can rebuild this exact
        # agent shape without depending on the in-process agent cache.
        # ``system_prompt`` is captured below after the prompt builder resolves.
        self._construction_snapshot: dict = {
            "enabled_tools": enabled_tools,
            "model_id": model_id,
            "provider": provider,
            "caching_enabled": caching_enabled,
            "inference_params": dict(resolved_params),
            "mantle_api_mode": mantle_api_mode,
            "mantle_region": mantle_region,
            "text_only_model": text_only_model,
            # Recorded so a paused turn resumes under the same cache key; the
            # surface is a key dimension.
            "client_surface": client_surface,
        }

        # Load retry configuration from environment variables
        from agents.main_agent.core.model_config import RetryConfig
        self.model_config.retry_config = RetryConfig.from_env()

        # Initialize system prompt builder. `client_surface` selects the
        # interface section — which controls the agent may name, and which
        # renderers it may emit. It is threaded into BOTH branches: an assistant
        # or custom prompt still runs in a terminal, and telling that user to
        # click a gear icon is wrong regardless of who wrote the instructions.
        self.client_surface = client_surface
        if system_prompt:
            self.prompt_builder = SystemPromptBuilder.from_user_prompt(system_prompt, surface=client_surface)
            self.system_prompt = self.prompt_builder.build(include_date=False)
        else:
            self.prompt_builder = SystemPromptBuilder(surface=client_surface)
            self.system_prompt = self.prompt_builder.build(include_date=True)

        # Snapshot the *unbuilt* system_prompt — i.e. the same value the
        # caller passed to ``get_agent`` originally. The cache key hashes
        # this raw value (see ``_create_cache_key``), so storing the built
        # prompt here causes resume to land on a different cache slot than
        # the original turn. That leaves the original (paused) agent stuck
        # in the cache; a later non-resume turn cache-hits to it and
        # Strands raises "must resume from interrupt with list of
        # interruptResponse's" because _interrupt_state is still activated.
        #
        # Trade-off: if the cache evicts between pause and resume AND the
        # original ``system_prompt`` was None, the rebuilt agent re-renders
        # the date via ``include_date=True`` and may pick up *today's* date
        # rather than the original turn's. Snapshot TTL is 1h, so this only
        # matters across a midnight crossing. Resume conversation context is
        # restored from AgentCore Memory regardless, so the model still sees
        # prior turns; only the system-prompt date line shifts.
        self._construction_snapshot["system_prompt"] = system_prompt
        # A cache-key component too (hashed with the prompt), so resume must
        # replay it verbatim.
        self._construction_snapshot["memory_context"] = self.memory_context

        # Sub-stages of `agent_build` (docs/specs/turn-latency-preamble.md).
        # A no-op unless the inference-api turn path installed a recorder.
        mark_stage("prompt")

        # Enabled OAuth-gated servers this build left out because the user has
        # not connected their provider. Filled by `_build_filtered_tools`; read
        # by the system prompt (so the model knows they exist but are
        # unavailable) and by `get_agent`'s cache hit (so connecting brings
        # them back).
        self.consent_gated_tools: List[Any] = []

        # Tools left out because an earlier source already holds their name
        # (`resolve_tool_name_conflicts`). Filled by `_build_filtered_tools`;
        # read by the system prompt so the model knows why one is missing.
        self.skipped_tool_names: List[Any] = []

        # Initialize tool registry and filter
        self.tool_registry = create_default_registry()
        self.tool_filter = ToolFilter(self.tool_registry)

        # Register external MCP tool IDs from enabled tools
        self._register_external_mcp_tools()
        mark_stage("registry")

        # Initialize gateway integration
        self.gateway_integration = GatewayIntegration()

        # Initialize multimodal prompt builder
        self.multimodal_builder = PromptBuilder()

        # Initialize session manager
        self.session_manager = SessionFactory.create_session_manager(
            session_id=session_id,
            user_id=self.user_id,
            caching_enabled=self.model_config.caching_enabled,
            memory_retrieval=memory_retrieval,
            memory_extraction=memory_extraction,
        )
        # Conversation restore from AgentCore Memory happens in here, so this
        # is a prime suspect for the cold build and has never been timed.
        mark_stage("session_mgr")

        # Initialize streaming coordinator
        self.stream_coordinator = StreamCoordinator()

        # Create the agent (subclass-specific)
        self._create_agent()
        mark_stage("finalize")

    @abstractmethod
    def _create_agent(self) -> None:
        """Create the specific agent type. Subclasses must implement."""

    @abstractmethod
    async def stream_async(
        self,
        message: str,
        session_id: Optional[str] = None,
        files: Optional[List] = None,
        citations: Optional[List] = None,
        original_message: Optional[str] = None,
        interrupt_responses: Optional[List[Dict[str, Any]]] = None,
        continue_truncated: bool = False,
        turn_agent_id: Optional[str] = None,
        turn_lease: Any = None,
    ) -> AsyncGenerator[str, None]:
        """Stream agent responses. Subclasses must implement.

        When `interrupt_responses` is provided, the call resumes a paused
        agent turn (Strands interrupt protocol) instead of starting a new
        one. In that case `message`/`files` are ignored — the original turn
        already has the user's prompt in its context.

        When `continue_truncated` is True, the call resumes after a
        max_tokens truncation: `message`/`files` are ignored and the loop is
        re-entered with an empty prompt so the model continues the truncated
        assistant message already in restored history (assistant-prefill),
        rather than answering a fresh instruction.
        """

    def _register_external_mcp_tools(self) -> None:
        """
        Register external MCP and Gateway tool IDs with the tool filter.

        Queries the tool catalog for the enabled tools and registers those
        with protocol='mcp_external' (external MCP servers) and protocol='mcp'
        (AgentCore Gateway targets) so they're recognized during filtering.
        Gateway rows are registered by protocol because their id need not
        carry the ``gateway_`` prefix the filter otherwise relies on. The same
        catalog reads serve both, so recognizing Gateway rows adds no lookup.
        """
        if not self.enabled_tools:
            return

        try:
            import asyncio

            from apis.shared.tools.repository import get_tool_catalog_repository

            repository = get_tool_catalog_repository()
            external_tool_ids = []
            gateway_tool_ids = []

            async def check_tools():
                # A scoped id (`base::tool`) selects one tool of an MCP server;
                # the catalog only knows the base id, and the tool filter
                # classifies by the base too (`base in _external_mcp_tools`), so
                # look up and register the BASE id. Registering the raw scoped
                # id would miss the catalog entirely (`get_tool` is an exact PK
                # lookup) and never match the base-keyed classifier — leaving a
                # per-tool external binding unclassified and never loaded.
                seen: set[str] = set()
                for tool_id in self.enabled_tools:
                    base = base_tool_id(tool_id)
                    if base in seen:
                        continue
                    seen.add(base)
                    tool = await repository.get_tool(base)
                    if tool and tool.protocol == "mcp_external":
                        external_tool_ids.append(base)
                    elif tool and tool.protocol == "mcp":
                        gateway_tool_ids.append(base)
                return external_tool_ids

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, check_tools())
                        tool_ids = future.result()
                else:
                    tool_ids = loop.run_until_complete(check_tools())
            except RuntimeError:
                tool_ids = asyncio.run(check_tools())

            if tool_ids:
                self.tool_filter.set_external_mcp_tools(tool_ids)
                logger.info(f"Registered {len(tool_ids)} external MCP tools: {tool_ids}")
            if gateway_tool_ids:
                self.tool_filter.set_gateway_tools(gateway_tool_ids)

        except Exception as e:
            logger.warning(f"Could not register external MCP tools: {e}")

    def _create_hooks(self) -> List:
        """
        Create agent hooks.

        Includes:
        - StopHook: Always enabled, cancels tool execution on user stop
        - SteeringHook: Injects a follow-up queued mid-turn at the next tool
          boundary
        - DisplayTextHook: Stores the user's original message for UI display
          as soon as their turn is appended, so an augmented prompt is never
          what the UI renders for an interrupted turn
        - OAuthConsentHook: Pauses the agent (Strands interrupt) when an
          OAuth-gated MCP tool is about to run without a cached token
        - AgentStatusHook: Records model/tool boundaries so the UI can say what
          the agent is doing while the turn streams
        - McpAppAwaitingSubmissionHook: Marks an MCP App's "form shown,
          nothing written yet" result so the UI does not call it a failure
        - Approval hooks: Gate dangerous operations for user confirmation

        Returns:
            list: List of initialized hooks
        """
        hooks = []

        # Always-on: session cancellation
        hooks.append(StopHook(self.session_manager))

        # Mid-turn steering: a follow-up the user typed while this turn was
        # streaming is injected into the tool-result message at the next tool
        # boundary. Registered unconditionally and inert unless the turn's
        # lease carries a queued entry; MID_TURN_STEERING_ENABLED=false makes
        # it return immediately. See docs/specs/mid-turn-steering.md.
        # Held on the wrapper so the stream coordinator can drain the
        # injections it confirmed and emit `steering_applied` for each. The
        # hook itself holds no per-turn state beyond that ack — the lease is
        # read off the session manager every boundary.
        self.steering_hook = SteeringHook(self.session_manager)
        hooks.append(self.steering_hook)

        # Persist the user's own words (`displayText`) the moment their turn
        # enters history, so an augmented prompt — RAG context, attachment
        # guidance, an `<interruption_note>` — never becomes what the UI shows
        # for a turn that doesn't finish. Held on the wrapper so the stream
        # coordinator can arm it per turn and skip its own end-of-turn write
        # once this has done it.
        self.display_text_hook = DisplayTextHook()
        hooks.append(self.display_text_hook)

        # OAuth consent gate for external MCP tools. Registered unconditionally;
        # the hook is a no-op for tools that don't have a registered provider.
        hooks.append(self._build_oauth_consent_hook())

        # Per-tool approval gate. Sources its gating set from the catalog
        # (`MCPServerConfig.tools[*].needs_approval`); a no-op for any tool
        # without a flag.
        hooks.append(self._build_mcp_external_approval_hook())

        # Reconnects a shared MCP client whose session died on its own (e.g.
        # a server answering 503) before the next call on it, and reports a
        # call that session dropped as an unknown outcome rather than a
        # failure. After the two gates so a call they stop is not reconnected
        # for nothing; a healthy call pays one session probe.
        hooks.append(MCPSessionHealHook())

        # AgentCore Web Search returns 10 results (~5.3k tokens) unless asked
        # for fewer; fill in maxResults=5 when the model leaves it out. Hands
        # Strands a copy of the tool use, so the model's request in history is
        # untouched and nothing reaches the prompt. Registered only while on.
        from apis.shared.feature_flags import web_search_default_max_results_enabled

        if web_search_default_max_results_enabled():
            hooks.append(WebSearchDefaultsHook())

        # Per-turn context-token attribution (system / tools / messages).
        # Best-effort; computes the breakdown on BeforeModelCallEvent and
        # stashes it on the agent for the stream coordinator to surface on the
        # final metadata SSE event. The session id keys a process-level memo
        # of the stable split, so an Agent rebuilt for this session (cache
        # bypass, @-mention, memory binding) adopts it instead of re-counting.
        hooks.append(ContextAttributionHook(session_id=self.session_id))

        # Live narration of what the agent is doing (model call / tool call
        # boundaries) plus Strands-measured per-tool durations. Held on the
        # wrapper so the stream coordinator can drain the transitions into
        # `agent_status` SSE events, and the closed tool batches into the
        # tool-summary side-channel. Registered unconditionally; the callbacks
        # return immediately when AGENT_STATUS_ENABLED=false.
        self.agent_status_hook = AgentStatusHook()
        hooks.append(self.agent_status_hook)

        # An MCP App write tool that handed the operation to its form returns
        # `isError: true` on purpose; mark it `awaiting_user_submission` at
        # intake so the stream and the persisted message both say "waiting
        # for you" rather than "failed". Only `structuredContent` changes,
        # which no model formatter sends.
        hooks.append(McpAppAwaitingSubmissionHook())

        # Content-free tool census (tool name → calls/errors per model call).
        # Held on the wrapper so the stream coordinator can read each call's
        # tally at turn end and persist it on that call's cost row for the
        # admin session profile. Non-drained, per-turn only. Registered
        # unconditionally; the callbacks return immediately when
        # COST_DIAGNOSTICS_ENABLED=false.
        self.tool_census_hook = ToolCensusHook()
        hooks.append(self.tool_census_hook)

        # Per-model-call context ledger: the conversation window's cumulative
        # trim count and the compaction decisions taken since the previous
        # call. Same shape and lifecycle as the census — read per call at
        # turn end, persisted on the cost row, off with the same kill switch.
        self.context_ledger_hook = ContextLedgerHook()
        hooks.append(self.context_ledger_hook)

        # Per-model-call prompt-cache prefix fingerprints (toolConfig /
        # system prompt / history hashes). Best-effort; the stream
        # coordinator persists them on each call's metadata row so avoidable
        # cache misses are diagnosable from stored data.
        # PROMPT_CACHE_OBSERVABILITY_ENABLED=false disables the layer.
        from apis.shared.observability import prompt_cache_observability_enabled

        if prompt_cache_observability_enabled():
            hooks.append(PrefixFingerprintHook())

        return hooks

    def _build_mcp_external_approval_hook(self) -> MCPExternalApprovalHook:
        """Resolve a Strands `selected_tool` to the set of tool names that
        pause for approval.

        Two sources, one gate. An external MCP tool uses the per-tool set
        cached by the integration (mirrors the provider lookup used by the
        OAuth consent hook). The skill-authoring writes are a fixed set of
        built-in names (skill-creator spec D10), composed in here so the
        snapshot, resume route, breadcrumb and SPA card stay the MCP ones.
        Gating a name that was not built this turn is a no-op.
        """
        from agents.builtin_tools.skill_authoring.approval import (
            APPROVAL_TOOL_NAMES as SKILL_AUTHORING_APPROVAL_NAMES,
            describe_for_approval,
        )
        from agents.main_agent.integrations.external_mcp_client import (
            get_external_mcp_integration,
        )
        from strands.tools.mcp import MCPAgentTool

        integration = get_external_mcp_integration()

        def approval_names_lookup(selected_tool: object) -> set[str]:
            if isinstance(selected_tool, MCPAgentTool):
                return integration.approval_names_for_client(selected_tool.mcp_client)
            if getattr(selected_tool, "tool_name", None) in SKILL_AUTHORING_APPROVAL_NAMES:
                return set(SKILL_AUTHORING_APPROVAL_NAMES)
            return set()

        owner_id = self.user_id

        async def approval_details_lookup(tool_name: str, tool_input: object):
            return await describe_for_approval(owner_id, tool_name, tool_input)

        return MCPExternalApprovalHook(
            approval_names_lookup=approval_names_lookup,
            approval_details_lookup=approval_details_lookup,
        )

    def _build_oauth_consent_hook(self) -> OAuthConsentHook:
        """Construct the OAuth consent hook with closures over the MCP
        integration and provider repository so it stays decoupled from them.
        """
        from agents.main_agent.integrations.external_mcp_client import (
            get_external_mcp_integration,
        )
        from strands.tools.mcp import MCPAgentTool

        integration = get_external_mcp_integration()

        def provider_lookup(selected_tool: object) -> Optional[str]:
            if not isinstance(selected_tool, MCPAgentTool):
                return None
            return integration.provider_for_client(selected_tool.mcp_client)

        async def scopes_lookup(provider_id: str) -> List[str]:
            from apis.shared.oauth.provider_repository import get_provider_repository

            provider = await get_provider_repository().get_provider(provider_id)
            return provider.scopes if provider else []

        async def custom_parameters_lookup(
            provider_id: str,
        ) -> Optional[dict[str, str]]:
            # The connector's admin-configured OAuth `customParameters`,
            # forwarded verbatim to AgentCore Identity (e.g. a Google
            # connector's `access_type=offline` for refresh tokens +
            # `prompt=consent`, or `hd=mycorp.com` for Workspace domain
            # restriction).
            from apis.shared.oauth.provider_repository import get_provider_repository

            provider = await get_provider_repository().get_provider(provider_id)
            return provider.custom_parameters if provider else None

        async def disconnected_lookup(provider_id: str) -> bool:
            # Durable per-(user, provider) disconnect intent. Read from DDB
            # on every gate call so a /disconnect on another replica is
            # picked up before the next tool runs.
            from apis.shared.oauth.disconnect_repository import (
                get_disconnect_repository,
            )

            return await get_disconnect_repository().is_disconnected(
                self.user_id, provider_id
            )

        return OAuthConsentHook(
            user_id=self.user_id,
            provider_lookup=provider_lookup,
            scopes_lookup=scopes_lookup,
            custom_parameters_lookup=custom_parameters_lookup,
            disconnected_lookup=disconnected_lookup,
        )

    def _expand_gateway_tool_ids(self, gateway_tool_ids: List[str]) -> List[str]:
        """Expand #419 catalog gateway tools into the gateway's runtime per-tool
        ids (see `expand_gateway_tool_ids`), bridging the async catalog lookup
        into this sync build path the same way `_register_external_mcp_tools`
        does.
        """
        from apis.shared.tools.repository import get_tool_catalog_repository
        from agents.main_agent.tools.gateway_integration import (
            expand_gateway_tool_ids,
        )

        repo = get_tool_catalog_repository()

        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    return executor.submit(
                        asyncio.run, expand_gateway_tool_ids(gateway_tool_ids, repo)
                    ).result()
            return loop.run_until_complete(
                expand_gateway_tool_ids(gateway_tool_ids, repo)
            )
        except RuntimeError:
            return asyncio.run(expand_gateway_tool_ids(gateway_tool_ids, repo))

    def _build_filtered_tools(self) -> List:
        """
        Filter tools and load gateway/external MCP clients.

        Returns:
            list: Combined list of local tools + MCP clients

        Each phase closes a ``tools.*`` build sub-stage (P1a in
        docs/specs/turn-path-ttft.md); ``groups`` sums them back into
        ``agent_build.tools``. Every mark is taken on this thread, after the
        executor hop returns — contextvars do not cross into the pool.
        """
        filter_result = self.tool_filter.filter_tools_extended(self.enabled_tools)
        local_tools = filter_result.local_tools
        gateway_tool_ids = filter_result.gateway_tool_ids
        external_mcp_tool_ids = filter_result.external_mcp_tool_ids
        mark_stage("tools.filter")

        # Get gateway client and add to tools if available
        if gateway_tool_ids:
            # #419 catalog tools (`gateway_<id>`) must be expanded to the
            # gateway's runtime per-tool ids (`gateway_<target>___<tool>`)
            # before the FilteredMCPClient can match them.
            gateway_tool_ids = self._expand_gateway_tool_ids(gateway_tool_ids)
            # The JWT-authorized Gateway needs *this user's* access token per
            # invocation — there is no machine-identity fallback. Passed
            # explicitly (never cached globally) so tokens can't cross users.
            gateway_client = self.gateway_integration.get_client(
                gateway_tool_ids, auth_token=self.auth_token
            )
            if gateway_client:
                local_tools = self.gateway_integration.add_to_tool_list(local_tools)
        mark_stage("tools.gateway")

        # Load external MCP tools
        if external_mcp_tool_ids:
            import asyncio

            from bedrock_agentcore.runtime import BedrockAgentCoreContext

            from agents.main_agent.integrations.external_mcp_client import get_external_mcp_integration

            # Capture request-scoped context values before crossing the thread
            # boundary below. ContextVars do not propagate into the executor's
            # fresh event loop, so anything we need there must be passed as args.
            oauth2_callback_url = BedrockAgentCoreContext.get_oauth2_callback_url()
            workload_access_token = BedrockAgentCoreContext.get_workload_access_token()

            external_integration = get_external_mcp_integration()
            # Filled inside the executor's loop — a plain list crosses the
            # thread boundary where a contextvar recorder would not.
            server_timings: List[dict] = []

            async def _load_with_context():
                if oauth2_callback_url:
                    BedrockAgentCoreContext.set_oauth2_callback_url(oauth2_callback_url)
                if workload_access_token:
                    BedrockAgentCoreContext.set_workload_access_token(workload_access_token)
                return await external_integration.load_external_tools(
                    external_mcp_tool_ids,
                    user_id=self.user_id,
                    auth_token=self.auth_token,
                    timings=server_timings,
                    consent_gated=self.consent_gated_tools,
                )

            # Probe with ``get_running_loop`` rather than ``get_event_loop``:
            # on 3.12 the latter RAISES when no loop is running instead of
            # creating one, so the old ``else: run_until_complete(...)`` arm
            # was unreachable and the no-loop case escaped as an uncaught
            # RuntimeError. Both arms now go through ``_load_with_context``
            # so the captured context is restored either way.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # No loop in this thread — safe to drive one here.
                external_clients = asyncio.run(_load_with_context())
            else:
                # A loop is already running; we cannot block it, so hand the
                # coroutine to a worker thread with its own fresh loop.
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, _load_with_context())
                    external_clients = future.result()

            for client in external_clients:
                if client not in local_tools:
                    local_tools.append(client)

            logger.info(f"Added {len(external_clients)} external MCP clients to tools")
            # Which server owned `tools.mcp`. The hop's thread and event-loop
            # startup are in the stage but in no server's numbers; the gap
            # between the two is that overhead.
            record_detail("mcpServers", server_timings)
        mark_stage("tools.mcp")

        # Append context-bound tools (e.g., spreadsheet analysis) created per-request
        if self.extra_tools:
            local_tools.extend(self.extra_tools)
            logger.info(f"Added {len(self.extra_tools)} extra context-bound tools")

        # Two sources offering one tool name would make Strands refuse to build
        # the agent at all, failing every turn. Keep the first, drop the rest.
        from agents.main_agent.tools.tool_name_conflicts import (
            resolve_tool_name_conflicts,
        )

        local_tools, self.skipped_tool_names = resolve_tool_name_conflicts(local_tools)
        mark_stage("tools.extra")

        return local_tools

    def get_model_config(self) -> dict:
        """Get current model configuration."""
        return {**self.model_config.to_dict(), "system_prompts": [self.system_prompt]}

    def get_tool_statistics(self) -> dict:
        """Get tool filtering statistics."""
        return self.tool_filter.get_statistics(self.enabled_tools)
