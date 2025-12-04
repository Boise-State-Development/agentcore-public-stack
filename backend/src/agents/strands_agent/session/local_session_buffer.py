"""
Local Session Buffer Manager
Wraps FileSessionManager with buffering and cancellation support for local development.
Similar to TurnBasedSessionManager but for local file-based storage.
"""

import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class LocalSessionBuffer:
    """
    Wrapper around FileSessionManager that adds:
    1. Cancellation support (cancelled flag)
    2. Simple buffering to batch writes

    For local development only - mimics TurnBasedSessionManager behavior.
    """

    def __init__(
        self,
        base_manager,
        session_id: str,
        batch_size: int = 5
    ):
        self.base_manager = base_manager
        self.session_id = session_id
        self.batch_size = batch_size
        self.cancelled = False  # Flag to stop accepting new messages
        self.pending_messages: List[Dict[str, Any]] = []

        logger.info(f"✅ LocalSessionBuffer initialized (batch_size={batch_size})")

    def append_message(self, message, agent, **kwargs):
        """
        Override append_message to buffer messages and check cancelled flag.
        """
        # If cancelled, don't accept new messages
        if self.cancelled:
            logger.warning(f"🚫 Session cancelled, ignoring message (role={message.get('role')})")
            return

        # Convert Message to dict format for buffering
        message_dict = {
            "role": message.get("role"),
            "content": message.get("content", [])
        }

        # Add to buffer
        self.pending_messages.append(message_dict)
        logger.debug(f"📝 Buffered message (role={message_dict['role']}, total={len(self.pending_messages)})")

        # Periodic flush to prevent data loss
        if len(self.pending_messages) >= self.batch_size:
            logger.info(f"⏰ Batch size ({self.batch_size}) reached, flushing buffer")
            self.flush()

    def flush(self) -> Optional[int]:
        """
        Force flush pending messages to FileSessionManager

        Returns:
            Message ID of the last flushed message, or None if nothing was flushed
        """
        if not self.pending_messages:
            return None

        logger.info(f"💾 Flushing {len(self.pending_messages)} messages to FileSessionManager")

        last_message_id = None

        # Write each pending message to base manager
        for message_dict in self.pending_messages:
            # Convert dict back to Message-like object
            from strands.types.session import SessionMessage
            from strands.types.content import Message

            strands_message: Message = {
                "role": message_dict["role"],
                "content": message_dict["content"]
            }

            # Create SessionMessage and pass to base manager
            session_message = SessionMessage.from_message(strands_message, 0)

            try:
                # FileSessionManager's append_message signature
                self.base_manager.append_message(session_message, agent=None)
            except Exception as e:
                logger.error(f"Failed to write message to FileSessionManager: {e}")

        # Get the latest message ID after flushing
        last_message_id = self._get_latest_message_id()

        # Clear buffer
        self.pending_messages = []
        logger.debug(f"✅ Buffer flushed (last message ID: {last_message_id})")

        return last_message_id

    def _get_latest_message_id(self) -> Optional[int]:
        """
        Get the ID of the most recently stored message in local file storage

        Returns:
            Message ID (1-indexed) or None if unavailable
        """
        try:
            from pathlib import Path
            from apis.app_api.storage.paths import get_messages_dir

            # Get messages directory
            messages_dir = get_messages_dir(self.session_id)

            if messages_dir.exists():
                # Get all message files sorted by number
                message_files = sorted(
                    messages_dir.glob("message_*.json"),
                    key=lambda p: int(p.stem.split("_")[1]) if p.stem.split("_")[1].isdigit() else 0
                )
                if message_files:
                    # Get the highest message number
                    latest_file = message_files[-1]
                    message_num = int(latest_file.stem.split("_")[1])
                    return message_num

        except Exception as e:
            logger.error(f"Failed to get latest message ID: {e}")

        return None

    # Delegate all other methods to base manager
    def __getattr__(self, name):
        """Delegate unknown methods to base FileSessionManager"""
        return getattr(self.base_manager, name)
