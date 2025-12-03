"""
Stream coordinator for managing agent streaming lifecycle
"""
import logging
import json
import os
from typing import AsyncGenerator, Optional, Any, Union, List, Dict

from .event_processor import StreamEventProcessor

logger = logging.getLogger(__name__)


class StreamCoordinator:
    """Coordinates streaming lifecycle for agent responses"""

    def __init__(self, stream_processor: StreamEventProcessor):
        """
        Initialize stream coordinator

        Args:
            stream_processor: Event processor for handling Strands events
        """
        self.stream_processor = stream_processor

    async def stream_response(
        self,
        agent: Any,
        prompt: Union[str, List[Dict[str, Any]]],
        session_manager: Any,
        session_id: str,
        user_id: str
    ) -> AsyncGenerator[str, None]:
        """
        Stream agent responses with proper lifecycle management

        Args:
            agent: Strands Agent instance
            prompt: User prompt (string or ContentBlock list)
            session_manager: Session manager for persistence
            session_id: Session identifier
            user_id: User identifier

        Yields:
            str: SSE formatted events
        """
        # Set environment variables for browser session isolation
        os.environ['SESSION_ID'] = session_id
        os.environ['USER_ID'] = user_id

        try:
            # Log prompt information
            self._log_prompt_info(prompt)

            # Stream events through processor
            async for event in self.stream_processor.process_stream(
                agent,
                prompt,
                file_paths=None,
                session_id=session_id
            ):
                yield event

            # Flush buffered messages (turn-based session manager)
            self._flush_session(session_manager)

        except Exception as e:
            # Handle errors with emergency flush
            logger.error(f"Error in stream_response: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

            # Emergency flush: save buffered messages before losing them
            self._emergency_flush(session_manager)

            # Send error event to client
            yield self._create_error_event(str(e))

    def _log_prompt_info(self, prompt: Union[str, List[Dict[str, Any]]]) -> None:
        """
        Log prompt information for debugging

        Args:
            prompt: Prompt (string or content blocks)
        """
        if isinstance(prompt, list):
            logger.info(f"Prompt is list with {len(prompt)} content blocks")
        else:
            logger.info(f"Prompt is string: {prompt[:100] if len(prompt) > 100 else prompt}")

    def _flush_session(self, session_manager: Any) -> None:
        """
        Flush session manager if it supports buffering

        Args:
            session_manager: Session manager instance
        """
        if hasattr(session_manager, 'flush'):
            session_manager.flush()
            logger.debug("💾 Session flushed after streaming complete")

    def _emergency_flush(self, session_manager: Any) -> None:
        """
        Emergency flush on error to prevent data loss

        Args:
            session_manager: Session manager instance
        """
        if hasattr(session_manager, 'flush'):
            try:
                pending_count = len(getattr(session_manager, 'pending_messages', []))
                session_manager.flush()
                logger.warning(f"🚨 Emergency flush on error - saved {pending_count} buffered messages")
            except Exception as flush_error:
                logger.error(f"Failed to emergency flush: {flush_error}")

    def _create_error_event(self, error_message: str) -> str:
        """
        Create SSE error event

        Args:
            error_message: Error message

        Returns:
            str: SSE formatted error event
        """
        error_event = {
            "type": "error",
            "message": error_message
        }
        return f"data: {json.dumps(error_event)}\n\n"
