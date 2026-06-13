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
from apis.shared.errors import (
    ConversationalErrorEvent,
    ErrorCode,
    StreamErrorEvent,
    build_conversational_error_event,
)

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
        # Wall-clock turn start as a tz-aware datetime. Used post-turn to
        # tell which artifacts (HEAD.updated_at) were touched *this* turn
        # vs. carried over from earlier turns in the same session.
        turn_start_dt = datetime.now(timezone.utc)
        first_token_time: Optional[float] = None

        # Set when a create_artifact / update_artifact tool call is seen
        # this turn — the only turns that need the post-turn artifacts
        # query + `artifact` SSE emit. Normal turns pay nothing.
        artifact_tool_invoked = False

        # MCP Apps (PR #3): toolUseId -> tool name, learned from tool_use /
        # content_block_start events so a later tool_result can be matched
        # back to its catalog `_meta.ui`. `ui_resource_emitted` dedupes the
        # `ui_resource` SSE per toolUseId (a tool result can surface twice —
        # once via the lifecycle path, once via the tool path). Both stay
        # empty and unused unless AGENTCORE_MCP_APPS_HOST_ENABLED=true.
        ui_tool_use_names: Dict[str, str] = {}
        ui_resource_emitted: set[str] = set()
        # Dedupes the instant header-only `ui_resource` shell (empty html, no
        # `resources/read`) emitted at `content_block_start` so the App frame's
        # header replaces the tool rail immediately — separate from
        # `ui_resource_emitted` so it never blocks the full html-bearing emit.
        ui_header_emitted: set[str] = set()
        # MCP Apps (streaming tool input, SEP-1865): a UI tool's frame is
        # mounted early at its `content_block_start` so the App's bridge is
        # live *while* the model streams the tool's arguments. We map a
        # tool-use block's index -> toolUseId (deltas carry only the index)
        # and accumulate the raw `toolUse.input` fragments per toolUseId, then
        # emit healed `ui_tool_input_partial` SSEs so a progressively-rendering
        # App (e.g. Excalidraw's guided camera tour) animates as args arrive.
        ui_block_index_to_tool_use_id: Dict[int, str] = {}
        ui_partial_input_acc: Dict[str, str] = {}

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

        # MCP Apps PR #5: subscribe this conversation stream to the
        # app-initiated tool-event broker so a `tools/call` proxied from an
        # embedded MCP App surfaces as a tool_use/tool_result card in the
        # live thread (and any buffered while no stream was active flush
        # in here). Inert + zero-cost unless the host flag is on; removed
        # in the method-level `finally` so a dropped stream can't leak.
        app_event_queue = None
        try:
            from agents.main_agent.integrations.mcp_apps import (
                is_mcp_apps_host_enabled,
            )

            if is_mcp_apps_host_enabled():
                from apis.shared.mcp_apps.broker import (
                    get_app_tool_event_broker,
                )

                app_event_queue = get_app_tool_event_broker().add_subscriber(
                    session_id
                )
        except Exception as e:  # noqa: BLE001 - never block the stream
            logger.warning("MCP Apps broker subscribe failed: %s", e)
            app_event_queue = None

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

                # Note whether the agent invoked an artifact authoring
                # tool this turn. Gates the post-turn artifacts query so
                # only artifact turns pay for it.
                if not artifact_tool_invoked and event.get("type") == "tool_use":
                    tool_name = (
                        event.get("data", {}).get("tool_use", {}).get("name")
                    )
                    if tool_name in ("create_artifact", "update_artifact"):
                        artifact_tool_invoked = True

                # MCP Apps (PR #3): remember toolUseId -> tool name so a
                # later tool_result can be matched to its catalog `_meta.ui`.
                # Captured from both event flavors that carry the pairing:
                # the `tool_use` event (data.tool_use) and the
                # `content_block_start` of a tool-use block (data.toolUse).
                etype = event.get("type")
                if etype == "tool_use":
                    td = event.get("data", {}).get("tool_use", {})
                    tn = td.get("name")
                    tuid = td.get("tool_use_id") or td.get("toolUseId")
                    if tn and tuid:
                        ui_tool_use_names[tuid] = tn
                elif etype == "content_block_start":
                    bd = event.get("data", {})
                    if bd.get("type") == "tool_use":
                        tu = bd.get("toolUse", {})
                        tn = tu.get("name")
                        tuid = tu.get("toolUseId") or tu.get("tool_use_id")
                        if tn and tuid:
                            ui_tool_use_names[tuid] = tn
                        # Map block index -> toolUseId so streaming
                        # `content_block_delta`s (which carry only the index)
                        # can be attributed to a toolUseId for partial input.
                        bidx = bd.get("contentBlockIndex")
                        if tuid and bidx is not None:
                            ui_block_index_to_tool_use_id[bidx] = tuid

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

                # Collect metadata_summary event (don't send to client as-is).
                #
                # NOTE: metadata_summary carries Strands' EventLoopMetrics
                # `accumulated_usage`, which sums each LLM call's full
                # context-size across the turn (and across the agent's
                # whole lifetime, per Strands' docs). For a 2-call tool
                # turn with call_1.input=1000 and call_2.input=2500,
                # accumulated_usage.inputTokens=3500 — but the *current*
                # context occupancy is 2500, not 3500. We deliberately do
                # NOT update accumulated_metadata["usage"] / ["metrics"]
                # from this event: stream_coordinator's accumulated_metadata
                # drives (a) the final SSE `usage` the frontend uses for
                # the context-% badge and (b) the compaction trigger —
                # both want "current context size", which the per-call
                # `metadata` events already provide via last-write-wins
                # `.update()`. Per-message cost attribution rides
                # per_message_metadata (per-call) and is unaffected.
                # We only keep the first_token_time backstop.
                if event.get("type") == "metadata_summary":
                    event_data = event.get("data", {})
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

                        # Cost: sum the FINAL usage of each assistant message in
                        # this turn and price it. We deliberately price each
                        # message independently and sum, instead of pricing
                        # the cumulative usage once, because Strands emits
                        # multiple metadata events per message (intermediate
                        # + cumulative) and the cumulative usage on the last
                        # event already includes prior messages' input
                        # tokens. Per-message pricing matches what gets
                        # persisted (one C# record per assistant message).
                        if main_agent_wrapper and hasattr(main_agent_wrapper, "model_config"):
                            model_id = main_agent_wrapper.model_config.model_id
                            try:
                                turn_total = 0.0
                                turn_input_cost = 0.0
                                turn_output_cost = 0.0
                                turn_cache_read_cost = 0.0
                                turn_cache_write_cost = 0.0
                                for msg_idx, msg_meta in enumerate(per_message_metadata):
                                    msg_usage = msg_meta.get("usage") or {}
                                    if not msg_usage:
                                        continue
                                    msg_cost = await self._calculate_streaming_cost(
                                        model_id=model_id,
                                        usage=msg_usage,
                                    )
                                    if msg_cost is None:
                                        continue
                                    turn_total += msg_cost.get("total", 0.0)
                                    turn_input_cost += msg_cost.get("inputCost", 0.0)
                                    turn_output_cost += msg_cost.get("outputCost", 0.0)
                                    turn_cache_read_cost += msg_cost.get("cacheReadCost", 0.0)
                                    turn_cache_write_cost += msg_cost.get("cacheWriteCost", 0.0)
                                    logger.info(
                                        f"💰 Per-message cost (msg_idx={msg_idx}): ${msg_cost['total']:.6f} "
                                        f"for {msg_usage.get('inputTokens', 0)} input, {msg_usage.get('outputTokens', 0)} output tokens"
                                    )
                                if turn_total > 0:
                                    final_metadata["cost"] = {
                                        "total": turn_total,
                                        "inputCost": turn_input_cost,
                                        "outputCost": turn_output_cost,
                                        "cacheReadCost": turn_cache_read_cost,
                                        "cacheWriteCost": turn_cache_write_cost,
                                    }
                                    logger.info(
                                        f"💰 Turn total cost: ${turn_total:.6f} across {len(per_message_metadata)} message(s)"
                                    )
                            except Exception as cost_error:
                                logger.warning(f"Failed to calculate turn cost: {cost_error}")

                            # Surface the model's context window so the
                            # frontend session-cost badge can show "% of
                            # context used" without an extra round-trip.
                            try:
                                from apis.shared.costs.pricing_config import get_model_by_model_id
                                model_record = await get_model_by_model_id(model_id)
                                if model_record is not None:
                                    max_input_tokens = getattr(model_record, "max_input_tokens", None)
                                    if max_input_tokens:
                                        final_metadata["contextWindow"] = int(max_input_tokens)
                            except Exception as ctx_err:
                                logger.debug(f"Skipping contextWindow lookup: {ctx_err}")

                            # Per-turn context attribution (system / tools /
                            # messages), computed by ContextAttributionHook at
                            # BeforeModelCallEvent and stashed on the agent.
                            # Partitions sum to `total`; the frontend pairs it
                            # with `contextWindow` above for free-space.
                            try:
                                from agents.main_agent.session.hooks.context_attribution import (
                                    get_context_breakdown,
                                )
                                breakdown = get_context_breakdown(agent)
                                if breakdown is not None:
                                    final_metadata["contextBreakdown"] = breakdown
                            except Exception as br_err:
                                logger.debug(f"Skipping contextBreakdown: {br_err}")

                        # Log cache metrics for performance monitoring
                        self._log_cache_metrics(usage=final_metadata.get("usage", {}), session_id=session_id)

                        # Send final metadata event to client (before done event)
                        final_metadata_event = {"type": "metadata", "data": final_metadata}
                        yield self._format_sse_event(final_metadata_event)

                    # Update compaction state after the final metadata event so
                    # the badge updates first, then the divider drops in. If the
                    # checkpoint advanced on this turn, emit a `compaction` SSE
                    # so the frontend can place an inline "earlier messages
                    # summarized" divider. Fires after metadata, before done.
                    #
                    # CAUTION: do NOT replace this with Strands'
                    # AgentResult.context_size / EventLoopMetrics.latest_context_size.
                    # Both return ONLY `inputTokens` from the last cycle —
                    # under Bedrock prompt caching that's the uncached
                    # suffix only, so a 50k-token fully-cached context
                    # reports ~50 (inputTokens) and hides ~49,950 in
                    # cacheReadInputTokens. Summing all three buckets
                    # below is the only correct "current context size"
                    # under caching.
                    if hasattr(session_manager, "update_after_turn"):
                        usage = accumulated_metadata.get("usage", {})
                        total_input_tokens = (
                            usage.get("inputTokens", 0)
                            + usage.get("cacheReadInputTokens", 0)
                            + usage.get("cacheWriteInputTokens", 0)
                        )
                        if total_input_tokens > 0:
                            try:
                                current_messages = getattr(agent, "messages", None)
                                compaction_result = await session_manager.update_after_turn(
                                    total_input_tokens,
                                    current_messages=current_messages,
                                )
                                logger.info(f"   Compaction state updated: {total_input_tokens:,} input tokens")
                                if compaction_result is not None:
                                    compaction_payload = {
                                        "type": "compaction",
                                        "previousCheckpoint": compaction_result.previous_checkpoint,
                                        "newCheckpoint": compaction_result.new_checkpoint,
                                        "summarizedTurns": compaction_result.summarized_turns,
                                        "inputTokens": compaction_result.input_tokens,
                                    }
                                    yield f"event: compaction\ndata: {json.dumps(compaction_payload)}\n\n"
                            except Exception as e:
                                logger.warning(f"Failed to update compaction state: {e}")

                # Emit one `artifact` SSE per artifact created/updated this
                # turn. Placed after the compaction emit (so it lands with
                # the other post-message_stop side-channel events) and
                # before `done`. Best-effort: a lookup failure logs and is
                # swallowed so it never breaks the live stream.
                if event.get("type") == "done" and artifact_tool_invoked:
                    # Anchor every artifact touched this turn to the turn's
                    # final assistant message. `done` lands after the last
                    # `message_stop`, so current_assistant_message_index is
                    # final here; this is the same odd-position index the
                    # post-loop block uses for per-message metadata
                    # (assistant_message_ids[-1]), which the messages
                    # endpoint re-derives as `idx` on reload.
                    produced_by_message_index = (
                        initial_message_count
                        + 2 * current_assistant_message_index
                        + 1
                        if current_assistant_message_index >= 0
                        else None
                    )
                    for sse in await self._extract_artifact_events(
                        session_id=session_id,
                        user_id=user_id,
                        turn_start=turn_start_dt,
                        produced_by_message_index=produced_by_message_index,
                    ):
                        yield sse

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

                    # When stream_processor's force_stop classifier (in
                    # _format_force_stop_message) already produced friendly
                    # user-facing markdown — recognizable by the leading "⚠️"
                    # — pass it through unwrapped. The generic
                    # build_conversational_error_event template would
                    # otherwise wrap it in a second "⚠️ Something went
                    # wrong" + blockquote, double-marking the message and
                    # appending a ceremonial "Please try again." The
                    # unclassified "Agent force-stopped: {raw}" fallthrough
                    # has no warning prefix and still goes through the
                    # generic wrapper below.
                    recoverable = error_data.get("recoverable", False)
                    if (
                        error_code == ErrorCode.AGENT_ERROR
                        and error_message
                        and error_message.lstrip().startswith("⚠️")
                    ):
                        metadata: Optional[Dict[str, Any]] = (
                            {"session_id": session_id} if session_id else None
                        )
                        conv_error_event = ConversationalErrorEvent(
                            code=error_code,
                            message=error_message,
                            recoverable=recoverable,
                            metadata=metadata,
                        )
                    else:
                        # Create a synthetic exception for build_conversational_error_event
                        synthetic_error = Exception(
                            f"{error_message}: {error_detail}" if error_detail else error_message
                        )

                        # Build conversational error event
                        conv_error_event = build_conversational_error_event(
                            code=error_code,
                            error=synthetic_error,
                            session_id=session_id,
                            recoverable=recoverable,
                        )

                    if error_code == ErrorCode.MAX_TOKENS:
                        # No verbose assistant bubble for truncation. The model
                        # stream already emitted its own message_stop
                        # (stopReason max_tokens) for the partial, so do NOT
                        # emit a second synthetic message_stop here — a
                        # duplicate with no active builder flips the client
                        # parser into an error state and drops the
                        # stream_error below. Just emit the stream_error
                        # signal (frontend shows the inline "response length
                        # limit reached" notice + Continue on the partial) and
                        # done; `done` finalizes any still-open builder.
                        yield conv_error_event.to_sse_format()
                        # Durable marker so the Continue affordance survives a
                        # page refresh (the partial itself is already in
                        # AgentCore Memory). Best-effort; never blocks the
                        # stream. Cleared at the start of the next non-resume
                        # turn (see invocations route).
                        try:
                            from apis.shared.sessions.metadata import set_truncated_turn
                            await set_truncated_turn(session_id, user_id)
                        except Exception as marker_err:
                            logger.error(
                                "max_tokens: failed to persist truncated_turn marker for session %s: %s",
                                session_id, marker_err, exc_info=True,
                            )
                        yield "event: done\ndata: {}\n\n"
                    else:
                        # Other errors still surface as a conversational
                        # assistant message in the chat.
                        yield f'event: message_start\ndata: {{"role": "assistant"}}\n\n'
                        yield f'event: content_block_start\ndata: {{"contentBlockIndex": 0, "type": "text"}}\n\n'
                        yield f"event: content_block_delta\ndata: {json.dumps({'contentBlockIndex': 0, 'type': 'text', 'text': conv_error_event.message})}\n\n"
                        yield f'event: content_block_stop\ndata: {{"contentBlockIndex": 0}}\n\n'
                        yield f'event: message_stop\ndata: {{"stopReason": "error"}}\n\n'
                        yield conv_error_event.to_sse_format()
                        yield "event: done\ndata: {}\n\n"

                    # Persist error messages to session.
                    #
                    # SKIP for max_tokens: Strands already appended the recovered
                    # partial assistant turn to agent.messages and the
                    # MessageAddedEvent hook persisted it to AgentCore Memory
                    # before the exception propagated; the user turn was
                    # persisted at turn start by the normal hook. Re-persisting
                    # here would duplicate the user turn and add a SECOND
                    # consecutive assistant message, breaking Bedrock role
                    # alternation for the follow-up "Continue" turn. The error
                    # explanation stays a live-only UI affordance for this turn.
                    if error_code == ErrorCode.MAX_TOKENS:
                        logger.info(
                            f"max_tokens: skipping error re-persist for session {session_id} "
                            f"(Strands already committed the recovered partial turn)"
                        )
                    else:
                        # Persist ONLY the assistant turn. The user turn was
                        # already persisted at turn start by Strands'
                        # MessageAddedEvent hook (any error event reaching
                        # this in-loop handler was emitted from inside
                        # ``process_agent_stream``, after the agent stream
                        # began iterating). Re-persisting the user turn
                        # would either duplicate it or cause AgentCore
                        # Memory to reject the conflicting write and drop
                        # the assistant message along with it.
                        #
                        # Persist what the user saw live: PR #388's
                        # double-wrap fix above means ``conv_error_event.message``
                        # is the un-wrapped friendly text for classified
                        # AGENT_ERROR cases (leading "⚠️") and the wrapped
                        # template for everything else — same string the
                        # content_block_delta below yields to the SSE
                        # stream. Persisting it keeps live and
                        # refresh-hydrated views in sync.
                        try:
                            from agents.main_agent.session.persistence import persist_synthetic_messages
                            from agents.main_agent.session.session_factory import SessionFactory

                            persist_session_manager = SessionFactory.create_session_manager(session_id=session_id, user_id=user_id, caching_enabled=False)
                            persist_synthetic_messages(
                                persist_session_manager,
                                session_id,
                                [("assistant", conv_error_event.message)],
                            )
                        except Exception as persist_error:
                            logger.error(f"Failed to persist intercepted error to session: {persist_error}", exc_info=True)

                    # Skip the original error event and exit the loop - we've handled the error
                    return

                # Format as SSE event and yield (including done event after metadata)
                sse_event = self._format_sse_event(event)
                yield sse_event

                # MCP Apps PR #5: interleave any app-initiated tool events
                # (a `tools/call` proxied from an embedded App, dispatched
                # out-of-band on /mcp-apps/proxy-call) into the live thread.
                # Non-blocking drain — never waits on the agent stream.
                if app_event_queue is not None:
                    from apis.shared.mcp_apps.broker import (
                        get_app_tool_event_broker,
                    )

                    for app_ev in get_app_tool_event_broker().drain(
                        app_event_queue
                    ):
                        yield self._format_sse_event(app_ev)

                # MCP Apps (PR #3): if this tool_result belongs to a
                # UI-bearing tool, fetch its `ui://` resource via
                # `resources/read` and emit a `ui_resource` SSE right after
                # the tool_result it correlates to (toolUseId ties them).
                # Inert + zero-cost unless the host flag is on; best-effort
                # so a fetch failure never breaks the live stream.
                if event.get("type") == "tool_result":
                    for sse in await self._extract_ui_resource_events(
                        event,
                        ui_tool_use_names,
                        ui_resource_emitted,
                        session_id=session_id,
                        user_id=user_id,
                    ):
                        yield sse

                # MCP Apps (streaming tool input): mount a UI tool's frame at
                # its `content_block_start` — BEFORE the model streams the
                # tool's arguments — so the App's bridge is live for the
                # progressive `ui_tool_input_partial` stream below. Deduped vs
                # the `tool_result` path above by `ui_resource_emitted`.
                elif event.get("type") == "content_block_start":
                    bd = event.get("data", {})
                    if bd.get("type") == "tool_use":
                        tu = bd.get("toolUse", {})
                        tuid = tu.get("toolUseId") or tu.get("tool_use_id")
                        tname = ui_tool_use_names.get(tuid) if tuid else None
                        # Header-only shell FIRST (instant, no resources/read)
                        # so the App frame's header + shimmer replace the tool
                        # rail with no flash; the full html-bearing resource
                        # follows below and mounts the iframe.
                        for sse in self._emit_ui_app_header_for_tool(
                            tname, tuid, ui_header_emitted
                        ):
                            yield sse
                        for sse in await self._emit_ui_resource_for_tool(
                            tname,
                            tuid,
                            ui_resource_emitted,
                            session_id=session_id,
                            user_id=user_id,
                        ):
                            yield sse

                # Accumulate streamed `toolUse.input` fragments and emit a
                # healed `ui_tool_input_partial` per delta — only for tools
                # whose frame we actually mounted (a cheap dict-miss otherwise).
                elif event.get("type") == "content_block_delta":
                    bd = event.get("data", {})
                    frag = bd.get("input")
                    if bd.get("type") == "tool_use" and isinstance(frag, str):
                        tuid = ui_block_index_to_tool_use_id.get(
                            bd.get("contentBlockIndex")
                        )
                        if tuid and tuid in ui_resource_emitted:
                            ui_partial_input_acc[tuid] = (
                                ui_partial_input_acc.get(tuid, "") + frag
                            )
                            for sse in self._emit_tool_input_partial(
                                tuid, ui_partial_input_acc[tuid]
                            ):
                                yield sse

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

        except Exception as e:
            # Handle errors with emergency flush
            logger.error(f"Error in stream_response: {e}")
            import traceback

            logger.error(f"Traceback: {traceback.format_exc()}")

            # Emergency flush: save buffered messages before losing them
            self._emergency_flush(session_manager)

            # This handler catches exceptions from stream_coordinator's own
            # loop body (e.g. interrupt extraction, artifact lookup,
            # metadata calculation). Exceptions from inside the agent
            # stream are caught one level down by process_agent_stream's
            # own `except Exception` and yielded as STREAM_ERROR events
            # — the in-loop branch above handles those. See
            # test_force_stop_persistence.py:217-235 for the trace path.
            # Coordinator-internal failures don't carry Bedrock-y patterns
            # the force_stop classifier could match, so use the generic
            # STREAM_ERROR template directly.
            error_event = build_conversational_error_event(code=ErrorCode.STREAM_ERROR, error=e, session_id=session_id, recoverable=True)

            # Emit message events so error appears in chat
            yield f'event: message_start\ndata: {{"role": "assistant"}}\n\n'
            yield f'event: content_block_start\ndata: {{"contentBlockIndex": 0, "type": "text"}}\n\n'
            yield f"event: content_block_delta\ndata: {json.dumps({'contentBlockIndex': 0, 'type': 'text', 'text': error_event.message})}\n\n"
            yield f'event: content_block_stop\ndata: {{"contentBlockIndex": 0}}\n\n'
            yield f'event: message_stop\ndata: {{"stopReason": "error"}}\n\n'
            yield error_event.to_sse_format()
            yield "event: done\ndata: {}\n\n"

            # Persist ONLY the assistant turn. Same reasoning as the
            # AGENT_ERROR path above — the user turn was already persisted
            # at turn start by Strands' MessageAddedEvent hook.
            try:
                from agents.main_agent.session.persistence import persist_synthetic_messages
                from agents.main_agent.session.session_factory import SessionFactory

                persist_session_manager = SessionFactory.create_session_manager(session_id=session_id, user_id=user_id, caching_enabled=False)
                persist_synthetic_messages(
                    persist_session_manager,
                    session_id,
                    [("assistant", error_event.message)],
                )
            except Exception as persist_error:
                logger.error(f"Failed to persist stream error to session: {persist_error}")
        finally:
            # MCP Apps PR #5: always release the broker subscription —
            # covers normal completion, the in-loop error `return`, and
            # the except path, so a dropped stream never leaks a queue.
            if app_event_queue is not None:
                try:
                    from apis.shared.mcp_apps.broker import (
                        get_app_tool_event_broker,
                    )

                    get_app_tool_event_broker().remove_subscriber(
                        session_id, app_event_queue
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "MCP Apps broker unsubscribe failed", exc_info=True
                    )

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
            inference_params = snapshot_source.get("inference_params") or {}
            snapshot = PausedTurnSnapshot(
                enabled_tools=snapshot_source.get("enabled_tools"),
                model_id=snapshot_source.get("model_id"),
                provider=snapshot_source.get("provider"),
                temperature=inference_params.get("temperature"),
                system_prompt=snapshot_source.get("system_prompt"),
                caching_enabled=snapshot_source.get("caching_enabled"),
                max_tokens=inference_params.get("max_tokens"),
                agent_type=snapshot_source.get("agent_type"),
                enabled_skills=snapshot_source.get("enabled_skills"),
                inference_params=dict(inference_params) if inference_params else None,
                mantle_endpoint_path=snapshot_source.get("mantle_endpoint_path"),
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

    async def _extract_artifact_events(
        self,
        session_id: Optional[str],
        user_id: Optional[str],
        turn_start: datetime,
        produced_by_message_index: Optional[int] = None,
    ) -> List[str]:
        """Yield one SSE-formatted `artifact` event per artifact whose
        HEAD was created or updated during this turn.

        Identifies "this turn" by `updated_at >= turn_start` rather than
        parsing the tool result text: it reflects exactly what was
        persisted, handles multiple artifacts in one turn, and ignores
        artifacts carried over from earlier turns in the same session. A
        row with an unparseable `updated_at` is included (the artifact
        tool ran this turn and the SPA dedupes by id+version anyway).

        Best-effort: any failure (artifacts not configured for this env,
        DynamoDB error) logs and returns [] — never breaks the stream.
        """
        if not (session_id and user_id):
            return []
        try:
            from agents.builtin_tools.artifacts.service import (
                ArtifactConfigError,
                list_session_artifacts,
                set_produced_by_message_index,
            )

            rows = await asyncio.to_thread(
                list_session_artifacts, user_id, session_id
            )
        except ArtifactConfigError:
            return []
        except Exception as e:
            logger.warning("Failed to list session artifacts: %s", e)
            return []

        events: List[str] = []
        for row in rows:
            updated_at = row.get("updated_at") or ""
            try:
                touched = datetime.fromisoformat(updated_at) >= turn_start
            except (ValueError, TypeError):
                touched = True
            if not touched:
                continue
            artifact_id = row.get("artifact_id", "")
            version = int(row.get("version", 0))
            if produced_by_message_index is not None and artifact_id:
                try:
                    await asyncio.to_thread(
                        set_produced_by_message_index,
                        user_id,
                        artifact_id,
                        version,
                        produced_by_message_index,
                    )
                except Exception as e:  # noqa: BLE001 - best-effort linkage
                    logger.warning(
                        "Failed to stamp produced_by_message_index "
                        "(artifact=%s): %s",
                        artifact_id,
                        e,
                    )
            payload = {
                "type": "artifact",
                "artifactId": artifact_id,
                "version": version,
                "title": row.get("title", ""),
                "contentType": row.get(
                    "content_type", "text/html; charset=utf-8"
                ),
                "sessionId": session_id,
                "updatedAt": updated_at,
                "action": "created" if version == 1 else "updated",
                "producedByMessageIndex": produced_by_message_index,
            }
            events.append(
                f"event: artifact\ndata: {json.dumps(payload)}\n\n"
            )
        return events

    def _emit_ui_app_header_for_tool(
        self,
        tool_name: Optional[str],
        tool_use_id: Optional[str],
        emitted: set,
    ) -> List[str]:
        """Emit a UI tool's instant header-only `ui_resource` shell (empty html).

        Runs at `content_block_start`, BEFORE the (potentially slow)
        `resources/read` in `_emit_ui_resource_for_tool`, so the App frame's
        header (icon + server + tool + shimmer) replaces the plain tool rail
        with no flash. Synchronous + cheap: it reads only the in-process
        catalog + captured `serverInfo` (no network). Deduped per toolUseId via
        its own `emitted` set so it never blocks the full html-bearing emit.
        Best-effort: any failure logs and returns [].
        """
        from agents.main_agent.integrations.mcp_apps import (
            build_ui_app_header,
            is_mcp_apps_host_enabled,
        )

        if not is_mcp_apps_host_enabled():
            return []
        if not tool_use_id or not tool_name or tool_use_id in emitted:
            return []
        try:
            payload = build_ui_app_header(tool_name, tool_use_id)
            if payload is None:
                return []
            emitted.add(tool_use_id)
            return [f"event: ui_resource\ndata: {json.dumps(payload)}\n\n"]
        except Exception as e:  # noqa: BLE001 - best-effort side channel
            logger.warning("Failed to emit ui_resource header: %s", e)
            return []

    async def _emit_ui_resource_for_tool(
        self,
        tool_name: Optional[str],
        tool_use_id: Optional[str],
        emitted: set,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[str]:
        """Fetch, emit, and persist a tool's MCP App `ui_resource` (deduped).

        PR #3 of the MCP Apps host-renderer initiative
        (`docs/kaizen/scoping/mcp-apps-host-renderer.md`), generalised so the
        frame can mount EARLY. When the host flag is on and the tool declared
        a `ui://` resource in its `tools/list` `_meta.ui` (recorded in the
        catalog by PR #2), fetch that resource via the spec-mandated
        `resources/read` against the same MCP client that surfaced the tool,
        and emit a single

            `{type, toolUseId, resourceUri, html, mimeType, csp, permissions}`

        event with the HTML inlined (so the frontend needs no MCP client). The
        blocking `resources/read` runs in a worker thread so the live stream
        is not stalled.

        Two call sites share this via the `emitted` dedupe set: the early
        mount at a UI tool's `content_block_start` (so the App's bridge is
        live *before* arguments stream — the window the `ui_tool_input_partial`
        stream needs) and the legacy post-`tool_result` fallback (covers a
        tool whose name wasn't captured at block start). The resource shell is
        static per `resourceUri` — independent of the tool's args/result — so
        fetching it at block start yields the same payload as at result time.

        Inert and zero-cost when `AGENTCORE_MCP_APPS_HOST_ENABLED` is false.
        Best-effort: any failure logs and returns [] — never breaks the stream.
        """
        from agents.main_agent.integrations.mcp_apps import (
            fetch_ui_resource,
            is_mcp_apps_host_enabled,
        )

        if not is_mcp_apps_host_enabled():
            return []
        if not tool_use_id or tool_use_id in emitted or not tool_name:
            return []

        try:
            payload = await asyncio.to_thread(
                fetch_ui_resource, tool_name, tool_use_id
            )
            if payload is None:
                return []

            emitted.add(tool_use_id)

            # Persist for reload survival (best-effort). The `ui_resource`
            # event is inline and never re-streams, so without this the
            # `mcp-app-frame` falls back to a plain tool card after a refresh.
            # Mirrors how artifacts persist + stamp from this same coordinator;
            # the read side is the app-api messages endpoint's `uiResources`
            # sidecar. Inert when the sessions-metadata table is absent (dev).
            if session_id and user_id:
                try:
                    from apis.shared.mcp_apps.ui_resource_store import (
                        get_ui_resource_store,
                    )

                    await asyncio.to_thread(
                        get_ui_resource_store().store,
                        user_id=user_id,
                        session_id=session_id,
                        tool_use_id=tool_use_id,
                        resource_uri=payload.get("resourceUri", ""),
                        html=payload.get("html", ""),
                        mime_type=payload.get("mimeType", ""),
                        csp=payload.get("csp", {}),
                        permissions=payload.get("permissions", {}),
                        sandbox_origin=payload.get("sandboxOrigin", ""),
                        server_name=payload.get("serverName", ""),
                        icon=payload.get("icon", ""),
                        tool_name=payload.get("toolName", ""),
                    )
                except Exception:  # noqa: BLE001 - persistence is best-effort
                    logger.warning(
                        "Failed to persist ui_resource for reload "
                        "(toolUseId=%s)",
                        tool_use_id,
                        exc_info=True,
                    )

            return [f"event: ui_resource\ndata: {json.dumps(payload)}\n\n"]
        except Exception as e:  # noqa: BLE001 - best-effort side channel
            logger.warning("Failed to emit ui_resource event: %s", e)
            return []

    async def _extract_ui_resource_events(
        self,
        event: Dict[str, Any],
        tool_use_names: Dict[str, str],
        emitted: set,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[str]:
        """Post-`tool_result` fallback emit of a tool's `ui_resource`.

        Pulls the toolUseId from the tool_result and delegates to
        `_emit_ui_resource_for_tool` (which dedupes against the early mount).
        """
        try:
            tool_result = event.get("data", {}).get("tool_result", {})
            if not isinstance(tool_result, dict):
                return []
            tool_use_id = tool_result.get("toolUseId") or tool_result.get(
                "tool_use_id"
            )
            tool_name = tool_use_names.get(tool_use_id) if tool_use_id else None
            return await self._emit_ui_resource_for_tool(
                tool_name,
                tool_use_id,
                emitted,
                session_id=session_id,
                user_id=user_id,
            )
        except Exception as e:  # noqa: BLE001 - best-effort side channel
            logger.warning("Failed to emit ui_resource event: %s", e)
            return []

    def _emit_tool_input_partial(
        self, tool_use_id: str, accumulated: str
    ) -> List[str]:
        """Emit a `ui_tool_input_partial` SSE from accumulated input fragments.

        Heals the streamed prefix of `toolUse.input` into the largest valid
        object it can (`heal_partial_json`) and ships it as the SEP-1865
        `tool-input-partial` payload, so an App that renders progressively
        (e.g. Excalidraw's guided camera tour) animates as the model generates
        the arguments. Skipped silently until the prefix heals to an object.
        Best-effort — never raises into the stream.
        """
        from apis.shared.mcp_apps.partial_json import heal_partial_json

        try:
            args = heal_partial_json(accumulated)
            if not args:
                return []
            payload = {
                "type": "ui_tool_input_partial",
                "toolUseId": tool_use_id,
                "arguments": args,
            }
            return [
                "event: ui_tool_input_partial\n"
                f"data: {json.dumps(payload)}\n\n"
            ]
        except Exception as e:  # noqa: BLE001 - best-effort side channel
            logger.warning("Failed to emit ui_tool_input_partial event: %s", e)
            return []

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
            from apis.shared.sessions.models import Attribution, LatencyMetrics, MessageMetadata, ModelInfo, TokenUsage
            from apis.shared.sessions.metadata import store_message_metadata

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

            # Get time to first token. We persist `None` (not 0) when the
            # provider didn't emit `timeToFirstByteMs` and we couldn't
            # measure it locally — a real TTFT can never be 0ms, and any
            # downstream aggregation (averages, percentiles) needs to
            # distinguish "not measured" from a real value to avoid
            # pulling stats toward zero.
            if accumulated_metadata.get("metrics", {}).get("timeToFirstByteMs"):
                time_to_first_token_ms = int(accumulated_metadata["metrics"]["timeToFirstByteMs"])
                logger.info(f"📊 Using provider timeToFirstByteMs: {time_to_first_token_ms}ms")
            else:
                logger.info("📊 No TTFT available - provider did not send timeToFirstByteMs for this message")

            # Create latency metrics if we have at least E2E latency.
            # `time_to_first_token_ms` may be None — LatencyMetrics.time_to_first_token
            # is Optional, so this serializes as JSON null.
            if end_to_end_latency_ms is not None:
                latency_metrics = LatencyMetrics(
                    time_to_first_token=time_to_first_token_ms,
                    end_to_end_latency=end_to_end_latency_ms,
                )
                logger.info(f"📊 Created LatencyMetrics: TTFT={time_to_first_token_ms}ms, E2E={end_to_end_latency_ms}ms")
            else:
                # Log if we couldn't determine any latency
                logger.warning("Could not determine latency metrics - no latencyMs from provider and no timing data available")

            # Extract ModelInfo from agent and create pricing snapshot for cost tracking
            model_info = None
            pricing_snapshot = None
            cost = None
            context_window: Optional[int] = None

            if agent and hasattr(agent, "model_config"):
                model_id = agent.model_config.model_id

                # Get pricing snapshot from managed models database
                pricing_snapshot = await self._get_pricing_snapshot(model_id)

                # Look up the model's max_input_tokens once so the bump of
                # session-level aggregates (for the chat cost badge) has a
                # context window value to persist alongside the latest
                # turn's input token count.
                try:
                    from apis.shared.costs.pricing_config import get_model_by_model_id
                    model_record = await get_model_by_model_id(model_id)
                    if model_record is not None:
                        max_input_tokens = getattr(model_record, "max_input_tokens", None)
                        if max_input_tokens:
                            context_window = int(max_input_tokens)
                except Exception as ctx_err:
                    logger.debug(f"Skipping contextWindow capture for storage: {ctx_err}")

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
                # contextWindow is passed as an extra field (MessageMetadata
                # has extra="allow"); _bump_session_aggregates picks it up
                # via model_extra to denormalize onto the session row.
                metadata_kwargs: Dict[str, Any] = dict(
                    latency=latency_metrics,
                    token_usage=token_usage,
                    model_info=model_info,
                    attribution=attribution,
                    cost=cost,
                    citations=citations,
                )
                if context_window is not None:
                    metadata_kwargs["contextWindow"] = context_window
                message_metadata = MessageMetadata(**metadata_kwargs)

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
            from apis.shared.costs.pricing_config import create_pricing_snapshot
            from apis.shared.sessions.models import PricingSnapshot

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
            from apis.shared.costs.calculator import CostCalculator

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
            enabled_tools = None
            system_prompt_hash = None
            if agent and hasattr(agent, "model_config"):
                last_model = agent.model_config.model_id
                enabled_tools = getattr(agent, "enabled_tools", None)
                if hasattr(agent, "system_prompt") and agent.system_prompt:
                    system_prompt_hash = hashlib.md5(agent.system_prompt.encode()).hexdigest()[:16]
            else:
                logger.warning("⚠️ Agent is None or missing model_config — skipping preference update")

            await update_session_activity(
                session_id=session_id,
                user_id=user_id,
                last_model=last_model,
                enabled_tools=enabled_tools,
                system_prompt_hash=system_prompt_hash,
            )
        except Exception as e:
            logger.error(f"Failed to update session metadata: {e}", exc_info=True)
            # Don't raise — metadata failures shouldn't break streaming.
