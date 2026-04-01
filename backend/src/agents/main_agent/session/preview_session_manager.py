"""
Preview Session Manager - In-memory session storage for assistant preview

This session manager maintains conversation history in memory for multi-turn
context within a preview session, but does NOT persist to permanent storage.

Used for assistant preview/testing in the form builder where users want to
test their assistant's behavior without cluttering their conversation history.
"""

import logging
from typing import List, Any
from strands.types.session import SessionMessage

logger = logging.getLogger(__name__)

# Preview session prefix - sessions with this prefix use in-memory storage only
PREVIEW_SESSION_PREFIX = "preview-"


def is_preview_session(session_id: str) -> bool:
    """Check if a session ID is a preview session (in-memory only, no persistence).

    Preview sessions are used for assistant testing in the form builder.
    They maintain conversation context within the session but don't save
    to the user's permanent conversation history.
    """
    return session_id.startswith(PREVIEW_SESSION_PREFIX)


class PreviewSessionManager:
    """
    In-memory session manager for preview sessions.

    Maintains conversation history in memory for multi-turn context,
    but does NOT persist to AgentCore Memory or file storage.

    This allows preview conversations to:
    - Have multi-turn context (assistant remembers previous messages in session)
    - NOT appear in user's conversation history
    - NOT count toward any usage quotas for stored messages
    """

    def __init__(self, session_id: str, user_id: str):
        """
        Initialize preview session manager.

        Args:
            session_id: Session identifier (should start with 'preview-')
            user_id: User identifier
        """
        self.session_id = session_id
        self.user_id = user_id
        self._messages: List[SessionMessage] = []
        self._message_index = 0

        logger.info("🔍 Preview session manager initialized")
        logger.info("   • In-memory storage only (no persistence)")
        logger.info("   • Multi-turn context: Enabled")

    def read_session(self, session_id: str, window_id: str = "default") -> List[SessionMessage]:
        """
        Read messages from the in-memory session.

        Args:
            session_id: Session identifier
            window_id: Window identifier (ignored for preview)

        Returns:
            List of session messages
        """
        logger.debug(f"🔍 Preview: Reading {len(self._messages)} messages from memory")
        return self._messages.copy()

    def create_message(self, session_id: str, window_id: str, message: SessionMessage) -> None:
        """
        Add a message to the in-memory session.

        Args:
            session_id: Session identifier
            window_id: Window identifier (ignored for preview)
            message: Message to add
        """
        self._messages.append(message)
        self._message_index += 1
        logger.debug(f"🔍 Preview: Added message to memory (total: {len(self._messages)})")

    def append_content_to_message(
        self,
        session_id: str,
        window_id: str,
        message_index: int,
        content: Any
    ) -> None:
        """
        Append content to an existing message.

        Args:
            session_id: Session identifier
            window_id: Window identifier (ignored for preview)
            message_index: Index of message to update
            content: Content to append
        """
        if 0 <= message_index < len(self._messages):
            msg = self._messages[message_index]
            if hasattr(msg, 'content') and isinstance(msg.content, list):
                msg.content.append(content)
            logger.debug(f"🔍 Preview: Appended content to message {message_index}")

    def get_message_count(self, session_id: str) -> int:
        """
        Get the number of messages in the session.

        Args:
            session_id: Session identifier

        Returns:
            Number of messages
        """
        return len(self._messages)

    def clear_session(self) -> None:
        """Clear all messages from the in-memory session."""
        self._messages.clear()
        self._message_index = 0
        logger.debug(f"🔍 Preview: Cleared session memory")

    # Properties for compatibility with other session managers
    @property
    def messages(self) -> List[SessionMessage]:
        """Get all messages in the session."""
        return self._messages.copy()

    @property
    def message_count(self) -> int:
        """Get the number of messages."""
        return len(self._messages)

    def register_hooks(self, registry, **kwargs) -> None:
        """
        Register hooks with the Strands Agent framework.

        For preview sessions, we use simple in-memory storage with no persistence.
        """
        from strands.hooks import AgentInitializedEvent, MessageAddedEvent

        logger.debug("🔗 Registering preview session hooks (in-memory only)")

        # Register initialization hook
        registry.add_callback(
            AgentInitializedEvent,
            lambda event: self._initialize_agent(event.agent)
        )

        # Register message added hook
        registry.add_callback(
            MessageAddedEvent,
            lambda event: self._on_message_added(event.message, event.agent)
        )

        logger.debug("✅ Preview session hooks registered")

    def _initialize_agent(self, agent) -> None:
        """
        Initialize agent with existing messages from memory.

        Args:
            agent: The Strands agent instance
        """
        # Load any existing messages into the agent
        if self._messages:
            # Convert SessionMessage to dict format expected by agent
            agent.messages = [
                msg.message if hasattr(msg, 'message') else msg
                for msg in self._messages
            ]
            logger.debug(f"🔍 Preview: Initialized agent with {len(self._messages)} messages")
        else:
            agent.messages = []
            logger.debug("🔍 Preview: Initialized agent with empty message list")

    def _on_message_added(self, message, agent) -> None:
        """
        Handle message added event - store in memory.

        Args:
            message: The message that was added
            agent: The Strands agent instance
        """
        from strands.types.session import SessionMessage

        # Wrap in SessionMessage if needed
        if not isinstance(message, SessionMessage):
            session_message = SessionMessage(
                message_id=str(self._message_index),
                message=message
            )
        else:
            session_message = message

        self._messages.append(session_message)
        self._message_index += 1
        logger.debug(f"🔍 Preview: Stored message in memory (total: {len(self._messages)})")
