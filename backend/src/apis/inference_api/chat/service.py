"""Chat feature service layer

Contains business logic for chat operations, including agent creation and management.
"""

import logging
import hashlib
import json
import os
from typing import Optional, List, Tuple
from functools import lru_cache
from datetime import datetime, timezone

import boto3

# from agentcore.agent.agent import ChatbotAgent
from agents.strands_agent.strands_agent import StrandsAgent
from apis.app_api.sessions.models import SessionMetadata
from apis.app_api.sessions.services.metadata import store_session_metadata

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
    caching_enabled: Optional[bool],
    provider: Optional[str],
    max_tokens: Optional[int]
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
        provider: LLM provider
        max_tokens: Maximum tokens to generate

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
        caching_enabled or False,
        provider or "bedrock",
        max_tokens or 0
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
    caching_enabled: Optional[bool] = None,
    provider: Optional[str] = None,
    max_tokens: Optional[int] = None
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
        model_id: Model ID (provider-specific format)
        temperature: Model temperature
        system_prompt: System prompt text
        caching_enabled: Whether to enable prompt caching (Bedrock only)
        provider: LLM provider ("bedrock", "openai", or "gemini")
        max_tokens: Maximum tokens to generate

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
        caching_enabled=caching_enabled,
        provider=provider,
        max_tokens=max_tokens
    )

    # Check cache
    if cache_key in _agent_cache:
        logger.debug(f"✅ Agent cache hit for session {session_id}")
        return _agent_cache[cache_key]

    # Cache miss - create new agent
    logger.debug(f"⚠️ Agent cache miss for session {session_id} - creating new instance")

    # Create agent with multi-provider support
    agent = StrandsAgent(
        session_id=session_id,
        user_id=user_id,
        enabled_tools=enabled_tools,
        model_id=model_id,
        temperature=temperature,
        system_prompt=system_prompt,
        caching_enabled=caching_enabled,
        provider=provider,
        max_tokens=max_tokens
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


# ============================================================
# Title Generation
# ============================================================

# System prompt for title generation optimized for Nova Micro
TITLE_GENERATION_SYSTEM_PROMPT = """You are a precise title generator for conversational AI sessions.

Your role is to analyze a user's initial message and create a concise, descriptive title that captures the essence of their intent or question.

Guidelines:
- Maximum 50 characters (strictly enforced)
- Use clear, specific language
- Avoid generic phrases like "Question about" or "Help with"
- Capture the core topic or action
- Use title case (capitalize major words)
- No quotes, periods, or special formatting

Examples:
Input: "Can you help me write a Python script to parse CSV files and extract specific columns?"
Output: Python CSV Parser Script

Input: "I need to understand how React hooks work, specifically useState and useEffect"
Output: React Hooks: useState & useEffect

Input: "What's the weather like in Tokyo right now?"
Output: Tokyo Weather Query

Input: "Help me debug this error: TypeError: Cannot read property 'map' of undefined"
Output: Debug TypeError Map Error

Focus on being informative and scannable. The title should allow users to quickly identify this conversation in a list."""


async def generate_conversation_title(
    session_id: str,
    user_id: str,
    user_input: str
) -> str:
    """
    Generate a conversation title using AWS Bedrock Nova Micro model.

    This function:
    1. Truncates user input to ~500 tokens (2000 chars as rough approximation)
    2. Calls Nova Micro with optimized system prompt
    3. Updates session metadata both locally and in cloud
    4. Returns generated title or fallback on error

    Args:
        session_id: Session identifier
        user_id: User identifier (from JWT)
        user_input: User's first message (will be truncated if needed)

    Returns:
        str: Generated conversation title (max 50 chars) or "New Conversation" on error
    """
    # Truncate input to approximately 500 tokens (~4 chars per token)
    # This keeps the request fast and cost-effective
    MAX_INPUT_LENGTH = 2000
    truncated_input = user_input[:MAX_INPUT_LENGTH]
    if len(user_input) > MAX_INPUT_LENGTH:
        truncated_input += "..."
        logger.debug(f"Truncated input from {len(user_input)} to {MAX_INPUT_LENGTH} chars")

    try:
        # Initialize Bedrock Runtime client
        bedrock_region = os.environ.get('AWS_REGION', 'us-east-1')
        bedrock_client = boto3.client('bedrock-runtime', region_name=bedrock_region)

        # Prepare request for Nova Micro
        # us.amazon.nova-micro-v1:0 is the fastest, most cost-effective model
        request_body = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": truncated_input}]
                }
            ],
            "system": [{"text": TITLE_GENERATION_SYSTEM_PROMPT}],
            "inferenceConfig": {
                "temperature": 0.3,  # Low temperature for consistent, focused output
                "maxTokens": 50,      # Title should be very short
                "topP": 0.9
            }
        }

        logger.info(f"🎯 Generating title for session {session_id} (input length: {len(truncated_input)} chars)")

        # Call Bedrock Nova Micro
        response = bedrock_client.converse(
            modelId="us.amazon.nova-micro-v1:0",
            messages=request_body["messages"],
            system=request_body["system"],
            inferenceConfig=request_body["inferenceConfig"]
        )

        # Extract generated title from response
        title = response["output"]["message"]["content"][0]["text"].strip()

        # Enforce 50 character limit (just in case model exceeds)
        if len(title) > 50:
            title = title[:47] + "..."
            logger.warning(f"Title exceeded 50 chars, truncated to: {title}")

        logger.info(f"✅ Generated title: '{title}' for session {session_id}")

        # Update session metadata with the generated title
        # This uses the existing metadata storage infrastructure
        # which handles both local file storage and DynamoDB
        now = datetime.now(timezone.utc).isoformat()
        session_metadata = SessionMetadata(
            session_id=session_id,
            user_id=user_id,
            title=title,
            status="active",
            created_at=now,
            last_message_at=now,
            message_count=0,  # Will be updated by streaming handler
            starred=False,
            tags=[],
            preferences=None
        )

        await store_session_metadata(
            session_id=session_id,
            user_id=user_id,
            session_metadata=session_metadata
        )

        logger.info(f"💾 Updated session metadata with title for session {session_id}")

        return title

    except Exception as e:
        # Log error but don't fail the request
        # Title generation is nice-to-have, not critical
        logger.error(f"Failed to generate title for session {session_id}: {e}", exc_info=True)

        # Return fallback title
        fallback_title = "New Conversation"

        # Still try to store metadata with fallback title
        try:
            now = datetime.now(timezone.utc).isoformat()
            session_metadata = SessionMetadata(
                session_id=session_id,
                user_id=user_id,
                title=fallback_title,
                status="active",
                created_at=now,
                last_message_at=now,
                message_count=0,
                starred=False,
                tags=[],
                preferences=None
            )
            await store_session_metadata(
                session_id=session_id,
                user_id=user_id,
                session_metadata=session_metadata
            )
        except Exception as metadata_error:
            logger.error(f"Failed to store fallback metadata: {metadata_error}")

        return fallback_title

