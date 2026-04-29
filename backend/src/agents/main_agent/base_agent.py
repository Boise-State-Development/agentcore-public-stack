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
    StopHook,
    OAuthConsentHook,
    MCPExternalApprovalHook,
)
from agents.main_agent.tools import (
    create_default_registry,
    ToolFilter,
    GatewayIntegration,
)
from agents.main_agent.multimodal import PromptBuilder
from agents.main_agent.streaming import StreamCoordinator

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
        skip_persistence: bool = False,
    ):
        """
        Initialize base agent with shared infrastructure.

        Args:
            session_id: Session identifier for message persistence
            user_id: User identifier for cross-session preferences (defaults to session_id)
            auth_token: Raw OIDC token for forwarding to external MCP tools (optional)
            enabled_tools: List of tool IDs to enable. If None, all tools are enabled.
            model_id: Model ID to use (format depends on provider)
            temperature: Model temperature (0.0 - 1.0)
            system_prompt: System prompt text
            caching_enabled: Whether to enable prompt caching (Bedrock only)
            provider: LLM provider ("bedrock", "openai", or "gemini")
            max_tokens: Maximum tokens to generate (optional)
            skip_persistence: If True, don't persist messages (for preview sessions)
        """
        # Basic state
        self.session_id = session_id
        self.user_id = user_id or session_id
        self.auth_token = auth_token
        self.enabled_tools = enabled_tools
        self.agent = None

        # Initialize model configuration
        self.model_config = ModelConfig.from_params(
            model_id=model_id, temperature=temperature, caching_enabled=caching_enabled, provider=provider, max_tokens=max_tokens
        )

        # Frozen snapshot of agent-construction params, used when the turn
        # pauses on OAuth consent so the resume request can rebuild this exact
        # agent shape without depending on the in-process agent cache.
        # ``system_prompt`` is captured below after the prompt builder resolves.
        self._construction_snapshot: dict = {
            "enabled_tools": enabled_tools,
            "model_id": model_id,
            "provider": provider,
            "temperature": temperature,
            "caching_enabled": caching_enabled,
            "max_tokens": max_tokens,
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

        # Capture the resolved system prompt — what we'd need to pass back to
        # ``get_agent`` to land on the same cache key on resume.
        self._construction_snapshot["system_prompt"] = self.system_prompt

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
    ) -> AsyncGenerator[str, None]:
        """Stream agent responses. Subclasses must implement.

        When `interrupt_responses` is provided, the call resumes a paused
        agent turn (Strands interrupt protocol) instead of starting a new
        one. In that case `message`/`files` are ignored — the original turn
        already has the user's prompt in its context.
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

            from apis.app_api.tools.repository import get_tool_catalog_repository

            repository = get_tool_catalog_repository()
            external_tool_ids = []

            async def check_tools():
                for tool_id in self.enabled_tools:
                    tool = await repository.get_tool(tool_id)
                    if tool and tool.protocol == "mcp_external":
                        external_tool_ids.append(tool_id)
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

        return MCPExternalApprovalHook(approval_names_lookup=approval_names_lookup)

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

        async def mark_disconnected(provider_id: str) -> None:
            # Persist a disconnect from the AfterToolCallEvent 401-retry
            # path so subsequent requests (potentially on other replicas)
            # also force a fresh consent.
            from apis.shared.oauth.disconnect_repository import (
                get_disconnect_repository,
            )

            await get_disconnect_repository().mark_disconnected(
                self.user_id, provider_id
            )

        return OAuthConsentHook(
            user_id=self.user_id,
            provider_lookup=provider_lookup,
            scopes_lookup=scopes_lookup,
            provider_type_lookup=provider_type_lookup,
            custom_parameters_lookup=custom_parameters_lookup,
            disconnected_lookup=disconnected_lookup,
            mark_disconnected=mark_disconnected,
        )

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

        return local_tools

    def get_model_config(self) -> dict:
        """Get current model configuration."""
        return {**self.model_config.to_dict(), "system_prompts": [self.system_prompt]}

    def get_tool_statistics(self) -> dict:
        """Get tool filtering statistics."""
        return self.tool_filter.get_statistics(self.enabled_tools)
