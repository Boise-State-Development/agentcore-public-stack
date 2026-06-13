"""Chat feature service layer

Contains business logic for chat operations, including agent creation and management.
"""

import json
import logging
import hashlib
import os
from typing import Any, Dict, Optional, List, Tuple

import boto3

# from agentcore.agent.agent import ChatbotAgent
from agents.main_agent.agent_types import create_agent
from agents.main_agent.base_agent import BaseAgent
from apis.shared.sessions.metadata import update_session_title

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


def _hash_inference_params(params: Optional[Dict[str, Any]]) -> str:
    """Stable hash of an inference-params dict for the agent cache key."""
    if not params:
        return "none"
    payload = json.dumps(params, sort_keys=True, default=str)
    return hashlib.md5(payload.encode()).hexdigest()[:8]


def _create_cache_key(
    session_id: str,
    user_id: Optional[str],
    enabled_tools: Optional[List[str]],
    model_id: Optional[str],
    inference_params: Optional[Dict[str, Any]],
    system_prompt: Optional[str],
    caching_enabled: Optional[bool],
    provider: Optional[str],
    freshness_hash: str,
    agent_type: Optional[str],
    skills_hash: str = "",
) -> Tuple:
    """
    Create a cache key for agent instances.

    `freshness_hash` is a short digest of the enabled tools' current
    `updated_at` values (see `freshness.get_freshness_hash`). When an
    admin edits a tool's config, the hash changes and the cache misses,
    so the next turn builds a fresh agent with the new config.

    `skills_hash` is the skills analog: a digest of the user's resolved
    accessible skill ids AND their `updated_at` values (skills/freshness).
    A SkillAgent's tool universe is derived from the user's granted skills,
    which `enabled_tools` does not capture — so without this an edit to a
    granted skill (or a role grant change) would serve a stale agent. Empty
    for chat agents (no skills), so the default path is unaffected.
    """
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
        _hash_inference_params(inference_params),
        prompt_hash,
        caching_enabled or False,
        provider or "bedrock",
        freshness_hash,
        agent_type or "chat",
        skills_hash,
    )


# LRU cache for agent instances
# maxsize=100 allows caching up to 100 different agent configurations
# This reduces initialization overhead for repeated requests
_agent_cache: dict = {}
_CACHE_MAX_SIZE = 100


def _is_paused_on_interrupt(agent: BaseAgent) -> bool:
    """Return True if the wrapped Strands agent is mid-interrupt.

    Used by ``get_agent`` to decide whether to evict a stale paused agent
    from the cache. ``getattr`` chains with defaults can never raise, so
    no try/except is needed.
    """
    inner = getattr(agent, "agent", None)
    state = getattr(inner, "_interrupt_state", None)
    return bool(state is not None and getattr(state, "activated", False))


async def get_agent(
    session_id: str,
    user_id: Optional[str] = None,
    auth_token: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    model_id: Optional[str] = None,
    temperature: Optional[float] = None,
    system_prompt: Optional[str] = None,
    caching_enabled: Optional[bool] = None,
    provider: Optional[str] = None,
    max_tokens: Optional[int] = None,
    agent_type: Optional[str] = None,
    extra_tools: Optional[list] = None,
    inference_params: Optional[Dict[str, Any]] = None,
    mantle_endpoint_path: Optional[str] = None,
    is_resume: bool = False,
    accessible_skill_ids: Optional[List[str]] = None,
) -> BaseAgent:
    """
    Get or create agent instance with current configuration for session

    Implements LRU caching to reduce agent initialization overhead.
    Cache key includes all configuration parameters plus a freshness
    hash of the enabled tools' `updated_at` values, so admin edits to a
    tool's config invalidate the cached agent on the next turn.

    Args:
        session_id: Session identifier
        user_id: User identifier (defaults to session_id)
        enabled_tools: List of tool IDs to enable
        model_id: Model ID (provider-specific format)
        temperature: Legacy. Folded into ``inference_params['temperature']``.
        system_prompt: System prompt text
        caching_enabled: Whether to enable prompt caching (Bedrock only)
        provider: LLM provider ("bedrock", "openai", or "gemini")
        max_tokens: Legacy. Folded into ``inference_params['max_tokens']``.
        agent_type: Agent factory variant ("chat" or "skill")
        inference_params: Canonical-name -> value map for inference params.
            When provided, supersedes the legacy ``temperature``/``max_tokens``
            kwargs (the explicit dict wins on key conflicts).

    Returns:
        BaseAgent subclass instance (cached or newly created)
    """
    from apis.shared.tools.freshness import get_freshness_hash

    # Merge legacy temperature/max_tokens into inference_params so the cache
    # key and BaseAgent see the same canonical dict.
    merged_params: Dict[str, Any] = dict(inference_params or {})
    if temperature is not None:
        merged_params.setdefault("temperature", temperature)
    if max_tokens is not None:
        merged_params.setdefault("max_tokens", max_tokens)

    freshness_hash = await get_freshness_hash(enabled_tools or [])

    # Skills dimension of the cache key (skill agents only). Digest of the
    # user's accessible skill ids + their updated_at, so an edit to a granted
    # skill or a role-grant change invalidates the cached SkillAgent. Empty
    # string for the chat path (no accessible_skill_ids) — key unchanged.
    skills_hash = ""
    if accessible_skill_ids:
        from apis.shared.skills.freshness import (
            get_freshness_hash as get_skills_freshness_hash,
        )

        skills_hash = await get_skills_freshness_hash(accessible_skill_ids)

    cache_key = _create_cache_key(
        session_id=session_id,
        user_id=user_id,
        enabled_tools=enabled_tools,
        model_id=model_id,
        inference_params=merged_params,
        system_prompt=system_prompt,
        caching_enabled=caching_enabled,
        provider=provider,
        freshness_hash=freshness_hash,
        agent_type=agent_type,
        skills_hash=skills_hash,
    )

    if not extra_tools and cache_key in _agent_cache:
        cached = _agent_cache[cache_key]
        # Defense in depth: a non-resume request should never be served a
        # paused agent. If we ever desync the cache key between the original
        # turn and a resume (e.g. snapshot stores a normalized form of one
        # of the params), the resume rebuilds under a new key while the
        # paused agent stays in the original slot — and a later non-resume
        # turn cache-hits to it. Strands then raises "must resume from
        # interrupt with list of interruptResponse's". Discard and rebuild.
        if not is_resume and _is_paused_on_interrupt(cached):
            logger.warning(
                "Cached agent is paused on an interrupt but request is not a resume; "
                "evicting and rebuilding (session=%s user=%s)",
                session_id, user_id,
            )
            del _agent_cache[cache_key]
        else:
            logger.debug("✅ Agent cache hit")
            return cached

    # Cache miss - create new agent
    logger.debug("⚠️ Agent cache miss - creating new instance")

    # Create agent via the type registry. Default "chat" preserves the
    # existing MainAgent (= ChatAgent) behavior; "skill" routes through
    # SkillAgent's progressive skill disclosure.
    resolved_agent_type = agent_type or "chat"
    create_kwargs: Dict[str, Any] = dict(
        agent_type=resolved_agent_type,
        session_id=session_id,
        user_id=user_id,
        auth_token=auth_token,
        enabled_tools=enabled_tools,
        model_id=model_id,
        system_prompt=system_prompt,
        caching_enabled=caching_enabled,
        provider=provider,
        max_tokens=max_tokens,
        extra_tools=extra_tools,
        inference_params=merged_params,
        mantle_endpoint_path=mantle_endpoint_path,
    )
    # Only the SkillAgent accepts accessible_skill_ids; ChatAgent's constructor
    # would reject the unknown kwarg, so gate it on the skill type.
    if resolved_agent_type == "skill":
        create_kwargs["accessible_skill_ids"] = accessible_skill_ids
    agent = create_agent(**create_kwargs)

    # Stamp the type onto the construction snapshot so a paused turn can
    # resume on the same factory variant after cache eviction. Skill turns
    # also stamp their effective skill set: resume must rebuild the same
    # skills_hash cache key even if the user toggles skills mid-pause.
    if hasattr(agent, "_construction_snapshot"):
        agent._construction_snapshot["agent_type"] = resolved_agent_type
        if resolved_agent_type == "skill" and accessible_skill_ids is not None:
            agent._construction_snapshot["enabled_skills"] = list(accessible_skill_ids)

    # Don't cache agents with context-bound extra_tools
    if extra_tools:
        logger.debug("⏭️ Skipping cache for agent with extra_tools")
        return agent

    # Add to cache with LRU eviction
    if len(_agent_cache) >= _CACHE_MAX_SIZE:
        # Remove oldest entry (first inserted)
        oldest_key = next(iter(_agent_cache))
        del _agent_cache[oldest_key]
        logger.debug(f"🗑️ Evicted oldest agent from cache (size={_CACHE_MAX_SIZE})")

    _agent_cache[cache_key] = agent
    logger.debug("💾 Cached agent")

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

        logger.info("🎯 Generating title (input length: %d chars)", len(truncated_input))

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
            logger.warning("Title exceeded 50 chars, truncated")

        logger.info("✅ Generated title successfully")

        # Targeted update — only writes the title attribute. The post-stream
        # update_session_activity write is also targeted and disjoint, so the
        # two cannot clobber each other on overlapping turns.
        await update_session_title(session_id=session_id, user_id=user_id, title=title)

        return title

    except Exception as e:
        # Title generation is nice-to-have. Leave the existing "New Conversation"
        # placeholder in place rather than writing a fallback; the row already
        # exists from the pre-create.
        logger.error("Failed to generate title: %s", e, exc_info=True)
        return "New Conversation"

