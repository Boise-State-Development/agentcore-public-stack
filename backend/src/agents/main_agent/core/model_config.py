"""
Model configuration for multi-provider LLM support (Bedrock, OpenAI, Gemini)
"""
import os
from typing import Dict, Any, Optional, Literal
from dataclasses import dataclass, field
from enum import Enum


class ModelProvider(str, Enum):
    """Supported LLM providers"""
    BEDROCK = "bedrock"
    OPENAI = "openai"
    GEMINI = "gemini"


@dataclass
class RetryConfig:
    """Configuration for model invocation retry behavior.

    Controls two independent retry layers:
    1. Botocore layer - HTTP-level retries before the Strands SDK sees errors
    2. Strands SDK layer - Agent event loop retries on ModelThrottledException

    When all retries are exhausted, the exception propagates to StreamCoordinator
    which streams it to the client as a conversational error message.

    Can be loaded from environment variables or passed directly.
    """
    # Botocore layer (HTTP-level retries, fires first)
    boto_max_attempts: int = 3          # Total attempts including initial call
    boto_retry_mode: str = "standard"   # "legacy", "standard", or "adaptive"
    connect_timeout: int = 5            # Seconds to wait for connection
    read_timeout: int = 120             # Seconds to wait for response

    # Strands SDK layer (agent event loop retries on ModelThrottledException)
    # Backoff sequence with defaults: 2s, 4s, 8s (3 retries before giving up)
    # Total worst-case wait: ~14s — fast enough for conversational UX
    sdk_max_attempts: int = 4           # Total attempts including initial call
    sdk_initial_delay: float = 2.0      # Seconds before first retry, doubles each retry
    sdk_max_delay: float = 16.0         # Cap on exponential backoff

    @classmethod
    def from_env(cls) -> "RetryConfig":
        """Load configuration from environment variables.

        Environment variables (all optional, defaults shown):
            RETRY_BOTO_MAX_ATTEMPTS=3
            RETRY_BOTO_MODE=standard
            RETRY_CONNECT_TIMEOUT=5
            RETRY_READ_TIMEOUT=120
            RETRY_SDK_MAX_ATTEMPTS=4
            RETRY_SDK_INITIAL_DELAY=2.0
            RETRY_SDK_MAX_DELAY=16.0
        """
        return cls(
            boto_max_attempts=int(os.environ.get("RETRY_BOTO_MAX_ATTEMPTS", "3")),
            boto_retry_mode=os.environ.get("RETRY_BOTO_MODE", "standard"),
            connect_timeout=int(os.environ.get("RETRY_CONNECT_TIMEOUT", "5")),
            read_timeout=int(os.environ.get("RETRY_READ_TIMEOUT", "120")),
            sdk_max_attempts=int(os.environ.get("RETRY_SDK_MAX_ATTEMPTS", "4")),
            sdk_initial_delay=float(os.environ.get("RETRY_SDK_INITIAL_DELAY", "2.0")),
            sdk_max_delay=float(os.environ.get("RETRY_SDK_MAX_DELAY", "16.0")),
        )


@dataclass
class ModelConfig:
    """Configuration for multi-provider LLM models"""
    model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    temperature: float = 0.7
    caching_enabled: bool = True
    provider: ModelProvider = ModelProvider.BEDROCK
    max_tokens: Optional[int] = None
    retry_config: Optional[RetryConfig] = None

    def get_provider(self) -> ModelProvider:
        """
        Detect provider from model_id if not explicitly set

        Returns:
            ModelProvider: Detected or configured provider
        """
        # Auto-detect from model_id patterns
        model_lower = self.model_id.lower()

        # Check if provider was explicitly set (not default)
        # If provider is set to non-Bedrock, return it immediately
        if self.provider != ModelProvider.BEDROCK:
            return self.provider

        # If provider is Bedrock (default), check if we should auto-detect
        if model_lower.startswith("gpt-") or model_lower.startswith("o1-"):
            return ModelProvider.OPENAI
        elif model_lower.startswith("gemini-"):
            return ModelProvider.GEMINI
        elif "anthropic" in model_lower or "claude" in model_lower:
            return ModelProvider.BEDROCK

        # Default to configured provider
        return self.provider

    def to_bedrock_config(self) -> Dict[str, Any]:
        """
        Convert to BedrockModel configuration dictionary

        Returns:
            dict: Configuration for BedrockModel initialization
        """
        from strands.models import CacheConfig

        config = {
            "model_id": self.model_id,
            "temperature": self.temperature
        }

        # TODO: Re-enable once Bedrock supports cachePoint blocks alongside
        # non-PDF document blocks (.md, .docx, etc.). Currently causes:
        # ValidationException: messages.N.content.M.type: Field required
        # because Bedrock can't translate cachePoint after document blocks
        # to the Anthropic format.
        # See: https://github.com/strands-agents/sdk-python/pull/1438
        # if self.caching_enabled:
        #     config["cache_config"] = CacheConfig(strategy="auto")

        # Configure botocore-level retries and timeouts for Bedrock API calls
        # This is the first retry layer (HTTP-level), fires before Strands SDK retries
        if self.retry_config:
            from botocore.config import Config as BotocoreConfig
            config["boto_client_config"] = BotocoreConfig(
                retries={
                    "max_attempts": self.retry_config.boto_max_attempts,
                    "mode": self.retry_config.boto_retry_mode,
                },
                connect_timeout=self.retry_config.connect_timeout,
                read_timeout=self.retry_config.read_timeout,
            )

        return config

    def to_openai_config(self) -> Dict[str, Any]:
        """
        Convert to OpenAI configuration dictionary

        Returns:
            dict: Configuration for OpenAIModel initialization
        """
        config = {
            "model_id": self.model_id,
            "params": {
                "temperature": self.temperature,
            }
        }

        if self.max_tokens:
            config["params"]["max_tokens"] = self.max_tokens

        return config

    def to_gemini_config(self) -> Dict[str, Any]:
        """
        Convert to Gemini configuration dictionary

        Returns:
            dict: Configuration for GeminiModel initialization
        """
        config = {
            "model_id": self.model_id,
            "params": {
                "temperature": self.temperature,
            }
        }

        if self.max_tokens:
            config["params"]["max_output_tokens"] = self.max_tokens

        return config

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary representation

        Returns:
            dict: Configuration as dictionary
        """
        return {
            "model_id": self.model_id,
            "temperature": self.temperature,
            "caching_enabled": self.caching_enabled,
            "provider": self.get_provider().value,
            "max_tokens": self.max_tokens
        }

    @classmethod
    def from_params(
        cls,
        model_id: Optional[str] = None,
        temperature: Optional[float] = None,
        caching_enabled: Optional[bool] = None,
        provider: Optional[str] = None,
        max_tokens: Optional[int] = None
    ) -> "ModelConfig":
        """
        Create ModelConfig from optional parameters

        Args:
            model_id: Model ID (provider-specific format)
            temperature: Model temperature (0.0 - 1.0)
            caching_enabled: Whether to enable prompt caching (Bedrock only)
            provider: Provider name ("bedrock", "openai", or "gemini")
            max_tokens: Maximum tokens to generate

        Returns:
            ModelConfig: Configuration instance with defaults applied
        """
        # Parse provider
        provider_enum = ModelProvider.BEDROCK
        if provider:
            try:
                provider_enum = ModelProvider(provider.lower())
            except ValueError:
                # Invalid provider, will auto-detect from model_id
                pass

        return cls(
            model_id=model_id or cls.model_id,
            temperature=temperature if temperature is not None else cls.temperature,
            caching_enabled=caching_enabled if caching_enabled is not None else cls.caching_enabled,
            provider=provider_enum,
            max_tokens=max_tokens
        )
