"""Hook to add cache points to conversation history before model calls

This hook implements prompt caching for AWS Bedrock Claude models, which provides
significant cost and latency benefits for conversational AI applications.

Benefits:
- Cost Reduction: Cached tokens are billed at ~90% lower cost than regular input tokens
- Latency Improvement: Cached content doesn't need to be re-processed, reducing response time
- Token Efficiency: Reduces the effective token count for long conversations
- Better UX: Faster responses, especially in multi-turn conversations

Model Compatibility:
- Works with Claude models on AWS Bedrock that support prompt caching:
  * Claude 3.5 Sonnet (us.anthropic.claude-3-5-sonnet-*)
  * Claude 3 Opus (us.anthropic.claude-3-opus-*)
  * Claude 3 Haiku (us.anthropic.claude-3-haiku-*)
  * Claude 3.7 Sonnet (us.anthropic.claude-3-7-sonnet-*)
  * Claude Haiku 4.5 (us.anthropic.claude-haiku-4-5-*)
- Requires Bedrock API version that supports prompt caching (2023-09-30 or later)
- System prompt caching is handled separately by BedrockModel configuration

How It Works:
- Bedrock allows up to 4 cache breakpoints per request (1 system + 3 conversation)
- This hook manages the 3 conversation cache points using a sliding window strategy
- Cache points are placed after assistant responses and tool results (most valuable content)
- When the 3-point limit is reached, the oldest cache point is removed (sliding window)
- This ensures the most recent conversation turns are always cached for optimal efficiency
"""

import logging
from typing import Any
from strands.hooks import HookProvider, HookRegistry, BeforeModelCallEvent

logger = logging.getLogger(__name__)


class ConversationCachingHook(HookProvider):
    """Hook to add cache points to conversation history before model calls

    This hook implements intelligent prompt caching for Claude models on AWS Bedrock.
    It strategically places cache points in conversation history to maximize cost savings
    and reduce latency while staying within Bedrock's 4 cache point limit.

    Benefits:
    - Cost Savings: Cached tokens cost ~90% less than regular input tokens
    - Latency Reduction: Cached content is processed instantly, improving response times
    - Token Efficiency: Reduces effective token usage in long conversations
    - Better Performance: Especially beneficial for multi-turn conversations with tool usage

    Model Compatibility:
    - Claude 3.5 Sonnet, Claude 3 Opus, Claude 3 Haiku, Claude 3.7 Sonnet, Claude Haiku 4.5
    - Any Claude model on AWS Bedrock that supports prompt caching
    - Requires Bedrock API version 2023-09-30 or later

    Strategy:
    - Maintain 3 cache points in conversation (sliding window)
    - Prioritize recent assistant messages and tool results
    - When limit reached, remove oldest cache point and add new one
    - Combined with system prompt cache = 4 total cache breakpoints (Claude/Bedrock limit)
    - Sliding cache points keep the most recent turns cached for optimal efficiency
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.add_conversation_cache_point)

    def add_conversation_cache_point(self, event: BeforeModelCallEvent) -> None:
        """Add cache points to conversation history with sliding window (max 3, remove oldest when full)
        
        This method implements a sliding window cache strategy:
        1. Counts existing cache points (max 3 allowed by Bedrock)
        2. If at limit, removes the oldest cache point to make room
        3. Identifies optimal cache locations (assistant responses, tool results)
        4. Adds new cache points after the most valuable content blocks
        
        The sliding window ensures we always cache the most recent conversation turns,
        which provides the best cost/performance benefits since recent context is most
        likely to be reused in subsequent model calls.
        """
        if not self.enabled:
            logger.info("❌ Caching disabled")
            return

        messages = event.agent.messages
        if not messages:
            logger.info("❌ No messages in history")
            return

        logger.info(f"🔍 Processing caching for {len(messages)} messages")

        # Count existing cache points across all content blocks
        # Bedrock allows up to 3 conversation cache points (plus 1 system prompt cache)
        existing_cache_count = 0
        cache_point_positions = []

        for msg_idx, msg in enumerate(messages):
            content = msg.get("content", [])
            if isinstance(content, list):
                for block_idx, block in enumerate(content):
                    if isinstance(block, dict) and "cachePoint" in block:
                        existing_cache_count += 1
                        cache_point_positions.append((msg_idx, block_idx))

        # If we already have 3 cache points, remove the oldest one (sliding window)
        if existing_cache_count >= 3:
            logger.info(f"📊 Cache limit reached: {existing_cache_count}/3 cache points")
            # Remove the oldest cache point to make room for new one
            if cache_point_positions:
                oldest_msg_idx, oldest_block_idx = cache_point_positions[0]
                oldest_msg = messages[oldest_msg_idx]
                oldest_content = oldest_msg.get("content", [])
                if isinstance(oldest_content, list) and oldest_block_idx < len(oldest_content):
                    # Remove the cache point block
                    del oldest_content[oldest_block_idx]
                    oldest_msg["content"] = oldest_content
                    existing_cache_count -= 1
                    logger.info(f"♻️  Removed oldest cache point at message {oldest_msg_idx} block {oldest_block_idx}")
                    # Update positions for remaining cache points
                    cache_point_positions.pop(0)

        # Strategy: Prioritize assistant messages, then tool_result blocks
        # This ensures every assistant turn gets cached, with or without tools

        assistant_candidates = []
        tool_result_candidates = []

        for msg_idx, msg in enumerate(messages):
            msg_role = msg.get("role", "")
            content = msg.get("content", [])

            if isinstance(content, list) and len(content) > 0:
                # For assistant messages: cache after reasoning/response (priority)
                if msg_role == "assistant":
                    last_block = content[-1]
                    has_cache = isinstance(last_block, dict) and "cachePoint" in last_block
                    if not has_cache:
                        assistant_candidates.append((msg_idx, len(content) - 1, "assistant"))

                # For user messages: cache after tool_result blocks (secondary)
                elif msg_role == "user":
                    for block_idx, block in enumerate(content):
                        if isinstance(block, dict) and "toolResult" in block:
                            has_cache = "cachePoint" in block
                            if not has_cache:
                                tool_result_candidates.append((msg_idx, block_idx, "tool_result"))

        remaining_slots = 3 - existing_cache_count
        logger.info(f"📊 Cache status: {existing_cache_count}/3 existing, {len(assistant_candidates)} assistant + {len(tool_result_candidates)} tool_result candidates, {remaining_slots} slots available")

        # Prioritize assistant messages: take most recent assistants first, then tool_results
        candidates_to_cache = []
        if remaining_slots > 0:
            # Take recent assistant messages first
            num_assistants = min(len(assistant_candidates), remaining_slots)
            if num_assistants > 0:
                candidates_to_cache.extend(assistant_candidates[-num_assistants:])
                remaining_slots -= num_assistants

            # Fill remaining slots with tool_results
            if remaining_slots > 0 and tool_result_candidates:
                num_tool_results = min(len(tool_result_candidates), remaining_slots)
                candidates_to_cache.extend(tool_result_candidates[-num_tool_results:])

        if candidates_to_cache:

            for msg_idx, block_idx, block_type in candidates_to_cache:
                msg = messages[msg_idx]
                content = msg.get("content", [])

                # Safety check: content must be a list and not empty
                if not isinstance(content, list):
                    logger.warning(f"⚠️  Skipping cache point: content is not a list at message {msg_idx}")
                    continue

                if len(content) == 0:
                    logger.warning(f"⚠️  Skipping cache point: content is empty at message {msg_idx}")
                    continue

                if block_idx >= len(content):
                    logger.warning(f"⚠️  Skipping cache point: block_idx {block_idx} out of range at message {msg_idx}")
                    continue

                block = content[block_idx]

                # For dict blocks (toolResult, text, etc.), add cachePoint as separate block after it
                if isinstance(block, dict):
                    # Safety: Don't insert cachePoint at the beginning of next message
                    # Only insert within the same message's content array
                    cache_block = {"cachePoint": {"type": "default"}}
                    insert_position = block_idx + 1

                    # Insert cache point after the current block
                    content.insert(insert_position, cache_block)
                    msg["content"] = content
                    existing_cache_count += 1
                    logger.info(f"✅ Added cache point after {block_type} at message {msg_idx} block {block_idx} (total: {existing_cache_count}/3)")

                elif isinstance(block, str):
                    # Convert string to structured format with cache
                    msg["content"] = [
                        {"text": block},
                        {"cachePoint": {"type": "default"}}
                    ]
                    existing_cache_count += 1
                    logger.info(f"✅ Added cache point after text at message {msg_idx} (total: {existing_cache_count}/3)")

                if existing_cache_count >= 3:
                    break

