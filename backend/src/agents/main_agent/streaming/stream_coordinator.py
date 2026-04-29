"""
Stream coordinator for managing agent streaming lifecycle
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from agents.main_agent.config.constants import EnvVars
from apis.shared.errors import ErrorCode, StreamErrorEvent, build_conversational_error_event

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
        user_id: str,
        main_agent_wrapper: Any = None,
        citations: Optional[List] = None,
        original_message: Optional[str] = None,
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
            main_agent_wrapper: MainAgent wrapper instance (has model_config, enabled_tools, etc.)
            citations: Optional list of citation dicts from RAG retrieval to persist with metadata
            original_message: Original user message before RAG augmentation (for clean UI display)

        Yields:
            str: SSE formatted events
        """
        # Set environment variables for browser session isolation
        os.environ[EnvVars.SESSION_ID] = session_id
        os.environ[EnvVars.USER_ID] = user_id

        # Track timing for latency metrics
        stream_start_time = time.time()
        first_token_time: Optional[float] = None

        # Accumulate metadata from stream
        accumulated_metadata: Dict[str, Any] = {"usage": {}, "metrics": {}}

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
                        per_message_metadata.append(
                            {
                                "usage": {},
                                "metrics": {},
                                "start_time": time.time(),  # When this message started
                                "first_token_time": None,  # When first token was received
                                "end_time": None,  # When this message ended
                            }
                        )
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
                                logger.info(
                                    f"📝 First TEXT token for assistant message {current_assistant_message_index} at {per_message_metadata[current_assistant_message_index]['first_token_time']:.3f}"
                                )
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
                                    logger.info(
                                        f"📊 Estimated TTFT for message {current_assistant_message_index}: {msg_meta['metrics']['timeToFirstByteMs']}ms (30% of {provider_latency}ms)"
                                    )
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

                # If the agent paused on an interrupt, surface one SSE event
                # per pending interrupt before the stream closes. The frontend
                # uses these to drive its prompts (OAuth popup, tool-approval
                # modal) and POSTs the user's response back to resume the turn.
                # Done before the metadata branch so the events land between
                # message_stop and the final metadata/done block. The
                # PausedTurnSnapshot is persisted once per pause regardless of
                # interrupt flavor, so any extractor's resume path can rebuild
                # the agent shape after a refresh / cache eviction.
                if event.get("type") == "done":
                    await self._persist_paused_turn_snapshot(
                        agent,
                        session_id=session_id,
                        user_id=user_id,
                        main_agent_wrapper=main_agent_wrapper,
                    )
                    for sse in await self._extract_oauth_required_events(
                        agent,
                        session_id=session_id,
                        user_id=user_id,
                    ):
                        yield sse
                    for sse in await self._extract_tool_approval_required_events(
                        agent,
                        session_id=session_id,
                        user_id=user_id,
                    ):
                        yield sse

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
                        final_metadata = {"usage": accumulated_metadata.get("usage", {}), "metrics": {}}

                        # Include provider metrics if available
                        if accumulated_metadata.get("metrics"):
                            final_metadata["metrics"].update(accumulated_metadata["metrics"])

                        # Add calculated time to first token (overrides provider value if we calculated it)
                        if time_to_first_token_ms is not None:
                            final_metadata["metrics"]["timeToFirstByteMs"] = time_to_first_token_ms

                        # Add end-to-end latency to metrics for consistency
                        final_metadata["metrics"]["latencyMs"] = int((stream_end_time - stream_start_time) * 1000)

                        # Calculate and add cost to metadata if we have usage and agent info
                        if main_agent_wrapper and hasattr(main_agent_wrapper, "model_config"):
                            model_id = main_agent_wrapper.model_config.model_id
                            usage_for_cost = accumulated_metadata.get("usage", {})
                            logger.info(f"💰 Cost calculation: model_id={model_id}, usage={usage_for_cost}")
                            try:
                                cost_result = await self._calculate_streaming_cost(model_id=model_id, usage=usage_for_cost)
                                if cost_result is not None:
                                    final_metadata["cost"] = cost_result
                                    logger.info(
                                        f"💰 Calculated streaming cost: ${cost_result['total']:.6f} (input=${cost_result['inputCost']:.6f}, output=${cost_result['outputCost']:.6f}) for {usage_for_cost.get('inputTokens', 0)} input, {usage_for_cost.get('outputTokens', 0)} output tokens"
                                    )
                            except Exception as cost_error:
                                logger.warning(f"Failed to calculate streaming cost: {cost_error}")

                        # Log cache metrics for performance monitoring
                        self._log_cache_metrics(usage=final_metadata.get("usage", {}), session_id=session_id)

                        # Send final metadata event to client (before done event)
                        final_metadata_event = {"type": "metadata", "data": final_metadata}
                        yield self._format_sse_event(final_metadata_event)

                # Intercept legacy "error" events from stream_processor and convert to conversational format
                # This ensures errors appear as assistant messages in the chat UI
                if event.get("type") == "error":
                    error_data = event.get("data", {})
                    error_message = error_data.get("error", "An error occurred")
                    error_detail = error_data.get("detail", "")
                    error_code_str = error_data.get("code", "stream_error")

                    # Map string code to ErrorCode enum
                    try:
                        error_code = ErrorCode(error_code_str)
                    except ValueError:
                        error_code = ErrorCode.STREAM_ERROR

                    # Create a synthetic exception for build_conversational_error_event
                    synthetic_error = Exception(f"{error_message}: {error_detail}" if error_detail else error_message)

                    # Build conversational error event
                    conv_error_event = build_conversational_error_event(
                        code=error_code, error=synthetic_error, session_id=session_id, recoverable=error_data.get("recoverable", False)
                    )

                    # Emit message events so error appears in chat
                    yield f'event: message_start\ndata: {{"role": "assistant"}}\n\n'
                    yield f'event: content_block_start\ndata: {{"contentBlockIndex": 0, "type": "text"}}\n\n'
                    yield f"event: content_block_delta\ndata: {json.dumps({'contentBlockIndex': 0, 'type': 'text', 'text': conv_error_event.message})}\n\n"
                    yield f'event: content_block_stop\ndata: {{"contentBlockIndex": 0}}\n\n'
                    yield f'event: message_stop\ndata: {{"stopReason": "error"}}\n\n'
                    yield conv_error_event.to_sse_format()
                    yield "event: done\ndata: {}\n\n"

                    # Persist error messages to session
                    try:
                        from strands.types.content import Message
                        from strands.types.session import SessionMessage

                        from agents.main_agent.session.session_factory import SessionFactory

                        persist_session_manager = SessionFactory.create_session_manager(session_id=session_id, user_id=user_id, caching_enabled=False)

                        # Extract user text from prompt (can be string or ContentBlock list)
                        if isinstance(prompt, str):
                            user_text = prompt
                        else:
                            # Extract text from ContentBlock list
                            user_text = " ".join(block.get("text", "") for block in prompt if isinstance(block, dict) and "text" in block)

                        user_msg: Message = {"role": "user", "content": [{"text": user_text}]}
                        assistant_msg: Message = {"role": "assistant", "content": [{"text": conv_error_event.message}]}

                        if hasattr(persist_session_manager, "base_manager") and hasattr(persist_session_manager.base_manager, "create_message"):
                            user_session_msg = SessionMessage.from_message(user_msg, 0)
                            assistant_session_msg = SessionMessage.from_message(assistant_msg, 1)
                            persist_session_manager.base_manager.create_message(session_id, "default", user_session_msg)
                            persist_session_manager.base_manager.create_message(session_id, "default", assistant_session_msg)
                            logger.info(f"💾 Saved intercepted error messages to session {session_id}")
                    except Exception as persist_error:
                        logger.error(f"Failed to persist intercepted error to session: {persist_error}")

                    # Skip the original error event and exit the loop - we've handled the error
                    return

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
            final_count = session_manager.message_count if hasattr(session_manager, "message_count") else None

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
                agent=main_agent_wrapper,  # Use wrapper instead of internal agent
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
                        logger.debug(f"📊 Message {idx} timing: start={msg_start_time:.3f}, first_token={first_token_str}, end={msg_end_time:.3f}")
                    else:
                        # Fallback to accumulated metadata and global timing (backward compatibility)
                        metadata_for_message = accumulated_metadata
                        msg_start_time = stream_start_time
                        msg_end_time = stream_end_time
                        first_token_for_message = first_token_time if idx == 0 else None

                    logger.info(f"📊 Queuing message metadata for message_id={msg_id} (index {idx})")
                    # Only attach citations to the first assistant message in the stream (RAG retrieval is for entire response)
                    citations_for_message = citations if idx == 0 else None
                    metadata_tasks.append(
                        self._store_message_metadata(
                            session_id=session_id,
                            user_id=user_id,
                            message_id=msg_id,
                            accumulated_metadata=metadata_for_message,
                            stream_start_time=msg_start_time,
                            stream_end_time=msg_end_time,
                            first_token_time=first_token_for_message,
                            agent=main_agent_wrapper,  # Use wrapper instead of internal agent
                            citations=citations_for_message,  # Pass citations for persistence
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

            # Store displayText for user message if original_message differs from augmented
            if original_message:
                user_message_index = initial_message_count  # User message is first in this turn
                try:
                    from apis.shared.sessions.metadata import store_user_display_text
                    await store_user_display_text(
                        session_id=session_id,
                        user_id=user_id,
                        message_id=user_message_index,
                        display_text=original_message,
                    )
                    logger.info(f"💾 Stored displayText for user message {user_message_index}")
                except Exception as e:
                    logger.error(f"Failed to store user displayText: {e}", exc_info=True)

            # Update compaction state if session manager supports it
            # This tracks input token usage and triggers compaction when threshold exceeded
            if hasattr(session_manager, "update_after_turn"):
                input_tokens = accumulated_metadata.get("usage", {}).get("inputTokens", 0)
                # Also include cache tokens for accurate context size tracking
                cache_read_tokens = accumulated_metadata.get("usage", {}).get("cacheReadInputTokens", 0)
                cache_write_tokens = accumulated_metadata.get("usage", {}).get("cacheWriteInputTokens", 0)
                total_input_tokens = input_tokens + cache_read_tokens + cache_write_tokens

                if total_input_tokens > 0:
                    try:
                        await session_manager.update_after_turn(total_input_tokens)
                        logger.info(f"   Compaction state updated: {total_input_tokens:,} input tokens")
                    except Exception as e:
                        logger.warning(f"Failed to update compaction state: {e}")

        except Exception as e:
            # Handle errors with emergency flush
            logger.error(f"Error in stream_response: {e}")
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")

            # Emergency flush: save buffered messages before losing them
            self._emergency_flush(session_manager)

            # Stream error as conversational assistant message for better UX
            error_event = build_conversational_error_event(code=ErrorCode.STREAM_ERROR, error=e, session_id=session_id, recoverable=True)

            # Emit message events so error appears in chat
            yield f'event: message_start\ndata: {{"role": "assistant"}}\n\n'
            yield f'event: content_block_start\ndata: {{"contentBlockIndex": 0, "type": "text"}}\n\n'
            yield f"event: content_block_delta\ndata: {json.dumps({'contentBlockIndex': 0, 'type': 'text', 'text': error_event.message})}\n\n"
            yield f'event: content_block_stop\ndata: {{"contentBlockIndex": 0}}\n\n'
            yield f'event: message_stop\ndata: {{"stopReason": "error"}}\n\n'
            yield error_event.to_sse_format()
            yield "event: done\ndata: {}\n\n"

            # Persist error messages to session
            try:
                from strands.types.content import Message
                from strands.types.session import SessionMessage

                from agents.main_agent.session.session_factory import SessionFactory

                persist_session_manager = SessionFactory.create_session_manager(session_id=session_id, user_id=user_id, caching_enabled=False)

                # Extract user text from prompt (can be string or ContentBlock list)
                if isinstance(prompt, str):
                    user_text = prompt
                else:
                    # Extract text from ContentBlock list
                    user_text = " ".join(block.get("text", "") for block in prompt if isinstance(block, dict) and "text" in block)

                user_msg: Message = {"role": "user", "content": [{"text": user_text}]}
                assistant_msg: Message = {"role": "assistant", "content": [{"text": error_event.message}]}

                if hasattr(persist_session_manager, "base_manager") and hasattr(persist_session_manager.base_manager, "create_message"):
                    user_session_msg = SessionMessage.from_message(user_msg, 0)
                    assistant_session_msg = SessionMessage.from_message(assistant_msg, 1)
                    persist_session_manager.base_manager.create_message(session_id, "default", user_session_msg)
                    persist_session_manager.base_manager.create_message(session_id, "default", assistant_session_msg)
                    logger.info(f"💾 Saved stream error messages to session {session_id}")
            except Exception as persist_error:
                logger.error(f"Failed to persist stream error to session: {persist_error}")

    async def _persist_paused_turn_snapshot(
        self,
        agent: Any,
        session_id: Optional[str],
        user_id: Optional[str],
        main_agent_wrapper: Any,
    ) -> None:
        """Persist a ``PausedTurnSnapshot`` capturing the agent's construction
        params so a resume after refresh / cache eviction rebuilds the same
        agent shape (matching tool registry) and lets Strands restore
        ``_interrupt_state`` from AgentCore Memory.

        Called once per pause from the ``done`` branch — shared across
        interrupt extractors so any flavor of pause (OAuth consent, tool
        approval, future variants) gets a snapshot. Multiple interrupts in
        the same turn share one snapshot; they were all built against the
        same agent. TTL matches AgentCore Identity's consent window so stale
        snapshots don't pin storage and a too-late resume returns a clean
        400.

        Persistence is best-effort: a DynamoDB write failure logs but does
        not break the live SSE flow.
        """
        from datetime import timedelta
        from apis.shared.sessions.metadata import set_paused_turn
        from apis.shared.sessions.models import PausedTurnSnapshot

        interrupt_state = getattr(agent, "_interrupt_state", None)
        if not interrupt_state or not getattr(interrupt_state, "activated", False):
            return
        if not (session_id and user_id):
            return
        snapshot_source = (
            getattr(main_agent_wrapper, "_construction_snapshot", None)
            if main_agent_wrapper
            else None
        )
        if not snapshot_source:
            return

        try:
            now = datetime.now(timezone.utc)
            snapshot = PausedTurnSnapshot(
                enabled_tools=snapshot_source.get("enabled_tools"),
                model_id=snapshot_source.get("model_id"),
                provider=snapshot_source.get("provider"),
                temperature=snapshot_source.get("temperature"),
                system_prompt=snapshot_source.get("system_prompt"),
                caching_enabled=snapshot_source.get("caching_enabled"),
                max_tokens=snapshot_source.get("max_tokens"),
                agent_type=snapshot_source.get("agent_type"),
                captured_at=now.isoformat(),
                expires_at=(now + timedelta(hours=1)).isoformat(),
            )
            await set_paused_turn(session_id, user_id, snapshot)
        except Exception as e:
            logger.error(
                "Failed to persist paused_turn snapshot for session %s: %s",
                session_id, e, exc_info=True,
            )

    async def _extract_oauth_required_events(
        self,
        agent: Any,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        triggering_message_id: Optional[str] = None,
    ) -> List[str]:
        """Yield one SSE-formatted `oauth_required` event per pending OAuth
        interrupt on the agent, persisting each one to session metadata so
        the frontend can rediscover them after a refresh.

        The Strands `_interrupt_state` is populated when `OAuthConsentHook`
        calls `event.interrupt(...)`. We look for interrupts whose `reason`
        carries `type: "oauth_required"` and translate them into the SSE
        shape the frontend already understands. Non-OAuth interrupts (other
        approval gates added later) are ignored here so they can be handled
        by their own SSE event types.

        The ``PausedTurnSnapshot`` is written separately by
        :meth:`_persist_paused_turn_snapshot` on the same ``done`` event —
        any pause flavor needs the snapshot, so it's hoisted out of here.

        Persistence is best-effort: a DynamoDB write failure logs but does
        not break the live SSE flow.
        """
        from apis.shared.oauth.models import OAuthRequiredEvent
        from apis.shared.sessions.metadata import add_pending_interrupt
        from apis.shared.sessions.models import PendingInterrupt

        interrupt_state = getattr(agent, "_interrupt_state", None)
        if not interrupt_state or not getattr(interrupt_state, "activated", False):
            return []

        events: List[str] = []
        for interrupt in interrupt_state.interrupts.values():
            reason = interrupt.reason or {}
            if not isinstance(reason, dict) or reason.get("type") != "oauth_required":
                continue
            provider_id = reason.get("providerId")
            authorization_url = reason.get("authorizationUrl")
            if not provider_id or not authorization_url:
                logger.warning(
                    "OAuth interrupt missing providerId or authorizationUrl: id=%s",
                    interrupt.id,
                )
                continue

            # Persist the breadcrumb before yielding so a client that loads
            # the session a moment later sees this interrupt. Only attempt
            # when we have session/user context — preview/anonymous flows
            # don't have a metadata record to write to.
            if session_id and user_id:
                try:
                    await add_pending_interrupt(
                        session_id=session_id,
                        user_id=user_id,
                        interrupt=PendingInterrupt(
                            interrupt_id=interrupt.id,
                            provider_id=provider_id,
                            triggering_message_id=triggering_message_id,
                            created_at=datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                except Exception as e:
                    logger.error(
                        "Failed to persist pending_interrupt %s: %s",
                        interrupt.id, e, exc_info=True,
                    )

            events.append(
                OAuthRequiredEvent(
                    provider_id=provider_id,
                    authorization_url=authorization_url,
                    interrupt_id=interrupt.id,
                ).to_sse_format()
            )
        return events

    async def _extract_tool_approval_required_events(
        self,
        agent: Any,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[str]:
        """Yield one SSE-formatted `tool_approval_required` event per pending
        per-tool approval interrupt on the agent, persisting each one to
        session metadata so the frontend can rediscover them after a refresh.

        The ``PausedTurnSnapshot`` needed to rebuild the agent on resume is
        written by :meth:`_persist_paused_turn_snapshot` on the same
        ``done`` event — independent of which interrupt flavor caused the
        pause.

        Persistence is best-effort: a DynamoDB write failure logs but does
        not break the live SSE flow.
        """
        from apis.shared.sessions.metadata import add_pending_interrupt
        from apis.shared.sessions.models import PendingInterrupt
        from apis.shared.tool_approval.models import ToolApprovalRequiredEvent

        interrupt_state = getattr(agent, "_interrupt_state", None)
        if not interrupt_state or not getattr(interrupt_state, "activated", False):
            return []

        events: List[str] = []
        for interrupt in interrupt_state.interrupts.values():
            reason = interrupt.reason or {}
            if not isinstance(reason, dict) or reason.get("type") != "tool_approval_required":
                continue
            tool_name = reason.get("toolName")
            if not tool_name:
                logger.warning(
                    "Tool approval interrupt missing toolName: id=%s", interrupt.id
                )
                continue

            tool_use_id = reason.get("toolUseId", "")
            tool_input = reason.get("toolInput")
            message = reason.get("message", "")

            # Persist the breadcrumb before yielding so a client that
            # refreshes mid-prompt can rehydrate the approve/decline UI.
            # Only attempt when we have session/user context — preview /
            # anonymous flows have no metadata record to write to.
            if session_id and user_id:
                try:
                    await add_pending_interrupt(
                        session_id=session_id,
                        user_id=user_id,
                        interrupt=PendingInterrupt(
                            interrupt_id=interrupt.id,
                            kind="tool_approval",
                            tool_use_id=tool_use_id,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            message=message,
                            created_at=datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                except Exception as e:
                    logger.error(
                        "Failed to persist tool_approval pending_interrupt %s: %s",
                        interrupt.id, e, exc_info=True,
                    )

            events.append(
                ToolApprovalRequiredEvent(
                    interrupt_id=interrupt.id,
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    message=message,
                ).to_sse_format()
            )
        return events

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

    def _log_cache_metrics(self, usage: Dict[str, Any], session_id: str) -> None:
        """
        Log cache performance metrics for monitoring and optimization.

        Logs detailed cache statistics including:
        - Cache read tokens (90% cost savings per token)
        - Cache write tokens (25% premium per token)
        - Cache hit rate (percentage of input tokens from cache)
        - Estimated cost savings from caching

        Args:
            usage: Token usage dictionary from model response
            session_id: Session identifier for log correlation
        """
        cache_read = usage.get("cacheReadInputTokens", 0)
        cache_write = usage.get("cacheWriteInputTokens", 0)
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)

        # Only log if we have cache activity
        if cache_read or cache_write:
            # Calculate cache hit rate
            # Total cacheable tokens = cache_read + cache_write + uncached input tokens
            # Note: inputTokens in Bedrock response = tokens AFTER last cache breakpoint (uncached)
            total_input = cache_read + cache_write + input_tokens
            cache_hit_rate = (cache_read / total_input * 100) if total_input > 0 else 0

            # Estimate cost impact (relative to non-cached scenario)
            # Cache read: 10% of base cost (90% savings)
            # Cache write: 125% of base cost (25% premium)
            # Regular input: 100% of base cost
            #
            # Cost without caching: all tokens at 100%
            # Cost with caching: cache_read * 0.10 + cache_write * 1.25 + input * 1.0
            cost_without_cache = total_input  # Normalized to 1.0 per token
            cost_with_cache = (cache_read * 0.10) + (cache_write * 1.25) + input_tokens
            cost_savings_pct = ((cost_without_cache - cost_with_cache) / cost_without_cache * 100) if cost_without_cache > 0 else 0

            logger.info(
                f"📦 Cache metrics [session={session_id[:8]}...]: "
                f"read={cache_read:,} tokens, write={cache_write:,} tokens, "
                f"uncached={input_tokens:,} tokens, output={output_tokens:,} tokens | "
                f"hit_rate={cache_hit_rate:.1f}%, est_savings={cost_savings_pct:.1f}%"
            )

            # Log warning if cache write with no reads (first request or cache miss)
            if cache_write > 0 and cache_read == 0:
                logger.debug(f"📦 Cache write only (new cache entry or miss) - subsequent requests should see cache reads")
        else:
            # No cache activity - might be non-Bedrock model or caching disabled
            if input_tokens > 0:
                logger.info(
                    f"📦 No cache activity [session={session_id[:8]}...]: "
                    f"input={input_tokens:,} tokens, output={output_tokens:,} tokens "
                    f"(usage keys: {list(usage.keys())})"
                )

    def _flush_session(self, session_manager: Any) -> Optional[int]:
        """
        Flush session manager if it supports buffering

        Args:
            session_manager: Session manager instance

        Returns:
            Message ID of the flushed message, or None if unavailable
        """
        if hasattr(session_manager, "flush"):
            message_id = session_manager.flush()
            return message_id
        return None

    def _get_initial_message_count(self, session_manager: Any) -> int:
        """
        Get the GLOBAL initial message count BEFORE streaming starts.

        Returns the total number of messages across ALL agents (default + voice)
        in the session, because metadata retrieval in get_messages_from_cloud()
        uses global enumerate indices across all agents' messages.

        The agent-specific message_count (from TurnBasedSessionManager) only
        counts messages for the "default" agent, which causes index mismatches
        in mixed voice+text sessions.  We prefer list_messages() which returns
        ALL messages regardless of agent_id.

        Args:
            session_manager: Session manager instance

        Returns:
            int: Number of messages that existed before this stream started (0 if unknown)
        """
        # Prefer list_messages() for global count — it returns ALL messages
        # regardless of agent_id, matching how get_messages_from_cloud() retrieves them.
        session_id = self._resolve_session_id(session_manager)
        if session_id:
            lister = self._resolve_list_messages(session_manager)
            if lister:
                try:
                    messages = lister(session_id, "default")
                    count = len(messages) if messages else 0
                    logger.info(f"Using global list_messages count: {count}")
                    return count
                except Exception as e:
                    logger.warning(f"Failed to get global message count: {e}")

        # Fallback to agent-specific message_count (may undercount in mixed sessions)
        if hasattr(session_manager, "message_count"):
            count = session_manager.message_count
            logger.debug(f"Fallback to TurnBasedSessionManager.message_count: {count}")
            return count

        if hasattr(session_manager, "base_manager"):
            base_manager = session_manager.base_manager
            if hasattr(base_manager, "message_count"):
                count = base_manager.message_count
                logger.debug(f"Fallback to base_manager.message_count: {count}")
                return count

        logger.warning("Could not determine initial message count, defaulting to 0")
        return 0

    @staticmethod
    def _resolve_session_id(session_manager: Any) -> Optional[str]:
        """Extract session_id from a session manager."""
        for mgr in (session_manager, getattr(session_manager, "base_manager", None)):
            if mgr is None:
                continue
            if hasattr(mgr, "config") and hasattr(mgr.config, "session_id"):
                return mgr.config.session_id
            if hasattr(mgr, "session_id"):
                return mgr.session_id
        return None

    @staticmethod
    def _resolve_list_messages(session_manager: Any) -> Optional[callable]:
        """Find list_messages callable on a session manager."""
        for mgr in (session_manager, getattr(session_manager, "base_manager", None)):
            if mgr and hasattr(mgr, "list_messages"):
                return mgr.list_messages
        return None

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
        if hasattr(session_manager, "_get_latest_message_id"):
            try:
                return session_manager._get_latest_message_id()
            except Exception:
                pass

        return None

    def _emergency_flush(self, session_manager: Any) -> None:
        """
        Emergency flush on error to prevent data loss

        Args:
            session_manager: Session manager instance
        """
        if hasattr(session_manager, "flush"):
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
        error_event = StreamErrorEvent(error=error_message, code=ErrorCode.STREAM_ERROR, detail=None, recoverable=False)
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
        agent: Any = None,
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
                    agent=agent,
                ),
                self._update_session_metadata(session_id=session_id, user_id=user_id, message_id=message_id, agent=agent),
                return_exceptions=True,  # Don't fail entire operation if one fails
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
        agent: Any = None,
        citations: Optional[List] = None,
    ) -> None:
        """
        Store message-level metadata (token usage, latency, model info, citations)

        Args:
            session_id: Session identifier
            user_id: User identifier
            message_id: Message ID from session manager
            accumulated_metadata: Metadata collected during streaming
            stream_start_time: Timestamp when stream started
            stream_end_time: Timestamp when stream ended
            first_token_time: Timestamp of first token received
            agent: Agent instance for extracting model info
            citations: Optional list of citation dicts from RAG retrieval
        """
        try:
            from apis.app_api.messages.models import Attribution, LatencyMetrics, MessageMetadata, ModelInfo, TokenUsage
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
                    cache_write_input_tokens=usage_data.get("cacheWriteInputTokens"),
                )

            # Build LatencyMetrics if we have timing data
            latency_metrics = None
            time_to_first_token_ms = None
            end_to_end_latency_ms = None

            # Log timing values for debugging
            logger.info(
                f"📊 _store_message_metadata timing: first_token_time={first_token_time}, stream_start_time={stream_start_time}, stream_end_time={stream_end_time}"
            )
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
                    time_to_first_token=time_to_first_token_ms if time_to_first_token_ms is not None else 0, end_to_end_latency=end_to_end_latency_ms
                )
                logger.info(f"📊 Created LatencyMetrics: TTFT={time_to_first_token_ms}ms, E2E={end_to_end_latency_ms}ms")
            else:
                # Log if we couldn't determine any latency
                logger.warning("Could not determine latency metrics - no latencyMs from provider and no timing data available")

            # Extract ModelInfo from agent and create pricing snapshot for cost tracking
            model_info = None
            pricing_snapshot = None
            cost = None

            if agent and hasattr(agent, "model_config"):
                model_id = agent.model_config.model_id

                # Get pricing snapshot from managed models database
                pricing_snapshot = await self._get_pricing_snapshot(model_id)

                # Extract provider from model config
                provider = None
                if hasattr(agent.model_config, "get_provider"):
                    provider = agent.model_config.get_provider().value

                model_info = ModelInfo(
                    model_id=model_id,
                    model_name=self._extract_model_name(model_id),
                    model_version=self._extract_model_version(model_id),
                    provider=provider,
                    pricing_snapshot=pricing_snapshot,
                )

                # Calculate cost if we have both usage and pricing
                if token_usage and pricing_snapshot:
                    cost_result = self._calculate_message_cost(usage=accumulated_metadata.get("usage", {}), pricing=pricing_snapshot)
                    if cost_result is not None:
                        cost = cost_result

            # Create Attribution for cost tracking foundation
            attribution = Attribution(
                user_id=user_id,
                session_id=session_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
                # organization_id will be added when multi-tenant billing is implemented
                # tags will be added for cost allocation features
            )

            # Create MessageMetadata
            if token_usage or latency_metrics or model_info or citations:
                message_metadata = MessageMetadata(
                    latency=latency_metrics,
                    token_usage=token_usage,
                    model_info=model_info,
                    attribution=attribution,
                    cost=cost,
                    citations=citations,  # Include citations from RAG retrieval
                )

                # Store metadata
                await store_message_metadata(session_id=session_id, user_id=user_id, message_id=message_id, message_metadata=message_metadata)

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
            "claude-3-haiku": "Claude 3 Haiku",
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

    def _calculate_message_cost(self, usage: Dict[str, Any], pricing: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Calculate message cost from usage and pricing

        Args:
            usage: Token usage dict
            pricing: Pricing snapshot (PricingSnapshot model)

        Returns:
            Dict with total cost and breakdown, or None if pricing unavailable
        """
        if not pricing:
            return None

        try:
            from apis.app_api.costs.calculator import CostCalculator

            # Convert PricingSnapshot model to dict for calculator
            if hasattr(pricing, "model_dump"):
                pricing_dict = pricing.model_dump(by_alias=True)
            else:
                pricing_dict = pricing

            total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing_dict)
            return {
                "total": total_cost,
                "inputCost": breakdown.input_cost,
                "outputCost": breakdown.output_cost,
                "cacheReadCost": breakdown.cache_read_cost,
                "cacheWriteCost": breakdown.cache_write_cost,
            }

        except Exception as e:
            logger.error(f"Failed to calculate message cost: {e}")
            return None

    async def _calculate_streaming_cost(self, model_id: str, usage: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Calculate cost for streaming response to send to client in real-time.

        This is a lightweight cost calculation used during streaming to show
        cost immediately in the UI. The full cost calculation with pricing
        snapshot is done in _store_message_metadata for persistence.

        Args:
            model_id: Model identifier
            usage: Token usage dict from streaming

        Returns:
            Dict with total cost and breakdown, or None if pricing unavailable
        """
        if not usage:
            return None

        try:
            # Get pricing snapshot for this model
            pricing = await self._get_pricing_snapshot(model_id)
            if not pricing:
                logger.warning(f"No pricing found for model {model_id}")
                return None

            # Log pricing for debugging
            if hasattr(pricing, "model_dump"):
                pricing_dict = pricing.model_dump(by_alias=True)
            else:
                pricing_dict = pricing
            logger.info(
                f"💰 Pricing for {model_id}: input=${pricing_dict.get('inputPricePerMtok', 0)}/M, output=${pricing_dict.get('outputPricePerMtok', 0)}/M, cache_read=${pricing_dict.get('cacheReadPricePerMtok', 0)}/M"
            )

            # Calculate cost using the calculator
            return self._calculate_message_cost(usage, pricing)

        except Exception as e:
            logger.warning(f"Failed to calculate streaming cost: {e}")
            return None

    async def _update_session_metadata(self, session_id: str, user_id: str, message_id: int, agent: Any = None) -> None:
        """Update per-turn session activity (lastMessageAt, messageCount, preferences).

        Delegates to ``update_session_activity``, which uses targeted writes
        so concurrent writers (title-gen, pending-interrupt persistence)
        cannot be clobbered. Pre-create is handled at /invocations entry, so
        no lazy-create branch is needed here.
        """
        try:
            import hashlib

            from apis.shared.sessions.metadata import update_session_activity

            last_model = None
            last_temperature = None
            enabled_tools = None
            system_prompt_hash = None
            if agent and hasattr(agent, "model_config"):
                last_model = agent.model_config.model_id
                last_temperature = getattr(agent.model_config, "temperature", None)
                enabled_tools = getattr(agent, "enabled_tools", None)
                if hasattr(agent, "system_prompt") and agent.system_prompt:
                    system_prompt_hash = hashlib.md5(agent.system_prompt.encode()).hexdigest()[:16]
            else:
                logger.warning("⚠️ Agent is None or missing model_config — skipping preference update")

            await update_session_activity(
                session_id=session_id,
                user_id=user_id,
                last_model=last_model,
                last_temperature=last_temperature,
                enabled_tools=enabled_tools,
                system_prompt_hash=system_prompt_hash,
            )
        except Exception as e:
            logger.error(f"Failed to update session metadata: {e}", exc_info=True)
            # Don't raise — metadata failures shouldn't break streaming.
