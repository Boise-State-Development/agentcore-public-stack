"""
Stream coordinator for managing agent streaming lifecycle
"""
import logging
import json
import os
from typing import AsyncGenerator, Optional, Any, Union, List, Dict

from .stream_processor import process_agent_stream

logger = logging.getLogger(__name__)


class StreamCoordinator:
    """Coordinates streaming lifecycle for agent responses"""

    def __init__(self):
        """
        Initialize stream coordinator

        The new implementation is stateless and uses pure functions,
        so no dependencies are needed in the constructor.
        """
        pass

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

            # Get raw agent stream
            agent_stream = agent.stream_async(prompt)
            
            # Track if we've seen metadata to avoid duplicate extraction
            metadata_seen = False

            # Process through new stream processor and format as SSE
            async for event in process_agent_stream(agent_stream):
                # Format as SSE event
                sse_event = self._format_sse_event(event)
                
                # Log metadata events for debugging
                if event.get("type") == "metadata":
                    logger.info(f"📡 Sending metadata event via SSE: {event.get('data')}")
                    metadata_seen = True
                
                yield sse_event

            # If we didn't get metadata from the stream, try to get it from the agent's last result
            # This is a fallback for cases where Strands doesn't emit result in the stream
            if not metadata_seen:
                try:
                    # Try to get metrics from agent's last result if available
                    if hasattr(agent, 'last_result') and agent.last_result:
                        result = agent.last_result
                        if hasattr(result, 'metrics'):
                            metrics = result.metrics
                            metadata_data = {}
                            
                            # Extract usage
                            if hasattr(metrics, 'accumulated_usage'):
                                usage = metrics.accumulated_usage
                                if usage:
                                    metadata_data["usage"] = {
                                        "inputTokens": getattr(usage, 'inputTokens', getattr(usage, 'input_tokens', 0)),
                                        "outputTokens": getattr(usage, 'outputTokens', getattr(usage, 'output_tokens', 0)),
                                        "totalTokens": getattr(usage, 'totalTokens', getattr(usage, 'total_tokens', 0)),
                                    }
                                    # Add cache tokens if available
                                    cache_read = getattr(usage, 'cacheReadInputTokens', getattr(usage, 'cache_read_input_tokens', None))
                                    cache_write = getattr(usage, 'cacheWriteInputTokens', getattr(usage, 'cache_write_input_tokens', None))
                                    if cache_read:
                                        metadata_data["usage"]["cacheReadInputTokens"] = cache_read
                                    if cache_write:
                                        metadata_data["usage"]["cacheWriteInputTokens"] = cache_write
                            
                            # Extract metrics
                            if hasattr(metrics, 'accumulated_metrics'):
                                accumulated_metrics = metrics.accumulated_metrics
                                if accumulated_metrics:
                                    metadata_data["metrics"] = {
                                        "latencyMs": getattr(accumulated_metrics, 'latencyMs', getattr(accumulated_metrics, 'latency_ms', 0))
                                    }
                            
                            if metadata_data:
                                logger.info("Extracted metadata from agent.last_result fallback")
                                metadata_event = {"type": "metadata", "data": metadata_data}
                                yield self._format_sse_event(metadata_event)
                except Exception as e:
                    logger.debug(f"Could not extract metadata from agent fallback: {e}")

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

    def _format_sse_event(self, event: Dict[str, Any]) -> str:
        """
        Format processed event as SSE (Server-Sent Event)

        Args:
            event: Processed event from stream_processor {"type": str, "data": dict}

        Returns:
            str: SSE formatted event string with event type and data
        """
        try:
            event_type = event.get("type", "message")
            event_data = event.get("data", {})

            # Format as SSE with explicit event type
            return f"event: {event_type}\ndata: {json.dumps(event_data)}\n\n"
        except (TypeError, ValueError) as e:
            # Fallback for non-serializable objects (should never happen with new processor)
            logger.error(f"Failed to serialize event: {e}")
            return f"event: error\ndata: {json.dumps({'error': f'Serialization error: {str(e)}'})}\n\n"

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
        return f"event: error\ndata: {json.dumps({'error': error_message})}\n\n"
