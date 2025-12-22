"""
Stream coordinator for managing agent streaming lifecycle
"""
import logging
import json
import os
import time
import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional, Any, Union, List, Dict

from .stream_processor import process_agent_stream
from apis.shared.errors import StreamErrorEvent, ErrorCode

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
        user_id: str,
        strands_agent_wrapper: Any = None
    ) -> AsyncGenerator[str, None]:
        """
        Stream agent responses with proper lifecycle management

        This method now also collects metadata during streaming and stores it
        after the stream completes.

        Args:
            agent: Strands Agent instance (internal agent)
            prompt: User prompt (string or ContentBlock list)
            session_manager: Session manager for persistence
            session_id: Session identifier
            user_id: User identifier
            strands_agent_wrapper: StrandsAgent wrapper instance (has model_config, enabled_tools, etc.)

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

        # Track individual metadata per assistant message during streaming
        # Each entry contains: usage, metrics, timing info (start_time, first_token_time, end_time)
        # This enables accurate per-message latency tracking for multi-turn tool use scenarios
        per_message_metadata: List[Dict[str, Any]] = []
        current_assistant_message_index = -1  # Track which assistant message we're on (0-indexed within this stream)

        # OPTIMIZATION: Capture initial message count BEFORE streaming starts
        # This allows us to calculate message indices without post-stream AgentCore Memory queries
        # The TurnBasedSessionManager.message_count is initialized from AgentCore Memory at session start
        # and represents the number of messages that existed BEFORE this stream
        initial_message_count = self._get_initial_message_count(session_manager)
        logger.info(f"📊 Initial message count before streaming: {initial_message_count}")

        try:
            # Get raw agent stream
            agent_stream = agent.stream_async(prompt)

            # Process through new stream processor and format as SSE
            async for event in process_agent_stream(agent_stream):
                # Track when new assistant messages start (to associate metadata with them)
                if event.get("type") == "message_start":
                    role = event.get("data", {}).get("role")
                    if role == "assistant":
                        current_assistant_message_index += 1
                        # Record the start time for this specific assistant message
                        # This enables accurate per-message latency calculation
                        per_message_metadata.append({
                            "usage": {},
                            "metrics": {},
                            "start_time": time.time(),  # When this message started
                            "first_token_time": None,   # When first token was received
                            "end_time": None            # When this message ended
                        })
                        logger.debug(f"📝 Assistant message {current_assistant_message_index} started at {per_message_metadata[-1]['start_time']}")

                # Track first token time per assistant message
                # This captures when the first content delta arrives for each message
                # We check for text content specifically to measure time to first TEXT token
                if event.get("type") == "content_block_delta":
                    event_data = event.get("data", {})
                    # Only track first token for text deltas (not tool use deltas)
                    # This gives accurate TTFT for actual text generation
                    if event_data.get("type") == "text" and event_data.get("text"):
                        if current_assistant_message_index >= 0 and current_assistant_message_index < len(per_message_metadata):
                            if per_message_metadata[current_assistant_message_index]["first_token_time"] is None:
                                per_message_metadata[current_assistant_message_index]["first_token_time"] = time.time()
                                logger.info(f"📝 First TEXT token for assistant message {current_assistant_message_index} at {per_message_metadata[current_assistant_message_index]['first_token_time']:.3f}")
                                # Also update global first_token_time for the first message (backward compatibility)
                                if current_assistant_message_index == 0 and first_token_time is None:
                                    first_token_time = per_message_metadata[0]["first_token_time"]

                # Track when assistant messages end
                if event.get("type") == "message_stop":
                    if current_assistant_message_index >= 0 and current_assistant_message_index < len(per_message_metadata):
                        per_message_metadata[current_assistant_message_index]["end_time"] = time.time()
                        logger.debug(f"📝 Assistant message {current_assistant_message_index} ended")
                
                # Track individual metadata events (per assistant message)
                if event.get("type") == "metadata":
                    event_data = event.get("data", {})
                    if current_assistant_message_index >= 0 and current_assistant_message_index < len(per_message_metadata):
                        msg_meta = per_message_metadata[current_assistant_message_index]

                        # Associate this metadata with the current assistant message
                        if "usage" in event_data:
                            msg_meta["usage"].update(event_data["usage"])
                        if "metrics" in event_data:
                            msg_meta["metrics"].update(event_data["metrics"])

                        # Calculate and store TTFT for this message NOW while we have timing context
                        # Use the first_token_time we captured from content_block_delta
                        # and the start_time from message_start
                        if msg_meta.get("first_token_time") and msg_meta.get("start_time"):
                            if "timeToFirstByteMs" not in msg_meta["metrics"]:
                                calculated_ttft = int((msg_meta["first_token_time"] - msg_meta["start_time"]) * 1000)
                                # For fast responses, TTFT should be at least the provider's reported latency portion
                                # If our calculated TTFT is < 10ms (event processing delay), use provider metrics
                                provider_latency = msg_meta["metrics"].get("latencyMs", 0)
                                if calculated_ttft < 10 and provider_latency > 100:
                                    # Estimate TTFT as ~30% of total latency (typical for LLM calls)
                                    msg_meta["metrics"]["timeToFirstByteMs"] = int(provider_latency * 0.3)
                                    logger.info(f"📊 Estimated TTFT for message {current_assistant_message_index}: {msg_meta['metrics']['timeToFirstByteMs']}ms (30% of {provider_latency}ms)")
                                elif calculated_ttft >= 10:
                                    msg_meta["metrics"]["timeToFirstByteMs"] = calculated_ttft
                                    logger.info(f"📊 Calculated TTFT for message {current_assistant_message_index}: {calculated_ttft}ms")

                        # ENRICH the metadata event sent to client with our calculated TTFT
                        # This ensures the client sees accurate per-message TTFT during streaming
                        if msg_meta["metrics"].get("timeToFirstByteMs"):
                            if "metrics" not in event_data:
                                event_data["metrics"] = {}
                            event_data["metrics"]["timeToFirstByteMs"] = msg_meta["metrics"]["timeToFirstByteMs"]
                            # Update the event with enriched data for client streaming
                            event = {"type": "metadata", "data": event_data}
                            logger.info(f"📊 Enriched metadata event for client with TTFT: {msg_meta['metrics']['timeToFirstByteMs']}ms")

                        logger.debug(f"📊 Metadata for message {current_assistant_message_index}: {msg_meta['metrics']}")
                    # Also accumulate for backward compatibility
                    if "usage" in event_data:
                        accumulated_metadata["usage"].update(event_data["usage"])
                    if "metrics" in event_data:
                        accumulated_metadata["metrics"].update(event_data["metrics"])
                
                # Collect metadata_summary event (don't send to client as-is)
                if event.get("type") == "metadata_summary":
                    event_data = event.get("data", {})
                    if "usage" in event_data:
                        accumulated_metadata["usage"].update(event_data["usage"])
                    if "metrics" in event_data:
                        accumulated_metadata["metrics"].update(event_data["metrics"])
                    if "first_token_time" in event_data:
                        first_token_time = event_data["first_token_time"]
                        # Associate first_token_time with first assistant message if we have one
                        if per_message_metadata and per_message_metadata[0]["first_token_time"] is None:
                            per_message_metadata[0]["first_token_time"] = first_token_time
                    # Don't yield this event to the client (will send final metadata before done)
                    continue
                
                # Check if this is the "done" event - send final metadata before it
                if event.get("type") == "done":
                    # Calculate end-to-end latency
                    stream_end_time = time.time()
                    
                    # Calculate time to first token for client display
                    time_to_first_token_ms = None
                    if first_token_time:
                        time_to_first_token_ms = int((first_token_time - stream_start_time) * 1000)
                    elif accumulated_metadata.get("metrics", {}).get("timeToFirstByteMs"):
                        time_to_first_token_ms = int(accumulated_metadata["metrics"]["timeToFirstByteMs"])
                    
                    # Send final metadata event to client with calculated TTFT
                    # This ensures the client receives the final metadata with accurate TTFT calculation
                    if accumulated_metadata.get("usage") or accumulated_metadata.get("metrics") or time_to_first_token_ms:
                        final_metadata = {
                            "usage": accumulated_metadata.get("usage", {}),
                            "metrics": {}
                        }
                        
                        # Include provider metrics if available
                        if accumulated_metadata.get("metrics"):
                            final_metadata["metrics"].update(accumulated_metadata["metrics"])
                        
                        # Add calculated time to first token (overrides provider value if we calculated it)
                        if time_to_first_token_ms is not None:
                            final_metadata["metrics"]["timeToFirstByteMs"] = time_to_first_token_ms
                        
                        # Add end-to-end latency to metrics for consistency
                        final_metadata["metrics"]["latencyMs"] = int((stream_end_time - stream_start_time) * 1000)
                        
                        # Send final metadata event to client (before done event)
                        final_metadata_event = {"type": "metadata", "data": final_metadata}
                        yield self._format_sse_event(final_metadata_event)

                # Format as SSE event and yield (including done event after metadata)
                sse_event = self._format_sse_event(event)
                yield sse_event

            # Calculate end-to-end latency (fallback if done event wasn't received)
            stream_end_time = time.time()

            # Flush buffered messages (turn-based session manager)
            # Note: In cloud mode with AgentCoreMemorySessionManager, the base manager's hooks
            # persist messages directly, so flush() typically returns None. This is expected.
            message_id = self._flush_session(session_manager)

            logger.info(f"💾 Flush returned message_id: {message_id}")

            # OPTIMIZATION: Calculate assistant message indices from message structure
            # Instead of querying AgentCore Memory (which adds 80-250ms latency),
            # we use the turn structure to calculate where assistant messages are.
            #
            # Turn structure (Converse API pattern):
            # - Position 0 (relative): user message
            # - Position 1 (relative): assistant message
            # - Position 2 (relative): user message (tool results) - if tools were used
            # - Position 3 (relative): assistant message - if tools were used
            # - ... continues alternating
            #
            # So assistant messages are at ODD relative positions: 1, 3, 5, ...
            # Absolute positions: initial_count + 1, initial_count + 3, initial_count + 5, ...
            #
            # This eliminates the need for post-stream AgentCore Memory queries!
            num_assistant_messages = current_assistant_message_index + 1 if current_assistant_message_index >= 0 else 0

            # Calculate assistant message absolute indices using the turn structure pattern
            # Assistant messages are at odd positions: initial_count + 1, initial_count + 3, ...
            assistant_message_ids = [
                initial_message_count + (2 * i + 1)  # Odd positions: 1, 3, 5, ...
                for i in range(num_assistant_messages)
            ]

            # Get final count for logging
            final_count = session_manager.message_count if hasattr(session_manager, 'message_count') else None

            logger.info(
                f"📊 Stream-based message tracking: "
                f"initial_count={initial_message_count}, "
                f"final_count={final_count}, "
                f"num_assistant_messages={num_assistant_messages}, "
                f"calculated_indices={assistant_message_ids}"
            )

            # Verify our calculation matches the actual final count
            # Expected: initial + 1 (user) + num_assistant * 2 - 1 (last assistant has no following tool result)
            # Simplified: initial + 2 * num_assistant
            if final_count is not None:
                expected_messages = 2 * num_assistant_messages  # user + assistant pairs
                actual_messages_added = final_count - initial_message_count
                if actual_messages_added != expected_messages:
                    logger.warning(
                        f"⚠️ Message count mismatch! "
                        f"Expected {expected_messages} messages added, but got {actual_messages_added}. "
                        f"Indices may be incorrect."
                    )

            # Set message_id to the last assistant message for backward compatibility
            if assistant_message_ids:
                message_id = assistant_message_ids[-1]

            # Always update session metadata (for last_model, message_count, etc.)
            await self._update_session_metadata(
                session_id=session_id,
                user_id=user_id,
                message_id=message_id,  # May be None if no assistant messages
                agent=strands_agent_wrapper  # Use wrapper instead of internal agent
            )

            # Store message-level metadata for assistant messages created during this stream
            # Use individual per-message metadata if we tracked it, otherwise fallback to accumulated
            message_ids_to_store = assistant_message_ids if assistant_message_ids else ([message_id] if message_id is not None else [])

            if message_ids_to_store:
                # Build list of metadata storage tasks for parallel execution
                metadata_tasks = []
                for idx, msg_id in enumerate(message_ids_to_store):
                    # Use individual metadata if we have it, otherwise use accumulated
                    if idx < len(per_message_metadata):
                        metadata_for_message = per_message_metadata[idx].copy()  # Copy to avoid mutation
                        # Use per-message timing for accurate latency calculation
                        # Each message has its own start_time, first_token_time, and end_time
                        msg_start_time = metadata_for_message.get("start_time") or stream_start_time
                        msg_end_time = metadata_for_message.get("end_time") or stream_end_time
                        first_token_for_message = metadata_for_message.get("first_token_time")

                        # For the FIRST message, enrich with global timeToFirstByteMs if available
                        # The provider's timeToFirstByteMs in metadata_summary is for the first LLM call
                        if idx == 0:
                            global_ttfb = accumulated_metadata.get("metrics", {}).get("timeToFirstByteMs")
                            if global_ttfb and "timeToFirstByteMs" not in metadata_for_message.get("metrics", {}):
                                if "metrics" not in metadata_for_message:
                                    metadata_for_message["metrics"] = {}
                                metadata_for_message["metrics"]["timeToFirstByteMs"] = global_ttfb
                                logger.info(f"📊 Enriched message 0 with global timeToFirstByteMs: {global_ttfb}ms")

                        # Fallback: if no first_token_time for this message, try global (for first message only)
                        if first_token_for_message is None and idx == 0:
                            first_token_for_message = first_token_time

                        first_token_str = f"{first_token_for_message:.3f}" if first_token_for_message is not None else "None"
                        logger.debug(
                            f"📊 Message {idx} timing: start={msg_start_time:.3f}, "
                            f"first_token={first_token_str}, "
                            f"end={msg_end_time:.3f}"
                        )
                    else:
                        # Fallback to accumulated metadata and global timing (backward compatibility)
                        metadata_for_message = accumulated_metadata
                        msg_start_time = stream_start_time
                        msg_end_time = stream_end_time
                        first_token_for_message = first_token_time if idx == 0 else None

                    logger.info(f"📊 Queuing message metadata for message_id={msg_id} (index {idx})")
                    metadata_tasks.append(
                        self._store_message_metadata(
                            session_id=session_id,
                            user_id=user_id,
                            message_id=msg_id,
                            accumulated_metadata=metadata_for_message,
                            stream_start_time=msg_start_time,
                            stream_end_time=msg_end_time,
                            first_token_time=first_token_for_message,
                            agent=strands_agent_wrapper  # Use wrapper instead of internal agent
                        )
                    )

                # Execute all metadata storage tasks in parallel
                # Use return_exceptions=True to prevent one failure from cancelling others
                if metadata_tasks:
                    results = await asyncio.gather(*metadata_tasks, return_exceptions=True)
                    # Log any failures (but don't raise - metadata failures shouldn't break streaming)
                    for idx, result in enumerate(results):
                        if isinstance(result, Exception):
                            logger.error(f"Failed to store metadata for message {message_ids_to_store[idx]}: {result}")

                logger.info(f"✅ Message metadata stored for {len(message_ids_to_store)} assistant messages (parallel)")

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
            return message_id
        return None

    def _get_initial_message_count(self, session_manager: Any) -> int:
        """
        Get the initial message count from session manager BEFORE streaming starts.

        This is a key optimization that eliminates post-stream AgentCore Memory queries.
        By capturing the message count at the start of streaming, we can calculate
        the indices of new messages without querying the database after streaming.

        The count is obtained from:
        1. TurnBasedSessionManager.message_count (initialized from AgentCore Memory at session start)
        2. LocalSessionBuffer or FileSessionManager (via list_messages)
        3. Fallback to 0 if no count is available

        Args:
            session_manager: Session manager instance

        Returns:
            int: Number of messages that existed before this stream started (0 if unknown)
        """
        # For TurnBasedSessionManager (cloud mode): use the pre-initialized message_count
        # This was queried from AgentCore Memory when the session manager was created
        if hasattr(session_manager, 'message_count'):
            count = session_manager.message_count
            logger.debug(f"Using TurnBasedSessionManager.message_count: {count}")
            return count

        # For LocalSessionBuffer: check the base manager
        if hasattr(session_manager, 'base_manager'):
            base_manager = session_manager.base_manager

            # Check if base manager has message_count (e.g., if it's also a TurnBasedSessionManager)
            if hasattr(base_manager, 'message_count'):
                count = base_manager.message_count
                logger.debug(f"Using base_manager.message_count: {count}")
                return count

            # For FileSessionManager: try to list messages
            if hasattr(base_manager, 'list_messages'):
                try:
                    # Get session_id from config or session_manager
                    session_id = None
                    if hasattr(base_manager, 'config'):
                        session_id = base_manager.config.session_id
                    elif hasattr(base_manager, 'session_id'):
                        session_id = base_manager.session_id
                    elif hasattr(session_manager, 'session_id'):
                        session_id = session_manager.session_id

                    if session_id:
                        messages = base_manager.list_messages(session_id, "default")
                        count = len(messages) if messages else 0
                        logger.debug(f"Using base_manager.list_messages count: {count}")
                        return count
                except Exception as e:
                    logger.warning(f"Failed to get message count from base_manager: {e}")

        # Direct session manager with list_messages
        if hasattr(session_manager, 'list_messages'):
            try:
                session_id = getattr(session_manager, 'session_id', None)
                if session_id:
                    messages = session_manager.list_messages(session_id, "default")
                    count = len(messages) if messages else 0
                    logger.debug(f"Using session_manager.list_messages count: {count}")
                    return count
            except Exception as e:
                logger.warning(f"Failed to get message count from session_manager: {e}")

        # Fallback: no count available (assume new session)
        logger.warning("Could not determine initial message count, defaulting to 0")
        return 0

    def _get_latest_message_id(self, session_manager: Any) -> Optional[int]:
        """
        Get the latest message ID from session manager without flushing

        This checks if messages have been flushed (e.g., during streaming when batch_size
        is reached) and returns the latest message ID if available.

        Args:
            session_manager: Session manager instance

        Returns:
            Latest message ID if available, or None
        """
        # Check if session manager has a method to get latest message ID without flushing
        if hasattr(session_manager, '_get_latest_message_id'):
            try:
                return session_manager._get_latest_message_id()
            except Exception:
                pass
        
        # For LocalSessionBuffer, check if base_manager has the method
        if hasattr(session_manager, 'base_manager'):
            base_manager = session_manager.base_manager
            if hasattr(base_manager, '_get_latest_message_id'):
                try:
                    return base_manager._get_latest_message_id()
                except Exception:
                    pass
        
        # Fallback: Try to get message count from session manager
        # This works for both TurnBasedSessionManager and LocalSessionBuffer
        if hasattr(session_manager, 'base_manager'):
            try:
                from apis.app_api.storage.paths import get_messages_dir
                from pathlib import Path
                
                # Get session_id from config
                if hasattr(session_manager.base_manager, 'config'):
                    session_id = session_manager.base_manager.config.session_id
                    messages_dir = get_messages_dir(session_id)
                    
                    if messages_dir.exists():
                        # Get all message files sorted by number
                        message_files = sorted(
                            messages_dir.glob("message_*.json"),
                            key=lambda p: int(p.stem.split("_")[1]) if p.stem.split("_")[1].isdigit() else 0
                        )
                        if message_files:
                            latest_file = message_files[-1]
                            message_num = int(latest_file.stem.split("_")[1])
                            return message_num
            except Exception:
                pass
        
        return None

    def _emergency_flush(self, session_manager: Any) -> None:
        """
        Emergency flush on error to prevent data loss

        Args:
            session_manager: Session manager instance
        """
        if hasattr(session_manager, 'flush'):
            try:
                session_manager.flush()
            except Exception as flush_error:
                logger.error(f"Failed to emergency flush: {flush_error}")

    def _create_error_event(self, error_message: str) -> str:
        """
        Create SSE error event with structured format

        Args:
            error_message: Error message

        Returns:
            str: SSE formatted error event
        """
        # Create structured error event
        error_event = StreamErrorEvent(
            error=error_message,
            code=ErrorCode.STREAM_ERROR,
            detail=None,
            recoverable=False
        )
        return f"event: error\ndata: {json.dumps(error_event.model_dump(exclude_none=True))}\n\n"

    async def _store_metadata_parallel(
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
        Store message and session metadata in parallel for better performance

        This method runs both storage operations concurrently using asyncio.gather(),
        reducing the total time spent on metadata persistence by ~50%.

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
            # Run both metadata storage operations in parallel
            # This reduces latency by executing both DB calls concurrently
            await asyncio.gather(
                self._store_message_metadata(
                    session_id=session_id,
                    user_id=user_id,
                    message_id=message_id,
                    accumulated_metadata=accumulated_metadata,
                    stream_start_time=stream_start_time,
                    stream_end_time=stream_end_time,
                    first_token_time=first_token_time,
                    agent=agent
                ),
                self._update_session_metadata(
                    session_id=session_id,
                    user_id=user_id,
                    message_id=message_id,
                    agent=agent
                ),
                return_exceptions=True  # Don't fail entire operation if one fails
            )
        except Exception as e:
            # Log but don't raise - metadata storage failures shouldn't break streaming
            logger.error(f"Failed to store metadata in parallel: {e}")

    async def _store_message_metadata(
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
        Store message-level metadata (token usage, latency, model info)

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
            from apis.app_api.sessions.services.metadata import store_message_metadata

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
            time_to_first_token_ms = None
            end_to_end_latency_ms = None

            # Log timing values for debugging
            logger.info(f"📊 _store_message_metadata timing: first_token_time={first_token_time}, stream_start_time={stream_start_time}, stream_end_time={stream_end_time}")
            logger.info(f"📊 _store_message_metadata metrics: {accumulated_metadata.get('metrics', {})}")

            # Get end-to-end latency from provider metrics if available (most accurate)
            # The provider's latencyMs is the total time for the API call
            provider_latency_ms = accumulated_metadata.get("metrics", {}).get("latencyMs")
            if provider_latency_ms:
                end_to_end_latency_ms = int(provider_latency_ms)
                logger.info(f"📊 Using provider latencyMs for E2E: {end_to_end_latency_ms}ms")
            else:
                # Fallback to calculated E2E from our timing
                end_to_end_latency_ms = int((stream_end_time - stream_start_time) * 1000)
                logger.info(f"📊 Calculated E2E latency: {end_to_end_latency_ms}ms")

            # Get time to first token
            # PRIORITY 1: Use provider's timeToFirstByteMs if available (most accurate)
            if accumulated_metadata.get("metrics", {}).get("timeToFirstByteMs"):
                time_to_first_token_ms = int(accumulated_metadata["metrics"]["timeToFirstByteMs"])
                logger.info(f"📊 Using provider timeToFirstByteMs: {time_to_first_token_ms}ms")
            # PRIORITY 2: Estimate TTFT as a portion of latency if we don't have it
            # This is a rough estimate but better than 0 or None
            # For most LLM calls, TTFT is typically 20-40% of total latency
            elif end_to_end_latency_ms and end_to_end_latency_ms > 100:
                # If E2E latency is available and substantial, estimate TTFT
                # We don't have actual TTFT so we can't store it accurately
                # Instead, log that we're missing it
                logger.info(f"📊 No TTFT available - provider did not send timeToFirstByteMs for this message")
                # Still create latency metrics with just E2E, using a placeholder of 0 for TTFT
                # This is better than losing all latency data
                time_to_first_token_ms = 0  # Indicates "not measured"

            # Create latency metrics if we have at least E2E latency
            if end_to_end_latency_ms is not None:
                latency_metrics = LatencyMetrics(
                    time_to_first_token=time_to_first_token_ms if time_to_first_token_ms is not None else 0,
                    end_to_end_latency=end_to_end_latency_ms
                )
                logger.info(f"📊 Created LatencyMetrics: TTFT={time_to_first_token_ms}ms, E2E={end_to_end_latency_ms}ms")
            else:
                # Log if we couldn't determine any latency
                logger.warning(
                    "Could not determine latency metrics - "
                    "no latencyMs from provider and no timing data available"
                )

            # Extract ModelInfo from agent and create pricing snapshot for cost tracking
            model_info = None
            pricing_snapshot = None
            cost = None

            if agent and hasattr(agent, 'model_config'):
                model_id = agent.model_config.model_id

                # Get pricing snapshot from managed models database
                pricing_snapshot = await self._get_pricing_snapshot(model_id)

                model_info = ModelInfo(
                    model_id=model_id,
                    model_name=self._extract_model_name(model_id),
                    model_version=self._extract_model_version(model_id),
                    pricing_snapshot=pricing_snapshot
                )

                # Calculate cost if we have both usage and pricing
                if token_usage and pricing_snapshot:
                    cost = self._calculate_message_cost(
                        usage=accumulated_metadata.get("usage", {}),
                        pricing=pricing_snapshot
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
                    attribution=attribution,
                    cost=cost
                )

                # Store metadata
                await store_message_metadata(
                    session_id=session_id,
                    user_id=user_id,
                    message_id=message_id,
                    message_metadata=message_metadata
                )

        except Exception as e:
            # Log but don't raise - metadata storage failures shouldn't break streaming
            logger.error(f"Failed to store message metadata: {e}")

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

    async def _get_pricing_snapshot(self, model_id: str) -> Optional[Dict[str, Any]]:
        """
        Get pricing snapshot from managed models database

        Args:
            model_id: Full model identifier

        Returns:
            PricingSnapshot dict or None if model not found
        """
        try:
            from apis.app_api.costs.pricing_config import create_pricing_snapshot
            from apis.app_api.messages.models import PricingSnapshot

            # Get pricing snapshot from managed models
            snapshot_dict = await create_pricing_snapshot(model_id)
            if not snapshot_dict:
                logger.warning(f"No pricing found for model: {model_id}")
                return None

            # Convert to PricingSnapshot model for validation
            snapshot = PricingSnapshot.model_validate(snapshot_dict)
            return snapshot

        except Exception as e:
            logger.error(f"Failed to get pricing snapshot for {model_id}: {e}")
            return None

    def _calculate_message_cost(
        self,
        usage: Dict[str, Any],
        pricing: Optional[Dict[str, Any]]
    ) -> Optional[float]:
        """
        Calculate message cost from usage and pricing

        Args:
            usage: Token usage dict
            pricing: Pricing snapshot (PricingSnapshot model)

        Returns:
            Total cost in USD or None if pricing unavailable
        """
        if not pricing:
            return None

        try:
            from apis.app_api.costs.calculator import CostCalculator

            # Convert PricingSnapshot model to dict for calculator
            if hasattr(pricing, 'model_dump'):
                pricing_dict = pricing.model_dump(by_alias=True)
            else:
                pricing_dict = pricing

            total_cost, _ = CostCalculator.calculate_message_cost(usage, pricing_dict)
            return total_cost

        except Exception as e:
            logger.error(f"Failed to calculate message cost: {e}")
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
        - preferences: Model/temperature/tools/system_prompt_hash from agent config
        - Auto-creates session metadata on first message

        Args:
            session_id: Session identifier
            user_id: User identifier
            message_id: Message ID that was just flushed
            agent: Agent instance for extracting model preferences
        """
        try:
            from apis.app_api.sessions.models import SessionMetadata, SessionPreferences
            from apis.app_api.sessions.services.metadata import store_session_metadata, get_session_metadata
            import hashlib

            logger.info(f"🔍 _update_session_metadata called for session {session_id}, message_id {message_id}")

            # Get existing metadata or create new
            existing = await get_session_metadata(session_id, user_id)

            if existing:
                logger.info(f"📄 Found existing metadata: messageCount={existing.message_count}, has_preferences={existing.preferences is not None}")
            else:
                logger.info(f"📄 No existing metadata found - creating new")

            # Calculate message count incrementally
            # NOTE: We cannot query AgentCore Memory immediately after flush due to eventual consistency.
            # The turn-based session manager calls create_message() then immediately calls list_messages(),
            # but the newly created message is not yet available for reading (can take several seconds).
            #
            # Instead, we use incremental counting:
            # - Each streaming turn creates 1 merged message in AgentCore Memory
            # - We increment the count by 1 per turn
            #
            # This count represents "turns" (user-assistant exchanges), not individual message events.
            # Tool use creates multiple content blocks within a single turn/message.
            if not existing:
                actual_message_count = 1
                logger.info(f"📊 First turn in session - message_count: {actual_message_count}")
            else:
                actual_message_count = existing.message_count + 1
                logger.info(f"📊 Incremental turn count: {existing.message_count} + 1 = {actual_message_count}")

            now = datetime.now(timezone.utc).isoformat()

            if not existing:
                # First message - create session metadata
                preferences = None
                if agent and hasattr(agent, 'model_config'):
                    logger.info(f"📦 Agent has model_config: model_id={agent.model_config.model_id}")

                    # Generate system prompt hash for tracking exact prompt version
                    # This hash represents the FINAL rendered system prompt (after date injection, etc.)
                    system_prompt_hash = None
                    if hasattr(agent, 'system_prompt') and agent.system_prompt:
                        system_prompt_hash = hashlib.md5(
                            agent.system_prompt.encode()
                        ).hexdigest()[:16]  # 16 char hash for uniqueness
                        logger.debug(f"Generated system_prompt_hash: {system_prompt_hash}")

                    # Extract enabled tools from agent
                    enabled_tools = getattr(agent, 'enabled_tools', None)

                    preferences = SessionPreferences(
                        last_model=agent.model_config.model_id,
                        last_temperature=getattr(agent.model_config, 'temperature', None),
                        enabled_tools=enabled_tools,
                        system_prompt_hash=system_prompt_hash
                    )
                    logger.info(f"✨ Created new preferences: last_model={preferences.last_model}")
                else:
                    logger.warning(f"⚠️ Agent is None or missing model_config")

                metadata = SessionMetadata(
                    session_id=session_id,
                    user_id=user_id,
                    title="New Conversation",  # Will be updated by frontend
                    status="active",
                    created_at=now,
                    last_message_at=now,
                    message_count=actual_message_count,
                    starred=False,
                    tags=[],
                    preferences=preferences
                )
            else:
                # Update existing - only update what changed
                preferences = existing.preferences
                if agent and hasattr(agent, 'model_config'):
                    logger.info(f"📦 Updating preferences with model_id={agent.model_config.model_id}")

                    # Update preferences if model/temperature/tools/system_prompt changed
                    prefs_dict = preferences.model_dump(by_alias=False) if preferences else {}
                    logger.info(f"📝 Existing prefs_dict: {prefs_dict}")

                    prefs_dict['last_model'] = agent.model_config.model_id
                    prefs_dict['last_temperature'] = getattr(agent.model_config, 'temperature', None)

                    # Update enabled_tools from agent
                    prefs_dict['enabled_tools'] = getattr(agent, 'enabled_tools', None)

                    # Update system_prompt_hash if system prompt changed
                    # This allows tracking when the prompt was modified during a conversation
                    if hasattr(agent, 'system_prompt') and agent.system_prompt:
                        new_hash = hashlib.md5(
                            agent.system_prompt.encode()
                        ).hexdigest()[:16]
                        # Only update if hash changed (prompt was modified)
                        if prefs_dict.get('system_prompt_hash') != new_hash:
                            logger.info(f"System prompt changed - updating hash from {prefs_dict.get('system_prompt_hash')} to {new_hash}")
                            prefs_dict['system_prompt_hash'] = new_hash

                    preferences = SessionPreferences(**prefs_dict)
                    logger.info(f"✨ Updated preferences: last_model={preferences.last_model}")
                else:
                    logger.warning(f"⚠️ Agent is None or missing model_config - keeping existing preferences")

                metadata = SessionMetadata(
                    session_id=session_id,
                    user_id=user_id,
                    title=existing.title,
                    status=existing.status,
                    created_at=existing.created_at,
                    last_message_at=now,
                    message_count=actual_message_count,
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

            logger.info(f"✅ Updated session metadata - last_model: {metadata.preferences.last_model if metadata.preferences else 'None'}, message_count: {metadata.message_count}")
            
            # Return message count for use as a fallback message_id
            return metadata.message_count

        except Exception as e:
            logger.error(f"Failed to update session metadata: {e}", exc_info=True)
            # Don't raise - metadata failures shouldn't break streaming
            return None
