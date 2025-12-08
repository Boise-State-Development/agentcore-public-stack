"""Chat feature service layer

Contains business logic for chat operations, including agent creation and management.
"""

import logging
import hashlib
from typing import Optional, List, Tuple
from functools import lru_cache

# from agentcore.agent.agent import ChatbotAgent
from agents.strands_agent.strands_agent import StrandsAgent

logger = logging.getLogger(__name__)


def _hash_tools(tools: Optional[List[str]]) -> str:
    """
    Create a stable hash of the enabled tools list for cache key

    Args:
        tools: List of tool names or None

    Returns:
        Hash string for cache key
    """
    if tools is None:
        return "all_tools"

    # Sort to ensure consistent hash regardless of order
    sorted_tools = sorted(tools)
    tools_str = ",".join(sorted_tools)
    return hashlib.md5(tools_str.encode()).hexdigest()[:8]


def _create_cache_key(
    session_id: str,
    user_id: Optional[str],
    enabled_tools: Optional[List[str]],
    model_id: Optional[str],
    temperature: Optional[float],
    system_prompt: Optional[str],
    caching_enabled: Optional[bool]
) -> Tuple:
    """
    Create a cache key for agent instances

    Args:
        session_id: Session identifier
        user_id: User identifier
        enabled_tools: List of enabled tool names
        model_id: Model identifier
        temperature: Model temperature
        system_prompt: System prompt text
        caching_enabled: Whether caching is enabled

    Returns:
        Tuple suitable for use as cache key
    """
    # Hash the tools list for stable key
    tools_hash = _hash_tools(enabled_tools)

    # Hash system prompt if provided (can be very long)
    prompt_hash = None
    if system_prompt:
        prompt_hash = hashlib.md5(system_prompt.encode()).hexdigest()[:8]

    return (
        session_id,
        user_id or session_id,
        tools_hash,
        model_id or "default",
        temperature or 0.0,
        prompt_hash,
        caching_enabled or False
    )


# LRU cache for agent instances
# maxsize=100 allows caching up to 100 different agent configurations
# This reduces initialization overhead for repeated requests
_agent_cache: dict = {}
_CACHE_MAX_SIZE = 100


def get_agent(
    session_id: str,
    user_id: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    model_id: Optional[str] = None,
    temperature: Optional[float] = None,
    system_prompt: Optional[str] = None,
    caching_enabled: Optional[bool] = None
) -> StrandsAgent:
    """
    Get or create agent instance with current configuration for session

    Implements LRU caching to reduce agent initialization overhead.
    Cache key includes all configuration parameters to ensure correct behavior.
    Session message history is managed by AgentCore Memory automatically.

    Args:
        session_id: Session identifier
        user_id: User identifier (defaults to session_id)
        enabled_tools: List of tool IDs to enable
        model_id: Bedrock model ID
        temperature: Model temperature
        system_prompt: System prompt text
        caching_enabled: Whether to enable prompt caching

    Returns:
        StrandsAgent instance (cached or newly created)
    """
    # Create cache key from all configuration parameters
    cache_key = _create_cache_key(
        session_id=session_id,
        user_id=user_id,
        enabled_tools=enabled_tools,
        model_id=model_id,
        temperature=temperature,
        system_prompt=system_prompt,
        caching_enabled=caching_enabled
    )

    # Check cache
    if cache_key in _agent_cache:
        logger.debug(f"✅ Agent cache hit for session {session_id}")
        return _agent_cache[cache_key]

    # Cache miss - create new agent
    logger.debug(f"⚠️ Agent cache miss for session {session_id} - creating new instance")

    # Create agent with AgentCore Memory - messages and preferences automatically loaded/saved
    agent = StrandsAgent(
        session_id=session_id,
        user_id=user_id,
        enabled_tools=enabled_tools,
        model_id=model_id,
        temperature=temperature,
        system_prompt=system_prompt,
        caching_enabled=caching_enabled
    )

    # Add to cache with LRU eviction
    if len(_agent_cache) >= _CACHE_MAX_SIZE:
        # Remove oldest entry (first inserted)
        oldest_key = next(iter(_agent_cache))
        del _agent_cache[oldest_key]
        logger.debug(f"🗑️ Evicted oldest agent from cache (size={_CACHE_MAX_SIZE})")

    _agent_cache[cache_key] = agent
    logger.debug(f"💾 Cached agent for session {session_id} (cache size={len(_agent_cache)})")

    return agent


def clear_agent_cache():
    """
    Clear the agent cache

    Useful for testing or when configuration changes require cache invalidation.
    """
    global _agent_cache
    _agent_cache = {}
    logger.info("🗑️ Agent cache cleared")

