"""
Strands Agent - Modular multi-agent orchestration system

This package provides a well-architected agent implementation with clear
separation of concerns across specialized modules:

- core: Model configuration and agent factory
- session: Session management and hooks
- tools: Tool registry, filtering, and gateway integration
- multimodal: Image and document content handling
- streaming: Response streaming coordination
- quota: Usage quota management and enforcement
- utils: Shared utilities (timezone, global state)

Main entry point:
    from agents.strands_agent import StrandsAgent

    agent = StrandsAgent(
        session_id="session-123",
        user_id="user-456",
        enabled_tools=["calculator", "weather", "gateway_wikipedia"],
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        temperature=0.7,
        caching_enabled=True
    )

    async for event in agent.stream_async("What's the weather in Seattle?"):
        print(event)
"""
from .strands_agent import StrandsAgent
from .core import ModelConfig, SystemPromptBuilder
from .session import SessionFactory
from .tools import ToolRegistry, ToolFilter, GatewayIntegration, create_default_registry
from .multimodal import PromptBuilder, ImageHandler, DocumentHandler, FileSanitizer
from .streaming import StreamCoordinator
from .quota import (
    QuotaTier,
    QuotaAssignment,
    QuotaAssignmentType,
    QuotaEvent,
    QuotaCheckResult,
    ResolvedQuota,
    QuotaRepository,
    QuotaResolver,
    QuotaChecker,
    QuotaEventRecorder,
)
from .utils import get_current_date_pacific, get_global_stream_processor

__version__ = "1.0.0"

__all__ = [
    # Main agent
    "StrandsAgent",

    # Core components
    "ModelConfig",
    "SystemPromptBuilder",

    # Session management
    "SessionFactory",

    # Tool management
    "ToolRegistry",
    "ToolFilter",
    "GatewayIntegration",
    "create_default_registry",

    # Multimodal
    "PromptBuilder",
    "ImageHandler",
    "DocumentHandler",
    "FileSanitizer",

    # Streaming
    "StreamCoordinator",

    # Quota management
    "QuotaTier",
    "QuotaAssignment",
    "QuotaAssignmentType",
    "QuotaEvent",
    "QuotaCheckResult",
    "ResolvedQuota",
    "QuotaRepository",
    "QuotaResolver",
    "QuotaChecker",
    "QuotaEventRecorder",

    # Utils
    "get_current_date_pacific",
    "get_global_stream_processor",
]
