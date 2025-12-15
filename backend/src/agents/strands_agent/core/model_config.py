"""
Model configuration for multi-provider LLM support (Bedrock, OpenAI, Gemini)
"""
from typing import Dict, Any, Optional, Literal
from dataclasses import dataclass
from enum import Enum


class ModelProvider(str, Enum):
    """Supported LLM providers"""
    BEDROCK = "bedrock"
    OPENAI = "openai"
    GEMINI = "gemini"


@dataclass
class ModelConfig:
    """Configuration for multi-provider LLM models"""
    model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    temperature: float = 0.7
    caching_enabled: bool = True
    provider: ModelProvider = ModelProvider.BEDROCK
    max_tokens: Optional[int] = None

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
        config = {
            "model_id": self.model_id,
            "temperature": self.temperature
        }

        # Add cache_prompt if caching is enabled (BedrockModel handles SystemContentBlock formatting)
        if self.caching_enabled:
            config["cache_prompt"] = "default"

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
