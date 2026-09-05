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
from agents.main_agent.core.bedrock_count_tokens import CountTokensBedrockModel
from agents.main_agent.core.model_config import ModelConfig, ModelProvider
from agents.main_agent.config.constants import EnvVars
from apis.shared.models.bedrock_responses import build_bedrock_responses_model
from apis.shared.models.mantle import build_mantle_model
from apis.shared.models.usage_normalization import usage_normalized

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
            BedrockModel: Configured Bedrock model (a ``CountTokensBedrockModel``
            so native CountTokens works for inference-profile model ids).
        """
        bedrock_config = model_config.to_bedrock_config()
        return CountTokensBedrockModel(**bedrock_config)

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
        api_key = os.getenv(EnvVars.OPENAI_API_KEY)
        if not api_key:
            raise ValueError(
                f"{EnvVars.OPENAI_API_KEY} environment variable is required for OpenAI models. "
                "Please set it in your .env file."
            )

        openai_config = model_config.to_openai_config()
        client_args = {"api_key": api_key}

        logger.info(f"Creating OpenAI model with model_id={model_config.model_id}")
        # Wrapped for Bedrock-Converse token-bucket semantics — OpenAI's
        # `input_tokens` is inclusive of the cache buckets, which our cost and
        # context-size math treats as disjoint. See
        # apis/shared/models/usage_normalization.py.
        return usage_normalized(OpenAIModel)(client_args=client_args, **openai_config)

    @staticmethod
    def _create_mantle_model(model_config: ModelConfig):
        """
        Create a Strands OpenAI-compatible model pointed at Bedrock Mantle

        Mantle is AWS's OpenAI-compatible inference surface for Bedrock-hosted
        models. Strands' ``bedrock_mantle_config`` owns the wire details: it
        mints the short-term bearer token (via aws-bedrock-token-generator,
        requires `bedrock-mantle:CallWithBearerToken`) on demand and derives the
        regional base URL plus the model-family base path
        (`openai.gpt-5.*` -> /openai/v1, everything else -> /v1).

        The model's declared API surface picks the class: Chat Completions
        (``OpenAIModel``) or the Responses API (``OpenAIResponsesModel``) — some
        Mantle models (e.g. `openai.gpt-5.x`) only serve Responses and reject
        Chat Completions.

        ``mantle_region`` optionally pins inference to the region hosting the
        model (e.g. us-east-1) independent of the app's region; it drives both
        the Mantle endpoint and the region the bearer token is signed for.

        Args:
            model_config: Model configuration

        Returns:
            OpenAIModel | OpenAIResponsesModel: Configured model targeting Mantle
        """
        # region resolves from (in order) the model override, then AWS_REGION;
        # None lets Strands fall back to the boto session / standard chain.
        region = model_config.mantle_region or os.getenv(EnvVars.AWS_REGION)
        mantle_config = model_config.to_mantle_config()
        logger.info(
            f"Creating Bedrock Mantle model (api={model_config.mantle_api_mode.value}) "
            f"with model_id={model_config.model_id} "
            f"region={region or '<agent default>'}"
        )
        # Shared builder — also used by the API-key /chat/api-converse handler
        # (apis/app_api) so the Mantle model construction is never forked.
        return build_mantle_model(
            model_id=mantle_config["model_id"],
            api_mode=model_config.mantle_api_mode,
            region=region,
            params=mantle_config.get("params"),
        )

    @staticmethod
    def _create_bedrock_responses_model(model_config: ModelConfig):
        """
        Create an OpenAI Responses model on the bedrock-runtime endpoint

        The only Bedrock path that serves prompt caching for GPT-5.6. Same
        OpenAI wire protocol as Mantle, different host and model-id shape
        (cross-Region inference profiles).

        Args:
            model_config: Model configuration

        Returns:
            OpenAIResponsesModel: Configured model targeting bedrock-runtime

        Raises:
            ValueError: If no AWS region can be resolved for the endpoint
        """
        # Same precedence as the Mantle path: model override, then AWS_REGION.
        # Unlike Mantle, an unresolvable region raises rather than defaulting —
        # the builder owns that, so both the URL and the token signature stay
        # on one value.
        region = model_config.mantle_region or os.getenv(EnvVars.AWS_REGION)
        responses_config = model_config.to_bedrock_responses_config()
        logger.info(
            f"Creating bedrock-runtime Responses model "
            f"with model_id={model_config.model_id} "
            f"region={region or '<agent default>'}"
        )
        # Shared builder — also used by the API-key /chat/api-converse handler
        # (apis/app_api) so the transport construction is never forked.
        return build_bedrock_responses_model(
            model_id=responses_config["model_id"],
            region=region,
            params=responses_config.get("params"),
        )

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
        api_key = os.getenv(EnvVars.GOOGLE_GEMINI_API_KEY)
        if not api_key:
            raise ValueError(
                f"{EnvVars.GOOGLE_GEMINI_API_KEY} environment variable is required for Gemini models. "
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
        hooks: Optional[List[Any]] = None,
        plugins: Optional[List[Any]] = None,
    ) -> Agent:
        """
        Create a Strands Agent instance with the appropriate model provider

        Args:
            model_config: Model configuration
            system_prompt: System prompt text
            tools: List of tools (local tools and/or MCP clients)
            session_manager: Session manager instance
            hooks: Optional list of agent hooks
            plugins: Optional list of Strands plugins (e.g. AgentSkills). A
                plugin auto-registers its hooks and tools with the agent, so
                this is how skills disclosure is wired (Skills v2).

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
        elif provider == ModelProvider.MANTLE:
            model = AgentFactory._create_mantle_model(model_config)
        elif provider == ModelProvider.BEDROCK_RESPONSES:
            model = AgentFactory._create_bedrock_responses_model(model_config)
        elif provider == ModelProvider.GEMINI:
            model = AgentFactory._create_gemini_model(model_config)
        else:
            raise ValueError(f"Unsupported model provider: {provider}")

        # Build SDK-level retry strategy for Bedrock provider
        # This is the second retry layer (agent event loop), retries with
        # exponential backoff. Only applies to Bedrock; other providers handle
        # retries internally.
        #
        # Stock ModelRetryStrategy retries ModelThrottledException ONLY, which
        # leaves Bedrock's transient service faults (ServiceUnavailableException,
        # InternalServerException, ...) unretried — they arrive as raw
        # botocore ClientErrors. BedrockTransientRetryStrategy widens the
        # predicate to cover those when they fire before the response stream
        # opens; see its module docstring for why mid-stream faults are excluded.
        retry_strategy = None
        if provider == ModelProvider.BEDROCK and model_config.retry_config:
            from strands import ModelRetryStrategy
            from agents.main_agent.core.retry_strategy import BedrockTransientRetryStrategy

            strategy_cls = (
                BedrockTransientRetryStrategy
                if model_config.retry_config.retry_transient_service_errors
                else ModelRetryStrategy
            )
            retry_strategy = strategy_cls(
                max_attempts=model_config.retry_config.sdk_max_attempts,
                initial_delay=model_config.retry_config.sdk_initial_delay,
                max_delay=model_config.retry_config.sdk_max_delay,
            )
            logger.info(
                f"Configured retry strategy: boto={model_config.retry_config.boto_max_attempts} attempts "
                f"({model_config.retry_config.boto_retry_mode}), "
                f"sdk={model_config.retry_config.sdk_max_attempts} attempts "
                f"({model_config.retry_config.sdk_initial_delay}s-{model_config.retry_config.sdk_max_delay}s backoff), "
                f"strategy={strategy_cls.__name__}"
            )

        # Bedrock prompt caching: give the system prompt its own cachePoint by
        # passing it as a SystemContentBlock list with a trailing cachePoint
        # (the cache_prompt model-config key is deprecated). Together with
        # cache_tools (set in to_bedrock_config) this keeps the stable
        # system+tools prefix readable from cache even when the auto-placed
        # message-level cache point misses — see the cachePoint budget comment
        # in ModelConfig.to_bedrock_config. Strands' auto strategy strips only
        # message-level cachePoints, never system ones. Agent.system_prompt
        # remains the plain string (split_system_prompt concatenates the text
        # blocks), so hashing/attribution/voice consumers are unaffected.
        agent_system_prompt: Any = system_prompt
        if system_prompt and model_config.bedrock_cache_points_supported():
            agent_system_prompt = [
                {"text": system_prompt},
                {"cachePoint": {"type": "default"}},
            ]

        # Create agent with session manager, hooks, and system prompt
        # Use SequentialToolExecutor to prevent concurrent browser operations
        # This prevents "Failed to start and initialize Playwright" errors with NovaAct
        agent = Agent(
            model=model,
            system_prompt=agent_system_prompt,
            tools=tools,
            tool_executor=SequentialToolExecutor(),
            session_manager=session_manager,
            hooks=hooks if hooks else None,
            plugins=plugins if plugins else None,
            retry_strategy=retry_strategy,
        )

        return agent
