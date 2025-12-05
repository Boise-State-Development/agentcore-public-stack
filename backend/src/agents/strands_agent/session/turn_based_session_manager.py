"""
Turn-based Session Manager Wrapper
Buffers messages within a turn and writes to AgentCore Memory only once per turn.
Reduces API calls by 75% (4 calls → 1 call per turn).
"""

import logging
from typing import Optional, Dict, Any, List, Union
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig

logger = logging.getLogger(__name__)


class TurnBasedSessionManager:
    """
    Wrapper around AgentCoreMemorySessionManager that buffers messages
    within a turn and writes them as a single merged message.

    A "turn" consists of:
    1. User message
    2. Assistant response (text + toolUse)
    3. Tool results (toolResult)

    Instead of creating 3-4 events, we create 1 merged event per turn.
    """

    def __init__(
        self,
        agentcore_memory_config: AgentCoreMemoryConfig,
        region_name: str = "us-west-2",
        batch_size: int = 5  # Flush every N messages to prevent data loss
    ):
        self.base_manager = AgentCoreMemorySessionManager(
            agentcore_memory_config=agentcore_memory_config,
            region_name=region_name
        )

        # Turn buffer
        self.pending_messages: List[Dict[str, Any]] = []
        self.last_message_role: Optional[str] = None
        self.batch_size = batch_size
        self.cancelled = False  # Flag to stop accepting new messages

        logger.info(f"✅ TurnBasedSessionManager initialized (buffering enabled, batch_size={batch_size})")

    def _should_flush_turn(self, message: Dict[str, Any]) -> bool:
        """
        Determine if we should flush the current turn.

        Flush when:
        1. New user message arrives (previous turn is complete)
        2. Assistant message has only text (no toolUse) - turn is complete
        """
        role = message.get("role", "")
        content = message.get("content", [])

        # Case 1: New user message starts a new turn
        if role == "user" and self.last_message_role == "assistant":
            return True

        # Case 2: Assistant message with no toolUse means turn is complete
        if role == "assistant":
            has_tool_use = any(
                isinstance(item, dict) and "toolUse" in item
                for item in content
            )
            if not has_tool_use:
                return True

        return False

    def _merge_turn_messages(self) -> Optional[Dict[str, Any]]:
        """
        Merge all messages in the current turn into a single message.

        Returns:
            Merged message with all content blocks combined, preserving message_id from first message
        """
        if not self.pending_messages:
            return None

        # If only 1 message, return as-is
        if len(self.pending_messages) == 1:
            return self.pending_messages[0]

        # Merge all content blocks
        merged_content = []
        merged_role = self.pending_messages[0].get("role", "assistant")
        # Preserve message_id from first message (the UUID sent to client in message_start event)
        merged_message_id = self.pending_messages[0].get("message_id")

        for msg in self.pending_messages:
            content = msg.get("content", [])
            if isinstance(content, list):
                merged_content.extend(content)

        merged_message = {
            "role": merged_role,
            "content": merged_content
        }
        
        # Include message_id if it exists
        if merged_message_id:
            merged_message["message_id"] = merged_message_id

        return merged_message

    def _flush_turn(self) -> Optional[int]:
        """
        Flush pending messages as a single merged message to AgentCore Memory

        Returns:
            Message ID of the flushed message, or None if nothing was flushed
        """
        if not self.pending_messages:
            return None

        merged_message = self._merge_turn_messages()
        if merged_message:
            # Write merged message to AgentCore Memory
            logger.info(f"💾 Flushing turn: {len(self.pending_messages)} messages → 1 merged event")

            # Extract UUID from merged message before persisting
            uuid_from_merged = merged_message.get("message_id")

            # Call base manager's create_message directly to persist
            # We need to convert to SessionMessage format first
            from strands.types.session import SessionMessage
            from strands.types.content import Message

            # Convert merged message to Message type for base manager
            strands_message: Message = {
                "role": merged_message["role"],
                "content": merged_message["content"]
            }

            # Create a SessionMessage and persist it
            session_message = SessionMessage.from_message(strands_message, 0)
            self.base_manager.create_message(
                self.base_manager.config.session_id,
                "default",  # agent_id (not used in AgentCore Memory)
                session_message
            )

            # Get message ID from session manager
            # For AgentCore Memory, we need to count messages to get the ID
            sequential_message_id = self._get_latest_message_id()

            # Store UUID-to-sequential-ID mapping if UUID exists
            # This preserves the UUID sent to the client in message_start events
            if uuid_from_merged and sequential_message_id:
                self._store_uuid_mapping(uuid_from_merged, sequential_message_id)
                logger.info(f"🔗 Stored UUID mapping: {uuid_from_merged} → {sequential_message_id}")

            # Clear buffer
            self.pending_messages = []

            return sequential_message_id

        # Clear buffer even if no message was created
        self.pending_messages = []
        return None

    def _get_latest_message_id(self) -> Optional[int]:
        """
        Get the ID of the most recently stored message

        For AgentCore Memory, we count the total messages in the session.
        For local file storage, this is handled differently.

        Returns:
            Message ID (1-indexed) or None if unavailable
        """
        try:
            # Get all messages from the session
            messages = self.base_manager.list_messages(
                self.base_manager.config.session_id,
                "default"  # agent_id
            )
            if messages:
                return len(messages)
        except Exception as e:
            logger.error(f"Failed to get latest message ID: {e}")

        return None

    def _store_uuid_mapping(self, uuid: str, sequential_id: int) -> None:
        """
        Store UUID-to-sequential-ID mapping for AgentCore Memory messages.

        Since AgentCore Memory uses sequential IDs but we send UUIDs to clients,
        we need to maintain a mapping to preserve the contract that client-facing
        IDs match stored message IDs.

        Args:
            uuid: UUID sent to client in message_start event
            sequential_id: Sequential ID returned by AgentCore Memory
        """
        try:
            import os
            import json
            from pathlib import Path

            # For cloud storage (AgentCore Memory), we could store in DynamoDB
            # For now, store in a local mapping file as a fallback
            # This mapping can be used when retrieving messages to map sequential IDs back to UUIDs
            conversations_table = os.environ.get('CONVERSATIONS_TABLE_NAME')
            session_id = self.base_manager.config.session_id

            if conversations_table:
                # Cloud: Store in DynamoDB (future implementation)
                # For now, log the mapping - it can be stored in message metadata
                logger.debug(f"Cloud storage: UUID {uuid} maps to sequential ID {sequential_id}")
                # TODO: Store UUID mapping in DynamoDB conversations table
            else:
                # Local: Store in a mapping file
                from apis.app_api.storage.paths import get_session_dir
                session_dir = get_session_dir(session_id)
                mapping_file = session_dir / "uuid_mappings.json"

                # Read existing mappings
                mappings = {}
                if mapping_file.exists():
                    try:
                        with open(mapping_file, 'r') as f:
                            mappings = json.load(f)
                    except Exception as e:
                        logger.warning(f"Failed to read UUID mappings: {e}")

                # Add new mapping
                mappings[str(sequential_id)] = uuid

                # Write back
                mapping_file.parent.mkdir(parents=True, exist_ok=True)
                with open(mapping_file, 'w') as f:
                    json.dump(mappings, f, indent=2)

                logger.debug(f"💾 Stored UUID mapping locally: {uuid} → {sequential_id}")

        except Exception as e:
            # Don't fail the flush if UUID mapping storage fails
            logger.warning(f"Failed to store UUID mapping: {e}")

    def add_message(self, message: Dict[str, Any]):
        """
        Add a message to the turn buffer.
        Automatically flushes when turn is complete or batch size is reached.
        """
        role = message.get("role", "")

        # Check if we should flush previous turn
        if self._should_flush_turn(message):
            self._flush_turn()

        # Add message to buffer
        self.pending_messages.append(message)
        self.last_message_role = role

        logger.debug(f"📝 Buffered message (role={role}, total={len(self.pending_messages)})")

        # Periodic flush: if buffer reaches batch_size, flush to prevent data loss
        if len(self.pending_messages) >= self.batch_size:
            logger.info(f"⏰ Batch size ({self.batch_size}) reached, flushing buffer")
            self._flush_turn()

    def flush(self) -> Optional[int]:
        """
        Force flush any pending messages (e.g., at end of stream)

        Returns:
            Message ID of the flushed message, or None if nothing was flushed
        """
        return self._flush_turn()

    def append_message(self, message, agent, message_id: Optional[str] = None, **kwargs):
        """
        Override append_message to buffer messages instead of immediately persisting.

        This is the key method that Strands framework calls to persist messages.
        We intercept it to implement turn-based buffering.

        Args:
            message: Message from Strands framework
            agent: Agent instance (not used in buffering)
            message_id: Optional UUID injected by MessageIdInjector wrapper
            **kwargs: Additional arguments
        """
        # If cancelled, don't accept new messages
        if self.cancelled:
            logger.warning(f"🚫 Session cancelled, ignoring message (role={message.get('role')})")
            return

        from strands.types.session import SessionMessage

        # Use provided ID or generate new one (fallback)
        if not message_id:
            import uuid
            message_id = str(uuid.uuid4())
            logger.warning(f"⚠️ No message_id provided, generated fallback: {message_id}")

        # Convert Message to dict format for buffering
        message_dict = {
            "message_id": message_id,  # Include UUID
            "role": message.get("role"),
            "content": message.get("content", [])
        }

        # Add to buffer and check if we should flush
        self.add_message(message_dict)

        logger.debug(f"🔄 Intercepted append_message {message_id} (role={message_dict['role']}, buffered={len(self.pending_messages)})")

    # Delegate all other methods to base manager
    def __getattr__(self, name):
        """Delegate unknown methods to base AgentCore session manager"""
        return getattr(self.base_manager, name)
