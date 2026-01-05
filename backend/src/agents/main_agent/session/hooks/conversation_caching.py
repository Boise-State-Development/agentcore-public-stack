"""Hook to add a single cache point to conversation history before model calls

This hook implements prompt caching for AWS Bedrock Claude models, which provides
significant cost and latency benefits for conversational AI applications.

Benefits:
- Cost Reduction: Cached tokens are billed at ~90% lower cost than regular input tokens
- Latency Improvement: Cached content doesn't need to be re-processed, reducing response time
- Token Efficiency: Reduces the effective token count for long conversations
- Better UX: Faster responses, especially in multi-turn conversations

Model Compatibility:
- Claude models on AWS Bedrock:
  * Claude 3.5 Sonnet, Claude 3 Opus, Claude 3 Haiku, Claude 3.7 Sonnet, Claude Haiku 4.5
- Amazon Nova models on AWS Bedrock:
  * Nova Micro, Nova Lite, Nova Pro, Nova Premier
  * Note: Nova has automatic caching, but explicit cache points unlock cost savings
  * Note: Nova does not support tool definition caching (messages only)
- NOT supported: Llama, Mistral, Titan, and other non-Claude/Nova models
- Requires Bedrock API version that supports prompt caching (2023-09-30 or later)

Strategy: Single Cache Point at End of Last Assistant Message
- A cache point means "cache everything up to this point"
- Placing one CP at the end of the last assistant message caches the entire conversation
- Works universally for: pure conversation, tool loops, and mixed scenarios
- Multiple cache points cause DUPLICATE write premiums (25% each)
- Testing showed 1 CP performs equally to 3 CPs but avoids redundant costs
"""

import logging
from typing import Any, Optional
from strands.hooks import HookProvider, HookRegistry, BeforeModelCallEvent

logger = logging.getLogger(__name__)

# Models that support prompt caching with cachePoint field
# Claude models (Anthropic on Bedrock)
CACHING_SUPPORTED_PATTERNS = [
    "anthropic.claude",      # All Claude models
    "us.anthropic.claude",   # Cross-region Claude models
    "amazon.nova",           # All Nova models
    "us.amazon.nova",        # Cross-region Nova models
]


def is_caching_supported(model_id: Optional[str]) -> bool:
    """Check if the model supports prompt caching with cachePoint field.

    Args:
        model_id: The model ID to check

    Returns:
        bool: True if the model supports caching, False otherwise
    """
    if not model_id:
        return False

    model_lower = model_id.lower()
    return any(pattern in model_lower for pattern in CACHING_SUPPORTED_PATTERNS)


class ConversationCachingHook(HookProvider):
    """Hook to add a single cache point at the end of the last assistant message

    Strategy: Single Cache Point at End of Last Assistant Message

    Key insight: A cache point means "cache everything up to this point".
    Placing the CP at the end of the last assistant message works for:
    - Pure conversation (no tools)
    - Agent loops with tool calls
    - Mixed scenarios

    Benefits:
    - Cost Savings: Cached tokens cost ~90% less than regular input tokens
    - Avoids Duplicate Premiums: Multiple CPs cause 25% write premium each
    - Simpler Logic: Single cache point eliminates sliding window complexity
    - Same Performance: Testing showed 1 CP performs equally to 3 CPs

    Model Compatibility:
    - Claude: 3.5 Sonnet, 3 Opus, 3 Haiku, 3.7 Sonnet, Haiku 4.5
    - Amazon Nova: Micro, Lite, Pro, Premier (messages only, no tool caching)
    - NOT supported: Llama, Mistral, Titan, and other models
    - Requires Bedrock API version 2023-09-30 or later
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.add_conversation_cache_point)

    def add_conversation_cache_point(self, event: BeforeModelCallEvent) -> None:
        """Add single cache point at the end of the last assistant message

        This method implements a simple 6-step caching strategy:
        0. Check if the model supports caching (Claude/Nova only)
        1. Find all existing cache points and the last assistant message
        2. Early return if no assistant message exists
        3. Check if cache point already exists at target location
        4. Remove ALL existing cache points (reverse order to avoid index issues)
        5. Append single cache point to end of last assistant content
        """
        if not self.enabled:
            return

        # Step 0: Check if model supports caching
        # Get model_id from the agent's model if available
        model_id = None
        if hasattr(event.agent, 'model') and hasattr(event.agent.model, 'config'):
            model_id = getattr(event.agent.model.config, 'model_id', None)
        elif hasattr(event.agent, 'model') and hasattr(event.agent.model, 'model_id'):
            model_id = event.agent.model.model_id

        if not is_caching_supported(model_id):
            logger.debug(f"Model {model_id} does not support caching - skipping cache point")
            return

        messages = event.agent.messages
        if not messages:
            return

        # Step 1: Find all existing cache points and the last assistant message
        cache_point_positions = []  # [(msg_idx, block_idx), ...]
        last_assistant_idx = None

        for msg_idx, msg in enumerate(messages):
            # Track last assistant message
            if msg.get("role") == "assistant":
                last_assistant_idx = msg_idx

            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            for block_idx, block in enumerate(content):
                if isinstance(block, dict) and "cachePoint" in block:
                    cache_point_positions.append((msg_idx, block_idx))

        # Step 2: If no assistant message yet, nothing to cache
        if last_assistant_idx is None:
            logger.debug("No assistant message in conversation - skipping cache point")
            return

        last_assistant_content = messages[last_assistant_idx].get("content", [])
        if not isinstance(last_assistant_content, list) or len(last_assistant_content) == 0:
            logger.debug("Last assistant message has no content - skipping cache point")
            return

        # Step 3: Check if cache point already exists at the end of last assistant message
        last_block = last_assistant_content[-1]
        if isinstance(last_block, dict) and "cachePoint" in last_block:
            logger.debug("Cache point already exists at end of last assistant message")
            return

        # Step 4: Remove ALL existing cache points (we only want 1 at the end)
        # Process in reverse order to avoid index shifting issues
        for msg_idx, block_idx in reversed(cache_point_positions):
            msg_content = messages[msg_idx].get("content", [])
            if isinstance(msg_content, list) and block_idx < len(msg_content):
                del msg_content[block_idx]
                logger.debug(f"Removed old cache point at msg {msg_idx} block {block_idx}")

        # Step 5: Add single cache point at the end of the last assistant message
        cache_block = {"cachePoint": {"type": "default"}}

        # Re-fetch content in case it was modified by deletion
        last_assistant_content = messages[last_assistant_idx].get("content", [])
        if isinstance(last_assistant_content, list):
            last_assistant_content.append(cache_block)
            logger.debug(f"Added cache point at end of assistant message {last_assistant_idx}")

