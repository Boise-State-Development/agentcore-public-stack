"""
Main Agent Orchestrator - Slim coordination layer for multi-agent system

This module provides a clean, maintainable agent implementation with clear separation
of concerns across specialized modules.
"""

import logging
from typing import AsyncGenerator, List, Optional

# Core orchestration
from agents.main_agent.core import AgentFactory, ModelConfig, SystemPromptBuilder

# Multimodal content
from agents.main_agent.multimodal import PromptBuilder

# Session management
from agents.main_agent.session import SessionFactory
from agents.main_agent.session.hooks import ConversationCachingHook, StopHook

# Streaming coordination
from agents.main_agent.streaming import StreamCoordinator

# Tool management
from agents.main_agent.tools import GatewayIntegration, ToolFilter, create_default_registry

logger = logging.getLogger(__name__)


class MainAgent:
    """
    Main Agent orchestrator with modular architecture

    Responsibilities:
    - Initialize and coordinate specialized modules
    - Provide public API for agent operations
    - Maintain minimal state (delegate to modules)
    """

    def __init__(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        enabled_tools: Optional[List[str]] = None,
        model_id: Optional[str] = None,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        caching_enabled: Optional[bool] = None,
        provider: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ):
        """
        Initialize Main Agent with modular architecture and multi-provider support

        Args:
            session_id: Session identifier for message persistence
            user_id: User identifier for cross-session preferences (defaults to session_id)
            enabled_tools: List of tool IDs to enable. If None, all tools are enabled.
            model_id: Model ID to use (format depends on provider)
                - Bedrock: "us.anthropic.claude-haiku-4-5-20251001-v1:0"
                - OpenAI: "gpt-4o", "gpt-4o-mini", "o1-preview"
                - Gemini: "gemini-2.5-flash", "gemini-2.5-pro"
            temperature: Model temperature (0.0 - 1.0)
            system_prompt: System prompt text
            caching_enabled: Whether to enable prompt caching (Bedrock only)
            provider: LLM provider ("bedrock", "openai", or "gemini"). If not specified,
                     will auto-detect from model_id
            max_tokens: Maximum tokens to generate (optional)
        """
        # Basic state
        self.session_id = session_id
        self.user_id = user_id or session_id
        self.enabled_tools = enabled_tools
        self.agent = None

        # Initialize model configuration
        self.model_config = ModelConfig.from_params(
            model_id=model_id, temperature=temperature, caching_enabled=caching_enabled, provider=provider, max_tokens=max_tokens
        )

        # Initialize system prompt builder
        if system_prompt:
            # User provided prompt (BFF already added date)
            self.prompt_builder = SystemPromptBuilder.from_user_prompt(system_prompt)
            self.system_prompt = self.prompt_builder.build(include_date=False)
        else:
            # Use default prompt with date injection
            self.prompt_builder = SystemPromptBuilder()
            self.system_prompt = self.prompt_builder.build(include_date=True)

        # Initialize tool registry and filter
        self.tool_registry = create_default_registry()
        self.tool_filter = ToolFilter(self.tool_registry)

        # Register external MCP tool IDs from enabled tools
        # (These will be loaded lazily during _create_agent)
        self._register_external_mcp_tools()

        # Initialize gateway integration
        self.gateway_integration = GatewayIntegration()

        # Initialize multimodal prompt builder
        self.multimodal_builder = PromptBuilder()

        # Initialize session manager
        self.session_manager = SessionFactory.create_session_manager(
            session_id=session_id, user_id=self.user_id, caching_enabled=self.model_config.caching_enabled
        )

        # Initialize streaming coordinator (now stateless)
        self.stream_coordinator = StreamCoordinator()

        # Create the agent
        self._create_agent()

    def _create_agent(self) -> None:
        """Create agent with filtered tools and session management"""
        try:
            # Get filtered tools using extended filter
            filter_result = self.tool_filter.filter_tools_extended(self.enabled_tools)
            local_tools = filter_result.local_tools
            gateway_tool_ids = filter_result.gateway_tool_ids
            external_mcp_tool_ids = filter_result.external_mcp_tool_ids

            # Get gateway client and add to tools if available
            if gateway_tool_ids:
                gateway_client = self.gateway_integration.get_client(gateway_tool_ids)
                if gateway_client:
                    local_tools = self.gateway_integration.add_to_tool_list(local_tools)

            # Load external MCP tools and add to tools list
            # Pass user_id for OAuth token retrieval on OAuth-enabled tools
            if external_mcp_tool_ids:
                import asyncio

                from agents.main_agent.integrations.external_mcp_client import get_external_mcp_integration

                external_integration = get_external_mcp_integration()
                # Run async load in sync context
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If we're already in an async context, we need to handle differently
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(
                            asyncio.run,
                            external_integration.load_external_tools(
                                external_mcp_tool_ids,
                                user_id=self.user_id,  # Pass user_id for OAuth
                            ),
                        )
                        external_clients = future.result()
                else:
                    external_clients = loop.run_until_complete(
                        external_integration.load_external_tools(
                            external_mcp_tool_ids,
                            user_id=self.user_id,  # Pass user_id for OAuth
                        )
                    )

                for client in external_clients:
                    if client not in local_tools:
                        local_tools.append(client)

                logger.info(f"Added {len(external_clients)} external MCP clients to tools")

            # Create hooks
            hooks = self._create_hooks()

            # Create agent using factory
            self.agent = AgentFactory.create_agent(
                model_config=self.model_config, system_prompt=self.system_prompt, tools=local_tools, session_manager=self.session_manager, hooks=hooks
            )

        except Exception as e:
            logger.error(f"Error creating agent: {e}")
            raise

    def _register_external_mcp_tools(self) -> None:
        """
        Register external MCP tool IDs with the tool filter.

        This queries the tool catalog for tools with protocol='mcp_external'
        and registers them with the tool filter so they're recognized during filtering.
        """
        if not self.enabled_tools:
            return

        try:
            import asyncio

            from apis.app_api.tools.repository import get_tool_catalog_repository

            repository = get_tool_catalog_repository()
            external_tool_ids = []

            # Query each enabled tool to check if it's an external MCP tool
            async def check_tools():
                for tool_id in self.enabled_tools:
                    tool = await repository.get_tool(tool_id)
                    if tool and tool.protocol == "mcp_external":
                        external_tool_ids.append(tool_id)
                return external_tool_ids

            # Run async check
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
                # No event loop available
                tool_ids = asyncio.run(check_tools())

            if tool_ids:
                self.tool_filter.set_external_mcp_tools(tool_ids)
                logger.info(f"Registered {len(tool_ids)} external MCP tools: {tool_ids}")

        except Exception as e:
            logger.warning(f"Could not register external MCP tools: {e}")
            # Non-fatal - external tools just won't be available

    def _create_hooks(self) -> List:
        """
        Create agent hooks

        Returns:
            list: List of initialized hooks
        """
        hooks = []

        # Add stop hook for session cancellation (always enabled)
        stop_hook = StopHook(self.session_manager)
        hooks.append(stop_hook)

        # Add conversation caching hook if enabled (Bedrock only - other providers don't support cachePoint)
        from agents.main_agent.core import ModelProvider

        if self.model_config.caching_enabled and self.model_config.get_provider() == ModelProvider.BEDROCK:
            conversation_hook = ConversationCachingHook(enabled=True)
            hooks.append(conversation_hook)
            logger.info(f"✅ ConversationCachingHook registered (caching_enabled={self.model_config.caching_enabled})")
        else:
            logger.info(
                f"⚠️ ConversationCachingHook NOT registered (caching_enabled={self.model_config.caching_enabled}, provider={self.model_config.get_provider()})"
            )

        return hooks

    async def stream_async(
        self, message: str, session_id: Optional[str] = None, files: Optional[List] = None, citations: Optional[List] = None
    ) -> AsyncGenerator[str, None]:
        """
        Stream agent responses

        Args:
            message: User message text
            session_id: Session identifier (defaults to instance session_id)
            files: Optional list of FileContent objects (with base64 bytes)
            citations: Optional list of citation dicts from RAG retrieval

        Yields:
            str: SSE formatted events
        """
        if not self.agent:
            self._create_agent()

        # Build prompt (handles multimodal content)
        prompt = self.multimodal_builder.build_prompt(message, files)

        # Stream using coordinator
        # Pass self (MainAgent) as main_agent_wrapper so coordinator can access model_config
        async for event in self.stream_coordinator.stream_response(
            agent=self.agent,
            prompt=prompt,
            session_manager=self.session_manager,
            session_id=session_id or self.session_id,
            user_id=self.user_id,
            main_agent_wrapper=self,  # Pass wrapper for metadata extraction
            citations=citations,  # Pass citations for storage
        ):
            yield event

    def get_model_config(self) -> dict:
        """
        Get current model configuration

        Returns:
            dict: Model configuration
        """
        return {**self.model_config.to_dict(), "system_prompts": [self.system_prompt]}

    def get_tool_statistics(self) -> dict:
        """
        Get tool filtering statistics

        Returns:
            dict: Tool statistics
        """
        return self.tool_filter.get_statistics(self.enabled_tools)
