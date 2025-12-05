"""
Message ID Injector

Wrapper around session managers that generates and injects UUIDs for messages.
This wrapper sits between the Strands agent framework and the actual session manager
(TurnBasedSessionManager or LocalSessionBuffer), generating UUIDs for each message
before it's stored.
"""

import uuid
import logging
from typing import Optional, List, Any

logger = logging.getLogger(__name__)


class MessageIdInjector:
    """
    Wrapper that generates UUIDs for messages and injects them before storage.

    The Strands agent framework calls append_message() automatically. This wrapper
    intercepts those calls to inject UUIDs, which are then:
    1. Sent to the client in message_start SSE events
    2. Stored with the message in the session manager
    3. Used for message retrieval and reference

    This maintains turn-based buffering efficiency while providing consistent IDs.
    """

    def __init__(self, base_manager: Any, session_id: str):
        """
        Initialize the MessageIdInjector wrapper.

        Args:
            base_manager: The underlying session manager (TurnBasedSessionManager or LocalSessionBuffer)
            session_id: Session identifier for logging purposes
        """
        self.base_manager = base_manager
        self.session_id = session_id
        self.current_message_id: Optional[str] = None
        self.next_message_id: Optional[str] = None
        self.message_ids: List[str] = []

    def peek_next_message_id(self) -> str:
        """
        Pre-generate UUID for the next message.

        This is called when we see a message_start event to get the ID before
        append_message is called by the Strands framework.

        IMPORTANT: Always generates a fresh UUID. The cached ID is consumed
        when append_message is called. If peek is called multiple times before
        append_message (shouldn't happen), we generate a new ID each time.

        Returns:
            str: UUID for the next message
        """
        # Always generate a fresh ID for each peek
        # The ID will be cached for append_message to use
        self.next_message_id = str(uuid.uuid4())
        logger.debug(f"🔮 Generated message ID for upcoming message: {self.next_message_id}")
        return self.next_message_id

    def get_current_message_id(self) -> Optional[str]:
        """
        Get the ID of the current message being processed.

        Returns:
            Optional[str]: Current message ID, or None if no message is active
        """
        return self.current_message_id

    def append_message(self, message, agent, **kwargs):
        """
        Intercept append_message to inject UUID before storing.

        This method is called by the Strands agent framework automatically
        when a message needs to be persisted. We generate/use a UUID and
        inject it via kwargs before passing to the base manager.

        Args:
            message: Message from Strands framework
            agent: Agent instance
            **kwargs: Additional arguments (we inject message_id here)

        Returns:
            Result from base manager's append_message
        """
        # Use pre-generated ID if available, otherwise generate new one
        if self.next_message_id:
            message_id = self.next_message_id
            self.next_message_id = None  # Clear for next message
        else:
            message_id = str(uuid.uuid4())
            logger.debug(f"🆕 Generated message ID on-the-fly: {message_id}")

        self.current_message_id = message_id
        self.message_ids.append(message_id)

        # Inject message_id into kwargs so base manager can access it
        kwargs['message_id'] = message_id

        logger.info(f"💾 Injecting message_id {message_id} into message (role={message.get('role')})")

        # Pass to base manager with injected ID
        return self.base_manager.append_message(message, agent, **kwargs)

    def flush(self):
        """
        Flush the base session manager.

        This triggers the turn-based buffering to write accumulated messages
        to storage (AgentCore Memory or local files).

        Returns:
            Result from base manager's flush method
        """
        result = self.base_manager.flush()
        logger.debug(f"💾 Flushed session with message IDs: {self.message_ids}")
        return result

    def __getattr__(self, name):
        """
        Delegate all other methods to the base manager.

        This allows the wrapper to be transparent for any methods we don't
        explicitly override (like list_messages, get_session, etc.).

        Args:
            name: Method name being accessed

        Returns:
            The attribute/method from the base manager
        """
        return getattr(self.base_manager, name)
