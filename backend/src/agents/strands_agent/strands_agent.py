"""
Strands Agent Orchestrator - Slim coordination layer for multi-agent system

This module provides a clean, maintainable agent implementation with clear separation
of concerns across specialized modules.
"""
import logging
from typing import AsyncGenerator, List, Optional

# Core orchestration
from agents.strands_agent.core import ModelConfig, SystemPromptBuilder, AgentFactory

# Session management
from agents.strands_agent.session import SessionFactory
from agents.strands_agent.session.hooks import StopHook, ConversationCachingHook

# Tool management
from agents.strands_agent.tools import (
    create_default_registry,
    ToolFilter,
    GatewayIntegration
)

# Multimodal content
from agents.strands_agent.multimodal import PromptBuilder

# Streaming coordination
from agents.strands_agent.streaming import StreamCoordinator

logger = logging.getLogger(__name__)


class StrandsAgent:
    """
    Main Strands Agent orchestrator with modular architecture

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
        caching_enabled: Optional[bool] = None
    ):
        """
        Initialize Strands Agent with modular architecture

        Args:
            session_id: Session identifier for message persistence
            user_id: User identifier for cross-session preferences (defaults to session_id)
            enabled_tools: List of tool IDs to enable. If None, all tools are enabled.
            model_id: Bedrock model ID to use
            temperature: Model temperature (0.0 - 1.0)
            system_prompt: System prompt text
            caching_enabled: Whether to enable prompt caching
        """
        # Basic state
        self.session_id = session_id
        self.user_id = user_id or session_id
        self.enabled_tools = enabled_tools
        self.agent = None

        # Initialize model configuration
        self.model_config = ModelConfig.from_params(
            model_id=model_id,
            temperature=temperature,
            caching_enabled=caching_enabled
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

        # Initialize gateway integration
        self.gateway_integration = GatewayIntegration()

        # Initialize multimodal prompt builder
        self.multimodal_builder = PromptBuilder()

        # Initialize session manager
        self.session_manager = SessionFactory.create_session_manager(
            session_id=session_id,
            user_id=self.user_id,
            caching_enabled=self.model_config.caching_enabled
        )

        # Initialize streaming coordinator (now stateless)
        self.stream_coordinator = StreamCoordinator()

        # Create the agent
        self._create_agent()

    def _create_agent(self) -> None:
        """Create Strands Agent with filtered tools and session management"""
        try:
            # Get filtered tools
            local_tools, gateway_tool_ids = self.tool_filter.filter_tools(self.enabled_tools)

            # Get gateway client and add to tools if available
            if gateway_tool_ids:
                gateway_client = self.gateway_integration.get_client(gateway_tool_ids)
                if gateway_client:
                    local_tools = self.gateway_integration.add_to_tool_list(local_tools)

            # Create hooks
            hooks = self._create_hooks()

            # Create agent using factory
            self.agent = AgentFactory.create_agent(
                model_config=self.model_config,
                system_prompt=self.system_prompt,
                tools=local_tools,
                session_manager=self.session_manager,
                hooks=hooks
            )

        except Exception as e:
            logger.error(f"Error creating agent: {e}")
            raise

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

        # Add conversation caching hook if enabled
        if self.model_config.caching_enabled:
            conversation_hook = ConversationCachingHook(enabled=True)
            hooks.append(conversation_hook)

        return hooks

    async def stream_async(
        self,
        message: str,
        session_id: Optional[str] = None,
        files: Optional[List] = None
    ) -> AsyncGenerator[str, None]:
        """
        Stream agent responses

        Args:
            message: User message text
            session_id: Session identifier (defaults to instance session_id)
            files: Optional list of FileContent objects (with base64 bytes)

        Yields:
            str: SSE formatted events
        """
        if not self.agent:
            self._create_agent()

        # Build prompt (handles multimodal content)
        prompt = self.multimodal_builder.build_prompt(message, files)

        # Stream using coordinator
        async for event in self.stream_coordinator.stream_response(
            agent=self.agent,
            prompt=prompt,
            session_manager=self.session_manager,
            session_id=session_id or self.session_id,
            user_id=self.user_id
        ):
            yield event

    def get_model_config(self) -> dict:
        """
        Get current model configuration

        Returns:
            dict: Model configuration
        """
        return {
            **self.model_config.to_dict(),
            "system_prompts": [self.system_prompt]
        }

    def get_tool_statistics(self) -> dict:
        """
        Get tool filtering statistics

        Returns:
            dict: Tool statistics
        """
        return self.tool_filter.get_statistics(self.enabled_tools)
