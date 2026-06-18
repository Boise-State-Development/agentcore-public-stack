"""
Base Agent - Abstract base class for all agent types

Provides shared initialization for model config, system prompt, tool registry,
session management, and streaming. Subclasses implement _create_agent() and
stream_async() for their specific agent type.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from agents.main_agent.core import ModelConfig, SystemPromptBuilder, AgentFactory
from agents.main_agent.session import SessionFactory
from agents.main_agent.session.hooks import (
    StopHook,
    OAuthConsentHook,
    MCPExternalApprovalHook,
    ContextAttributionHook,
)
from agents.main_agent.tools import (
    create_default_registry,
    ToolFilter,
    GatewayIntegration,
)
from agents.main_agent.multimodal import PromptBuilder
from agents.main_agent.streaming import StreamCoordinator
from apis.shared.tools.scoped_ids import base_tool_id

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
        mantle_endpoint_path: Optional[str] = None,
        skip_persistence: bool = False,
        extra_tools: Optional[List[Any]] = None,
    ):
        """
        Initialize base agent with shared infrastructure.

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
        """
        # Basic state
        self.session_id = session_id
        self.user_id = user_id or session_id
        self.auth_token = auth_token
        self.enabled_tools = enabled_tools
        self.extra_tools = extra_tools or []
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
            mantle_endpoint_path=mantle_endpoint_path,
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
            "mantle_endpoint_path": mantle_endpoint_path,
        }

        # Load retry configuration from environment variables
        from agents.main_agent.core.model_config import RetryConfig
        self.model_config.retry_config = RetryConfig.from_env()

        # Initialize system prompt builder
        if system_prompt:
            self.prompt_builder = SystemPromptBuilder.from_user_prompt(system_prompt)
            self.system_prompt = self.prompt_builder.build(include_date=False)
        else:
            self.prompt_builder = SystemPromptBuilder()
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

        # Initialize tool registry and filter
        self.tool_registry = create_default_registry()
        self.tool_filter = ToolFilter(self.tool_registry)

        # Register external MCP tool IDs from enabled tools
        self._register_external_mcp_tools()

        # Initialize gateway integration
        self.gateway_integration = GatewayIntegration()

        # Initialize multimodal prompt builder
        self.multimodal_builder = PromptBuilder()

        # Initialize session manager
        self.session_manager = SessionFactory.create_session_manager(
            session_id=session_id, user_id=self.user_id, caching_enabled=self.model_config.caching_enabled
        )

        # Initialize streaming coordinator
        self.stream_coordinator = StreamCoordinator()

        # Create the agent (subclass-specific)
        self._create_agent()

    @abstractmethod
    def _create_agent(self) -> None:
        """Create the specific agent type. Subclasses must implement."""
        ...

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
        ...

    def _register_external_mcp_tools(self) -> None:
        """
        Register external MCP tool IDs with the tool filter.

        Queries the tool catalog for tools with protocol='mcp_external'
        and registers them so they're recognized during filtering.
        """
        if not self.enabled_tools:
            return

        try:
            import asyncio

            from apis.shared.tools.repository import get_tool_catalog_repository

            repository = get_tool_catalog_repository()
            external_tool_ids = []

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

        except Exception as e:
            logger.warning(f"Could not register external MCP tools: {e}")

    def _create_hooks(self) -> List:
        """
        Create agent hooks.

        Includes:
        - StopHook: Always enabled, cancels tool execution on user stop
        - OAuthConsentHook: Pauses the agent (Strands interrupt) when an
          OAuth-gated MCP tool is about to run without a cached token
        - Approval hooks: Gate dangerous operations for user confirmation

        Returns:
            list: List of initialized hooks
        """
        hooks = []

        # Always-on: session cancellation
        hooks.append(StopHook(self.session_manager))

        # OAuth consent gate for external MCP tools. Registered unconditionally;
        # the hook is a no-op for tools that don't have a registered provider.
        hooks.append(self._build_oauth_consent_hook())

        # Per-tool approval gate. Sources its gating set from the catalog
        # (`MCPServerConfig.tools[*].needs_approval`); a no-op for any tool
        # without a flag.
        hooks.append(self._build_mcp_external_approval_hook())

        # Per-turn context-token attribution (system / tools / messages).
        # Best-effort; computes the breakdown on BeforeModelCallEvent and
        # stashes it on the agent for the stream coordinator to surface on the
        # final metadata SSE event.
        hooks.append(ContextAttributionHook())

        return hooks

    def _build_mcp_external_approval_hook(self) -> MCPExternalApprovalHook:
        """Resolve a Strands `selected_tool` to the per-tool approval set
        cached by the external MCP integration. Mirrors the provider lookup
        used by the OAuth consent hook."""
        from agents.main_agent.integrations.external_mcp_client import (
            get_external_mcp_integration,
        )
        from strands.tools.mcp import MCPAgentTool

        integration = get_external_mcp_integration()

        def approval_names_lookup(selected_tool: object) -> set[str]:
            if not isinstance(selected_tool, MCPAgentTool):
                return set()
            return integration.approval_names_for_client(selected_tool.mcp_client)

        # Tools dispatched indirectly (SkillAgent's skill_executor meta-tool)
        # never present as MCPAgentTool, so approval_names_lookup can't gate
        # them. Subclasses that fold tools provide a tool_use-based second
        # chance — mirrors the OAuth consent hook's fold-aware lookup.
        return MCPExternalApprovalHook(
            approval_names_lookup=approval_names_lookup,
            tool_use_approval_lookup=self._build_tool_use_approval_lookup(),
        )

    def _build_tool_use_approval_lookup(
        self,
    ) -> Optional[Callable[[dict], Optional[Any]]]:
        """Approval-target resolution from a raw `tool_use` dict, for agents
        that dispatch tools indirectly (SkillAgent's meta-tools). The base
        agent has no such indirection, so the approval hook gets None and
        relies on `approval_names_lookup` alone.
        """
        return None

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

        # Tools dispatched indirectly (SkillAgent's skill_executor meta-tool)
        # never present as MCPAgentTool, so provider_lookup can't gate them.
        # Subclasses that fold tools provide a tool_use-based second chance.
        tool_use_provider_lookup = self._build_tool_use_provider_lookup()

        async def scopes_lookup(provider_id: str) -> List[str]:
            from apis.shared.oauth.provider_repository import get_provider_repository

            provider = await get_provider_repository().get_provider(provider_id)
            return provider.scopes if provider else []

        async def provider_type_lookup(provider_id: str) -> Optional[str]:
            # AgentCore Identity needs vendor-specific OAuth params
            # forwarded via `customParameters` (e.g. Google's
            # `access_type=offline` for refresh tokens). The hook reads
            # this to forward those.
            from apis.shared.oauth.provider_repository import get_provider_repository

            provider = await get_provider_repository().get_provider(provider_id)
            return provider.provider_type.value if provider else None

        async def custom_parameters_lookup(
            provider_id: str,
        ) -> Optional[dict[str, str]]:
            # Admin-supplied OAuth extras (e.g. `hd=mycorp.com` for
            # Google Workspace domain restriction). Merged with the
            # vendor baseline by `custom_parameters_for`; baseline wins
            # on conflict.
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
            provider_type_lookup=provider_type_lookup,
            custom_parameters_lookup=custom_parameters_lookup,
            disconnected_lookup=disconnected_lookup,
            tool_use_provider_lookup=tool_use_provider_lookup,
        )

    def _build_tool_use_provider_lookup(self) -> Optional[Callable[[dict], Optional[str]]]:
        """OAuth provider resolution from a raw `tool_use` dict, for agents
        that dispatch tools indirectly (SkillAgent's meta-tools). The base
        agent has no such indirection, so the consent hook gets None and
        relies on `provider_lookup` alone.
        """
        return None

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
        """
        filter_result = self.tool_filter.filter_tools_extended(self.enabled_tools)
        local_tools = filter_result.local_tools
        gateway_tool_ids = filter_result.gateway_tool_ids
        external_mcp_tool_ids = filter_result.external_mcp_tool_ids

        # Get gateway client and add to tools if available
        if gateway_tool_ids:
            # #419 catalog tools (`gateway_<id>`) must be expanded to the
            # gateway's runtime per-tool ids (`gateway_<target>___<tool>`)
            # before the FilteredMCPClient can match them.
            gateway_tool_ids = self._expand_gateway_tool_ids(gateway_tool_ids)
            gateway_client = self.gateway_integration.get_client(gateway_tool_ids)
            if gateway_client:
                local_tools = self.gateway_integration.add_to_tool_list(local_tools)

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
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                async def _load_with_context():
                    if oauth2_callback_url:
                        BedrockAgentCoreContext.set_oauth2_callback_url(oauth2_callback_url)
                    if workload_access_token:
                        BedrockAgentCoreContext.set_workload_access_token(workload_access_token)
                    return await external_integration.load_external_tools(
                        external_mcp_tool_ids,
                        user_id=self.user_id,
                        auth_token=self.auth_token,
                    )

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, _load_with_context())
                    external_clients = future.result()
            else:
                external_clients = loop.run_until_complete(
                    external_integration.load_external_tools(
                        external_mcp_tool_ids,
                        user_id=self.user_id,
                        auth_token=self.auth_token,
                    )
                )

            for client in external_clients:
                if client not in local_tools:
                    local_tools.append(client)

            logger.info(f"Added {len(external_clients)} external MCP clients to tools")

        # Append context-bound tools (e.g., spreadsheet analysis) created per-request
        if self.extra_tools:
            local_tools.extend(self.extra_tools)
            logger.info(f"Added {len(self.extra_tools)} extra context-bound tools")

        return local_tools

    def get_model_config(self) -> dict:
        """Get current model configuration."""
        return {**self.model_config.to_dict(), "system_prompts": [self.system_prompt]}

    def get_tool_statistics(self) -> dict:
        """Get tool filtering statistics."""
        return self.tool_filter.get_statistics(self.enabled_tools)
