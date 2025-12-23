"""
Session Manager Wrapper for AgentCore Memory

This wrapper provides:
1. Message count tracking (avoids eventual consistency issues with AgentCore Memory)
2. Proper hook registration (ensures our callbacks are used, not the base manager's)
3. Session cancellation support

IMPORTANT: This wrapper does NOT buffer or merge messages. Each message is persisted
individually to AgentCore Memory with its correct role. The Converse API requires
messages to maintain their individual roles (user, assistant) - merging them breaks
conversation reconstruction.
"""

import logging
from typing import Optional, Dict, Any, List, Union
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig

logger = logging.getLogger(__name__)


class TurnBasedSessionManager:
    """
    Wrapper around AgentCoreMemorySessionManager that provides:
    - Message count tracking (initialized once at startup to avoid eventual consistency issues)
    - Proper hook registration (intercepts MessageAddedEvent to track count)
    - Session cancellation support

    NOTE: Despite the name, this class no longer performs turn-based buffering/merging.
    Messages are passed through to the base manager individually to preserve correct
    role attribution required by the Converse API.
    """

    def __init__(
        self,
        agentcore_memory_config: AgentCoreMemoryConfig,
        region_name: str = "us-west-2",
        batch_size: int = 5,  # Kept for backward compatibility, not used
        max_buffer_size: int = 20  # Kept for backward compatibility, not used
    ):
        self.base_manager = AgentCoreMemorySessionManager(
            agentcore_memory_config=agentcore_memory_config,
            region_name=region_name
        )

        self.cancelled = False  # Flag to stop accepting new messages

        # Message count tracking to avoid eventual consistency issues
        # Initialize by querying AgentCore Memory once at startup
        self.message_count: int = self._initialize_message_count()

        logger.info(
            f"✅ TurnBasedSessionManager initialized "
            f"(pass-through mode, initial_message_count={self.message_count})"
        )

    def _initialize_message_count(self) -> int:
        """
        Initialize message count by querying AgentCore Memory once at startup.

        This avoids eventual consistency issues during active streaming by only
        querying at initialization when we're not racing with concurrent writes.

        Returns:
            Initial message count (0 if session is new or if query fails)
        """
        try:
            messages = self.base_manager.list_messages(
                self.base_manager.config.session_id,
                "default"  # agent_id
            )
            initial_count = len(messages) if messages else 0
            logger.info(f"📊 Initialized message count from AgentCore Memory: {initial_count}")
            return initial_count
        except Exception as e:
            logger.warning(f"Failed to initialize message count from AgentCore Memory: {e}, defaulting to 0")
            return 0

    def flush(self) -> Optional[int]:
        """
        Flush is now a no-op since we don't buffer messages.
        Returns the current message count - 1 (0-based index of last message).

        Returns:
            Message ID of the last message, or None if no messages exist
        """
        if self.message_count > 0:
            return self.message_count - 1
        return None

    def append_message(self, message, agent, **kwargs):
        """
        Pass message through to base manager and track message count.

        This intercepts the Strands framework's append_message call to:
        1. Check if session is cancelled
        2. Increment our message count (for tracking without querying AgentCore Memory)
        3. Delegate actual persistence to the base manager

        Args:
            message: Message from Strands framework
            agent: Agent instance
            **kwargs: Additional arguments
        """
        # If cancelled, don't accept new messages
        if self.cancelled:
            logger.warning(f"🚫 Session cancelled, ignoring message (role={message.get('role')})")
            return

        # Delegate to base manager for actual persistence
        self.base_manager.append_message(message, agent, **kwargs)

        # Increment our message count
        self.message_count += 1

        role = message.get("role", "unknown")
        logger.debug(f"📝 Message persisted (role={role}, total_count={self.message_count})")

    def register_hooks(self, registry, **kwargs):
        """
        Register hooks with the Strands Agent framework.

        CRITICAL: This method MUST be defined on the wrapper class to prevent
        the base manager from registering its own hooks. If we delegate to
        base_manager.register_hooks(), the base manager will register callbacks
        that point to ITS append_message method, bypassing our message count tracking.

        We register OUR append_message as the callback for MessageAddedEvent.
        Our append_message delegates to the base manager but also tracks the count.

        Args:
            registry: HookRegistry from Strands framework
            **kwargs: Additional arguments
        """
        from strands.hooks import (
            AgentInitializedEvent,
            MessageAddedEvent,
            AfterInvocationEvent
        )

        logger.info("🔗 TurnBasedSessionManager registering hooks (intercepting MessageAddedEvent for count tracking)")

        # Register initialization hook - delegate to base manager
        registry.add_callback(
            AgentInitializedEvent,
            lambda event: self.base_manager.initialize(event.agent)
        )

        # Register message added hook - use OUR append_message (for count tracking)
        # Our append_message delegates to base_manager.append_message but also increments count
        registry.add_callback(
            MessageAddedEvent,
            lambda event: self.append_message(event.message, event.agent)
        )

        # Register sync hooks - delegate to base manager
        registry.add_callback(
            MessageAddedEvent,
            lambda event: self.base_manager.sync_agent(event.agent)
        )

        registry.add_callback(
            AfterInvocationEvent,
            lambda event: self.base_manager.sync_agent(event.agent)
        )

        # CRITICAL: Register retrieve_customer_context hook for long-term memory retrieval
        # This queries the configured namespaces (preferences, facts) and injects
        # relevant memories as <user_context> into the conversation when a user message is added
        registry.add_callback(
            MessageAddedEvent,
            lambda event: self.base_manager.retrieve_customer_context(event)
        )

        logger.info("✅ TurnBasedSessionManager hooks registered successfully (including LTM retrieval)")

    # Delegate all other methods to base manager
    def __getattr__(self, name):
        """Delegate unknown methods to base AgentCore session manager"""
        return getattr(self.base_manager, name)
