"""
Factory for creating Strands Agent instances
"""
import logging
from typing import List, Optional, Any
from strands import Agent
from strands.models import BedrockModel
from strands.tools.executors import SequentialToolExecutor
from agents.strands_agent.core.model_config import ModelConfig

logger = logging.getLogger(__name__)


class AgentFactory:
    """Factory for creating configured Strands Agent instances"""

    @staticmethod
    def create_agent(
        model_config: ModelConfig,
        system_prompt: str,
        tools: List[Any],
        session_manager: Any,
        hooks: Optional[List[Any]] = None
    ) -> Agent:
        """
        Create a Strands Agent instance

        Args:
            model_config: Model configuration
            system_prompt: System prompt text
            tools: List of tools (local tools and/or MCP clients)
            session_manager: Session manager instance
            hooks: Optional list of agent hooks

        Returns:
            Agent: Configured Strands Agent instance
        """
        # Create BedrockModel with configuration
        bedrock_config = model_config.to_bedrock_config()
        model = BedrockModel(**bedrock_config)

        # Create agent with session manager, hooks, and system prompt
        # Use SequentialToolExecutor to prevent concurrent browser operations
        # This prevents "Failed to start and initialize Playwright" errors with NovaAct
        agent = Agent(
            model=model,
            system_prompt=system_prompt,  # Always string - BedrockModel handles caching internally
            tools=tools,
            tool_executor=SequentialToolExecutor(),
            session_manager=session_manager,
            hooks=hooks if hooks else None
        )

        return agent
