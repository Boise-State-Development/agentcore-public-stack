"""
Stream coordinator for managing agent streaming lifecycle
"""
import logging
import json
import os
import time
from datetime import datetime, timezone
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

        This method now also collects metadata during streaming and stores it
        after the stream completes.

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

        # Track timing for latency metrics
        stream_start_time = time.time()
        first_token_time: Optional[float] = None

        # Accumulate metadata from stream
        accumulated_metadata: Dict[str, Any] = {
            "usage": {},
            "metrics": {}
        }

        try:
            # Log prompt information
            self._log_prompt_info(prompt)

            # Get raw agent stream
            agent_stream = agent.stream_async(prompt)
            
            # Track if we've seen metadata to avoid duplicate extraction
            metadata_seen = False

            # Process through new stream processor and format as SSE
            async for event in process_agent_stream(agent_stream):
                # Collect metadata_summary event (don't send to client)
                if event.get("type") == "metadata_summary":
                    event_data = event.get("data", {})
                    if "usage" in event_data:
                        accumulated_metadata["usage"].update(event_data["usage"])
                    if "metrics" in event_data:
                        accumulated_metadata["metrics"].update(event_data["metrics"])
                    if "first_token_time" in event_data:
                        first_token_time = event_data["first_token_time"]
                    # Don't yield this event to the client
                    continue

                # Format as SSE event and yield
                sse_event = self._format_sse_event(event)
                
                # Log metadata events for debugging
                if event.get("type") == "metadata":
                    logger.info(f"📡 Sending metadata event via SSE: {event.get('data')}")
                    metadata_seen = True
                
                yield sse_event

            # Calculate end-to-end latency
            stream_end_time = time.time()

            # Flush buffered messages (turn-based session manager)
            # This returns the message ID of the flushed message
            message_id = self._flush_session(session_manager)

            # Store metadata after flush completes
            if message_id and (accumulated_metadata.get("usage") or first_token_time):
                await self._store_metadata(
                    session_id=session_id,
                    user_id=user_id,
                    message_id=message_id,
                    accumulated_metadata=accumulated_metadata,
                    stream_start_time=stream_start_time,
                    stream_end_time=stream_end_time,
                    first_token_time=first_token_time,
                    agent=agent  # Pass agent for model info extraction
                )

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

    def _flush_session(self, session_manager: Any) -> Optional[int]:
        """
        Flush session manager if it supports buffering

        Args:
            session_manager: Session manager instance

        Returns:
            Message ID of the flushed message, or None if unavailable
        """
        if hasattr(session_manager, 'flush'):
            message_id = session_manager.flush()
            logger.debug(f"💾 Session flushed after streaming complete (message ID: {message_id})")
            return message_id
        return None

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

    async def _store_metadata(
        self,
        session_id: str,
        user_id: str,
        message_id: int,
        accumulated_metadata: Dict[str, Any],
        stream_start_time: float,
        stream_end_time: float,
        first_token_time: Optional[float],
        agent: Any = None
    ) -> None:
        """
        Store message metadata after streaming completes

        Args:
            session_id: Session identifier
            user_id: User identifier
            message_id: Message ID from session manager
            accumulated_metadata: Metadata collected during streaming
            stream_start_time: Timestamp when stream started
            stream_end_time: Timestamp when stream ended
            first_token_time: Timestamp of first token received
            agent: Agent instance for extracting model info
        """
        try:
            from apis.app_api.messages.models import (
                MessageMetadata, TokenUsage, LatencyMetrics,
                ModelInfo, Attribution
            )
            from apis.app_api.metadata.service import store_message_metadata

            # Build TokenUsage if we have usage data
            token_usage = None
            if accumulated_metadata.get("usage"):
                usage_data = accumulated_metadata["usage"]
                token_usage = TokenUsage(
                    input_tokens=usage_data.get("inputTokens", 0),
                    output_tokens=usage_data.get("outputTokens", 0),
                    total_tokens=usage_data.get("totalTokens", 0),
                    cache_read_input_tokens=usage_data.get("cacheReadInputTokens"),
                    cache_write_input_tokens=usage_data.get("cacheWriteInputTokens")
                )

            # Build LatencyMetrics if we have timing data
            latency_metrics = None
            if first_token_time:
                latency_metrics = LatencyMetrics(
                    time_to_first_token=int((first_token_time - stream_start_time) * 1000),
                    end_to_end_latency=int((stream_end_time - stream_start_time) * 1000)
                )

            # Extract ModelInfo from agent (for cost tracking foundation)
            model_info = None
            if agent and hasattr(agent, 'model_config'):
                model_id = agent.model_config.model_id
                model_info = ModelInfo(
                    model_id=model_id,
                    model_name=self._extract_model_name(model_id),
                    model_version=self._extract_model_version(model_id)
                    # pricing_snapshot will be added in future implementation
                )

            # Create Attribution for cost tracking foundation
            attribution = Attribution(
                user_id=user_id,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc).isoformat()
                # organization_id will be added when multi-tenant billing is implemented
                # tags will be added for cost allocation features
            )

            # Create MessageMetadata
            if token_usage or latency_metrics or model_info:
                message_metadata = MessageMetadata(
                    latency=latency_metrics,
                    token_usage=token_usage,
                    model_info=model_info,
                    attribution=attribution
                )

                # Store metadata
                await store_message_metadata(
                    session_id=session_id,
                    user_id=user_id,
                    message_id=message_id,
                    message_metadata=message_metadata
                )

                logger.info(f"✅ Stored metadata for message {message_id} (model: {model_info.model_name if model_info else 'unknown'})")

        except Exception as e:
            # Log but don't raise - metadata storage failures shouldn't break streaming
            logger.error(f"Failed to store metadata: {e}")

        # Update session-level metadata after message metadata
        try:
            await self._update_session_metadata(
                session_id=session_id,
                user_id=user_id,
                message_id=message_id,
                agent=agent
            )
        except Exception as e:
            # Log but don't raise - metadata storage failures shouldn't break streaming
            logger.error(f"Failed to update session metadata: {e}")

    def _extract_model_name(self, model_id: str) -> str:
        """
        Extract human-readable model name from model ID

        Args:
            model_id: Full model identifier (e.g., "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

        Returns:
            Human-readable name (e.g., "Claude Sonnet 4.5")
        """
        # Map model IDs to friendly names
        # TODO: Move to configuration file in future implementation
        model_name_map = {
            "claude-sonnet-4-5": "Claude Sonnet 4.5",
            "claude-opus-4": "Claude Opus 4",
            "claude-haiku-4-5": "Claude Haiku 4.5",
            "claude-3-5-sonnet": "Claude 3.5 Sonnet",
            "claude-3-opus": "Claude 3 Opus",
            "claude-3-haiku": "Claude 3 Haiku"
        }

        # Extract model name from ID
        for key, name in model_name_map.items():
            if key in model_id:
                return name

        # Fallback: return the model ID itself
        return model_id

    def _extract_model_version(self, model_id: str) -> Optional[str]:
        """
        Extract model version from model ID

        Args:
            model_id: Full model identifier

        Returns:
            Version string (e.g., "v1") or None
        """
        # Extract version from model ID (e.g., "v1:0" -> "v1")
        if ":0" in model_id:
            parts = model_id.split("-")
            for part in parts:
                if part.startswith("v") and ":" in part:
                    return part.split(":")[0]
        return None

    async def _update_session_metadata(
        self,
        session_id: str,
        user_id: str,
        message_id: int,
        agent: Any = None
    ) -> None:
        """
        Update session-level metadata after each message

        This updates conversation-level tracking after each message:
        - lastMessageAt: Timestamp of this message
        - messageCount: Incremented by 1
        - preferences: Model/temperature from agent config
        - Auto-creates session metadata on first message

        Args:
            session_id: Session identifier
            user_id: User identifier
            message_id: Message ID that was just flushed
            agent: Agent instance for extracting model preferences
        """
        try:
            from apis.app_api.messages.models import SessionMetadata, SessionPreferences
            from apis.app_api.metadata.service import store_session_metadata, get_session_metadata

            # Get existing metadata or create new
            existing = await get_session_metadata(session_id, user_id)

            now = datetime.now(timezone.utc).isoformat()

            if not existing:
                # First message - create session metadata
                preferences = None
                if agent and hasattr(agent, 'model_config'):
                    preferences = SessionPreferences(
                        last_model=agent.model_config.model_id,
                        last_temperature=getattr(agent.model_config, 'temperature', None)
                    )

                metadata = SessionMetadata(
                    session_id=session_id,
                    user_id=user_id,
                    title="New Conversation",  # Will be updated by frontend
                    status="active",
                    created_at=now,
                    last_message_at=now,
                    message_count=1,
                    starred=False,
                    tags=[],
                    preferences=preferences
                )
            else:
                # Update existing - only update what changed
                preferences = existing.preferences
                if agent and hasattr(agent, 'model_config'):
                    # Update preferences if model/temperature changed
                    prefs_dict = preferences.model_dump(by_alias=False) if preferences else {}
                    prefs_dict['last_model'] = agent.model_config.model_id
                    prefs_dict['last_temperature'] = getattr(agent.model_config, 'temperature', None)
                    preferences = SessionPreferences(**prefs_dict)

                metadata = SessionMetadata(
                    session_id=session_id,
                    user_id=user_id,
                    title=existing.title,
                    status=existing.status,
                    created_at=existing.created_at,
                    last_message_at=now,
                    message_count=existing.message_count + 1,
                    starred=existing.starred,
                    tags=existing.tags,
                    preferences=preferences
                )

            # Store updated metadata (uses deep merge in storage layer)
            await store_session_metadata(
                session_id=session_id,
                user_id=user_id,
                session_metadata=metadata
            )

            logger.info(f"✅ Updated session metadata (msg count: {metadata.message_count}, last message: {now})")

        except Exception as e:
            logger.error(f"Failed to update session metadata: {e}")
            # Don't raise - metadata failures shouldn't break streaming
