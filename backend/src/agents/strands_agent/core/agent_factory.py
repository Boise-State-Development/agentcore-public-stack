"""
Factory for creating Strands Agent instances with multi-provider support
"""
import os
import logging
from typing import List, Optional, Any
from strands import Agent
from strands.models import BedrockModel
from strands.models.openai import OpenAIModel
from strands.models.gemini import GeminiModel
from strands.tools.executors import SequentialToolExecutor
from agents.strands_agent.core.model_config import ModelConfig, ModelProvider

logger = logging.getLogger(__name__)


class AgentFactory:
    """Factory for creating configured Strands Agent instances with multi-provider support"""

    @staticmethod
    def _create_bedrock_model(model_config: ModelConfig) -> BedrockModel:
        """
        Create a BedrockModel instance

        Args:
            model_config: Model configuration

        Returns:
            BedrockModel: Configured Bedrock model
        """
        bedrock_config = model_config.to_bedrock_config()
        return BedrockModel(**bedrock_config)

    @staticmethod
    def _create_openai_model(model_config: ModelConfig) -> OpenAIModel:
        """
        Create an OpenAIModel instance

        Args:
            model_config: Model configuration

        Returns:
            OpenAIModel: Configured OpenAI model

        Raises:
            ValueError: If OPENAI_API_KEY environment variable is not set
        """
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY environment variable is required for OpenAI models. "
                "Please set it in your .env file."
            )

        openai_config = model_config.to_openai_config()
        client_args = {"api_key": api_key}

        logger.info(f"Creating OpenAI model with model_id={model_config.model_id}")
        return OpenAIModel(client_args=client_args, **openai_config)

    @staticmethod
    def _create_gemini_model(model_config: ModelConfig) -> GeminiModel:
        """
        Create a GeminiModel instance

        Args:
            model_config: Model configuration

        Returns:
            GeminiModel: Configured Gemini model

        Raises:
            ValueError: If GOOGLE_GEMINI_API_KEY environment variable is not set
        """
        api_key = os.getenv("GOOGLE_GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GOOGLE_GEMINI_API_KEY environment variable is required for Gemini models. "
                "Please set it in your .env file."
            )

        gemini_config = model_config.to_gemini_config()
        client_args = {"api_key": api_key}

        logger.info(f"Creating Gemini model with model_id={model_config.model_id}")
        return GeminiModel(client_args=client_args, **gemini_config)

    @staticmethod
    def create_agent(
        model_config: ModelConfig,
        system_prompt: str,
        tools: List[Any],
        session_manager: Any,
        hooks: Optional[List[Any]] = None
    ) -> Agent:
        """
        Create a Strands Agent instance with the appropriate model provider

        Args:
            model_config: Model configuration
            system_prompt: System prompt text
            tools: List of tools (local tools and/or MCP clients)
            session_manager: Session manager instance
            hooks: Optional list of agent hooks

        Returns:
            Agent: Configured Strands Agent instance

        Raises:
            ValueError: If provider is unsupported or API keys are missing
        """
        # Detect provider
        provider = model_config.get_provider()
        logger.info(f"Creating agent with provider={provider.value}, model_id={model_config.model_id}")

        # Create appropriate model based on provider
        if provider == ModelProvider.BEDROCK:
            model = AgentFactory._create_bedrock_model(model_config)
        elif provider == ModelProvider.OPENAI:
            model = AgentFactory._create_openai_model(model_config)
        elif provider == ModelProvider.GEMINI:
            model = AgentFactory._create_gemini_model(model_config)
        else:
            raise ValueError(f"Unsupported model provider: {provider}")

        # Create agent with session manager, hooks, and system prompt
        # Use SequentialToolExecutor to prevent concurrent browser operations
        # This prevents "Failed to start and initialize Playwright" errors with NovaAct
        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            tool_executor=SequentialToolExecutor(),
            session_manager=session_manager,
            hooks=hooks if hooks else None
        )

        return agent
