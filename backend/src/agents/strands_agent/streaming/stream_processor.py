"""Stream processor for transforming agent stream events into standardized format.

This module provides functionality to transform raw agent stream events from
Strands Agents into a standardized event format for API consumption.

ARCHITECTURE OVERVIEW:
=====================
The stream processor follows a modular design where:
1. Raw events come from Strands Agents (via agent.stream_async())
2. Each event is processed by specialized handler functions
3. Handlers transform events into a standardized format: {"type": str, "data": dict}
4. Processed events are yielded back to the caller

The main function (process_agent_stream) orchestrates the flow:
- Iterates through raw events from the agent stream
- Calls specialized handlers for different event categories
- Handles errors gracefully
- Ensures all data is JSON-serializable before yielding

ENHANCEMENTS (v2):
==================
DUAL-LAYER ARCHITECTURE:
- Layer 1 (Structural): Content block events provide message reconstruction foundation
  * message_start/stop, content_block_start/delta/stop
  * contentBlockIndex for tracking multiple blocks per message
  * Basic tool use info (toolUseId, name, input)

- Layer 2 (Rich UX): Tool events provide rich user experience enhancements
  * display_content: Formatted code blocks, JSON displays, rich output
  * message: Progress indicators ("Searching...", "Running...")
  * integration metadata: Icons and branding for external tools
  * Execution results (tool_result) and errors (tool_error)

ADDITIONAL ENHANCEMENTS:
- Rich inline citation support with citation_start/citation_end events
- Lifecycle events available for debugging (commented out in production)
- Cache token metrics for prompt caching cost tracking
"""

import logging
import time
from datetime import datetime, date
from decimal import Decimal
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, TypedDict
from uuid import UUID

from apis.shared.errors import StreamErrorEvent, ErrorCode

logger = logging.getLogger(__name__)

# Type aliases for better readability
# RawEvent: The raw dictionary structure that comes from Strands Agents
RawEvent = Dict[str, Any]


class ProcessedEventDict(TypedDict):
    """TypedDict for processed events to improve type safety."""
    type: str
    data: Dict[str, Any]


# ProcessedEvent: The standardized format we output: {"type": str, "data": dict}
ProcessedEvent = ProcessedEventDict


def _serialize_object(obj: Any) -> Any:
    """Serialize an object to a JSON-serializable format.

    WHY THIS EXISTS:
    Some objects from Strands (like AgentResult) are not JSON-serializable by default.
    This function recursively converts complex objects into dictionaries/strings
    so they can be safely sent over HTTP/SSE streams.

    HANDLING STRATEGY (in order of preference):
    1. Primitives (str, int, float, bool) - return as-is
    2. Common types (datetime, UUID, Decimal) - convert to string/ISO format
    3. Dictionaries - recursively serialize all values
    4. Lists/tuples - recursively serialize all items
    5. Objects with __dict__ - convert to dict and serialize recursively
    6. Objects with to_dict() method - use that method
    7. Fallback - convert to string representation

    Args:
        obj: Object to serialize (can be any type)

    Returns:
        JSON-serializable representation of the object
    """
    # Handle None - None is JSON-serializable
    if obj is None:
        return None

    # Handle primitive types - these are already JSON-serializable
    if isinstance(obj, (str, int, float, bool)):
        return obj

    # Handle datetime objects - convert to ISO format string
    if isinstance(obj, datetime):
        return obj.isoformat()

    # Handle date objects - convert to ISO format string
    if isinstance(obj, date):
        return obj.isoformat()

    # Handle UUID objects - convert to string
    if isinstance(obj, UUID):
        return str(obj)

    # Handle Decimal objects - convert to string to preserve precision
    # JSON doesn't have a native Decimal type, so we use string representation
    if isinstance(obj, Decimal):
        return str(obj)

    # Handle bytes objects - convert to base64 or string representation
    if isinstance(obj, bytes):
        # For binary data, we could use base64, but for simplicity we'll use repr
        # In production, you might want to use base64.b64encode(obj).decode('utf-8')
        return obj.decode('utf-8', errors='replace')

    # Handle dictionaries - recursively serialize all values
    # This ensures nested objects are also serialized
    if isinstance(obj, dict):
        return {key: _serialize_object(value) for key, value in obj.items()}

    # Handle lists/tuples - recursively serialize all items
    # This handles arrays of complex objects
    if isinstance(obj, (list, tuple)):
        return [_serialize_object(item) for item in obj]

    # Handle dataclasses and objects with __dict__
    # Most Python objects have __dict__ which contains their attributes
    # We convert these to dictionaries and serialize recursively
    if hasattr(obj, "__dict__"):
        return {key: _serialize_object(value) for key, value in obj.__dict__.items()}

    # Handle objects with to_dict method
    # Some objects provide a to_dict() method for serialization
    # We use that method and then serialize the result recursively
    if hasattr(obj, "to_dict"):
        return _serialize_object(obj.to_dict())

    # Fallback: convert to string representation
    # If we can't serialize it any other way, convert to string
    # This ensures we never fail completely, though data may be lost
    try:
        return str(obj)
    except Exception as e:
        logger.warning(
            f"Could not serialize object of type {type(obj)}: {e}, converting to string"
        )
        return str(obj)


def _create_event(event_type: str, data: Dict[str, Any]) -> ProcessedEvent:
    """Create a standardized event dictionary.

    WHY THIS EXISTS:
    All events need to follow the same structure: {"type": str, "data": dict}
    This function ensures consistency and handles serialization automatically.

    Args:
        event_type: The type of event (e.g., "delta", "tool_use", "complete")
        data: Event-specific data dictionary (will be serialized if needed)

    Returns:
        Standardized event dictionary with "type" and "data" keys
        Example: {"type": "delta", "data": {"content": "Hello"}}
    """
    # Serialize data to ensure JSON compatibility
    # This handles cases where data contains non-serializable objects
    serialized_data = _serialize_object(data)
    return {"type": event_type, "data": serialized_data}


def _handle_lifecycle_events(event: RawEvent) -> List[ProcessedEvent]:
    """Process lifecycle events from the agent stream.

    LIFECYCLE EVENTS:
    These events track the state and flow of the agent execution:
    - init_event_loop: Agent is initializing
    - start_event_loop: A new event loop cycle is starting
    - message: A complete message was created
    - event: Raw event from the underlying model stream
    - result: Final result object (contains the complete agent response)

    NOTE: Multiple lifecycle events can be present in a single raw event,
    so we return a list of processed events.

    Args:
        event: Raw event from Strands Agents

    Returns:
        List of processed events (may be empty if no lifecycle events found)
    """
    events = []

    # init_event_loop: True at the start of agent invocation initializing
    # This is the first event when an agent starts processing
    if event.get("init_event_loop", False):
        events.append(_create_event("init_event_loop", {}))

    # start_event_loop: True when the event loop is starting
    # This happens at the beginning of each event loop cycle
    # (an agent may go through multiple cycles if it uses tools)
    if event.get("start_event_loop", False):
        events.append(_create_event("start_event_loop", {}))

    # message: Present when a new message is created
    # IMPORTANT: Message events can occur alongside other events (MessageAddedEvent).
    # We check if it's a valid message structure (has "role" key) to distinguish
    # from cases where "message" appears as part of other event structures.
    if "message" in event:
        message = event["message"]
        # Check if it's a valid message structure (dict with "role" key)
        # This indicates a MessageAddedEvent rather than a nested message reference
        if isinstance(message, dict) and "role" in message:
            # Only exclude if it's clearly part of another event structure
            # (e.g., when it's nested in a result object, not a standalone message event)
            if not any(key in event for key in ["result"]):
                events.append(_create_event("message", {"message": message}))
                
                # Extract toolResult from message content blocks and create tool_result events
                # toolResult is located in message.content blocks
                if "content" in message and isinstance(message["content"], list):
                    for content_item in message["content"]:
                        # Check for toolResult (both camelCase and snake_case)
                        tool_result = None
                        if isinstance(content_item, dict):
                            tool_result = content_item.get("toolResult") or content_item.get("tool_result")
                        
                        if tool_result:
                            # Create a tool_result event with the full wrapped message structure
                            # The message sent to the client should be the full wrapped tool_result
                            # with {"role": "user", etc...}
                            wrapped_message = {
                                "role": message.get("role"),
                                "content": [content_item]  # Single content block with toolResult
                            }
                            # Include other message fields if present
                            if "id" in message:
                                wrapped_message["id"] = message["id"]
                            if "model" in message:
                                wrapped_message["model"] = message["model"]
                            
                            events.append(_create_event("tool_result", {
                                "message": wrapped_message,
                                "tool_result": tool_result
                            }))

    # event: Raw event from the model stream
    # This is a pass-through for low-level model events
    if "event" in event:
        events.append(_create_event("event", {"event": event["event"]}))

    # result: The final AgentResult (needs serialization)
    # AgentResult is a complex object that needs to be converted to a dict
    # This contains the final response, metrics, and other completion data
    if "result" in event:
        result_data = _serialize_object(event["result"])
        events.append(_create_event("result", {"result": result_data}))

    return events


def _handle_completion_events(event: RawEvent) -> Tuple[List[ProcessedEvent], bool]:
    """Process completion and error events.

    WHY THIS IS SEPARATE:
    Completion events are special because they signal the end of processing.
    We need to check these FIRST and potentially break out of the loop early.
    That's why this function returns a boolean flag (should_break).

    COMPLETION EVENTS:
    - complete: Normal completion - agent finished successfully
    - force_stop: Error condition - agent was forced to stop

    Args:
        event: Raw event from Strands Agents

    Returns:
        Tuple of (processed events, should_break)
        - processed events: List of completion/error events
        - should_break: True if we should stop processing (complete or error occurred)
    """
    events = []
    should_break = False

    # complete: True when the event loop cycle completes
    # This is the normal completion signal - agent finished successfully
    # We break because there's nothing more to process
    if event.get("complete", False):
        events.append(_create_event("complete", {}))
        should_break = True

    # force_stop: True if the event loop was forced to stop
    # This indicates an error or forced termination
    # We convert this to an "error" event type for consistency
    # We also break because processing should stop on error
    if event.get("force_stop", False):
        reason = event.get("force_stop_reason", "unknown reason")

        # Create structured error event
        error_event = StreamErrorEvent(
            error=f"Agent force-stopped: {reason}",
            code=ErrorCode.AGENT_ERROR,
            detail=str(reason) if reason != "unknown reason" else None,
            recoverable=False
        )
        # Convert to event dict format
        events.append(_create_event("error", error_event.model_dump(exclude_none=True)))
        should_break = True

    return events, should_break


def _handle_content_block_events(event: RawEvent, current_block_index: Dict[str, int]) -> List[ProcessedEvent]:
    """Process content block events from the agent stream.

    CONTENT BLOCK EVENTS - STRUCTURAL FOUNDATION:
    These events provide the structural foundation for reconstructing messages.
    They track the lifecycle and index of each content block (text or tool use).
    This is the core Bedrock Converse API event structure.

    WHY CONTENT BLOCKS ARE THE FOUNDATION:
    - Provides contentBlockIndex for proper message reconstruction
    - Tracks message boundaries (messageStart/Stop)
    - Distinguishes between multiple content blocks in a single message
    - Handles interleaved text and tool use blocks correctly
    - Matches Bedrock Converse API structure directly

    RELATIONSHIP WITH TOOL EVENTS:
    - Content blocks: Structural info (what, where, when in the message)
    - Tool events: Rich UX layer (display_content, progress, execution results)
    - Both are needed for complete rich tool UX experience

    EVENT TYPES (from event.event structure):
    - messageStart: Marks the beginning of a message (contains role)
    - contentBlockStart: Marks the start of a content block (text or tool use)
      * Includes toolUseId, name, type for tool use blocks
      * Includes contentBlockIndex for tracking

    - contentBlockDelta: Incremental updates to a content block
      * Text chunks (for text blocks)
      * Tool input JSON (for tool use blocks)
      * Always includes contentBlockIndex

    - contentBlockStop: Marks the end of a content block
      * Signals completion of a specific block by index

    - messageStop: Marks the end of a message
      * Includes stopReason (end_turn, tool_use, max_tokens, etc.)

    PROVIDER COMPATIBILITY:
    - Some providers (like OpenAI) don't send contentBlockIndex
    - We track it ourselves using current_block_index dict to ensure correct indexing
    - This fixes issues with multiple tool uses being merged into one block

    Args:
        event: Raw event from Strands Agents
        current_block_index: Dict to track current block index across calls

    Returns:
        List of processed events (may be empty if no content block events found)
    """
    events = []

    # Check if this event has nested event structure
    if "event" not in event or not isinstance(event["event"], dict):
        return events

    inner_event = event["event"]

    # messageStart: Beginning of a message
    # Contains role (typically "assistant")
    # Reset block index and skipped blocks when new message starts
    if "messageStart" in inner_event:
        message_start = inner_event["messageStart"]
        current_block_index["index"] = 0  # Reset to 0 for new message
        current_block_index["skipped_blocks"] = set()  # Clear skipped blocks for new message
        events.append(_create_event("message_start", {
            "role": message_start.get("role", "assistant")
        }))

    # contentBlockStart: Beginning of a content block
    # Contains start object (toolUse or text) and contentBlockIndex
    if "contentBlockStart" in inner_event:
        block_start = inner_event["contentBlockStart"]

        # Get contentBlockIndex from provider, or use our tracked index
        # Some providers (like OpenAI) don't send contentBlockIndex
        if "contentBlockIndex" in block_start:
            block_index = block_start["contentBlockIndex"]
        else:
            # Provider didn't send index, use our tracked index
            block_index = current_block_index.get("index", 0)

        block_data = {
            "contentBlockIndex": block_index
        }

        # Extract start object (toolUse or text)
        if "start" in block_start:
            start_obj = block_start["start"]

            # Handle tool use start
            if "toolUse" in start_obj:
                tool_use = start_obj["toolUse"]
                block_data["type"] = "tool_use"
                block_data["toolUse"] = {
                    "toolUseId": tool_use.get("toolUseId"),
                    "name": tool_use.get("name"),
                    "type": tool_use.get("type", "tool_use")
                }

            # Handle text start (if model emits it)
            elif "text" in start_obj:
                block_data["type"] = "text"

        events.append(_create_event("content_block_start", block_data))

    # contentBlockDelta: Incremental update to a content block
    # Contains delta object (text or toolUse input) and contentBlockIndex
    if "contentBlockDelta" in inner_event:
        block_delta = inner_event["contentBlockDelta"]

        # Get contentBlockIndex from provider, or use our tracked index
        # Some providers (like OpenAI) don't send contentBlockIndex
        if "contentBlockIndex" in block_delta:
            block_index = block_delta["contentBlockIndex"]
        else:
            # Provider didn't send index, use our tracked index
            block_index = current_block_index.get("index", 0)

        delta_data = {
            "contentBlockIndex": block_index
        }

        # Extract delta object
        if "delta" in block_delta:
            delta_obj = block_delta["delta"]

            # Handle text delta
            if "text" in delta_obj:
                delta_data["type"] = "text"
                delta_data["text"] = delta_obj["text"]
                events.append(_create_event("content_block_delta", delta_data))

            # Handle tool use input delta
            elif "toolUse" in delta_obj:
                tool_use = delta_obj["toolUse"]
                delta_data["type"] = "tool_use"
                if "input" in tool_use:
                    delta_data["input"] = tool_use["input"]
                events.append(_create_event("content_block_delta", delta_data))

            # Skip reasoningContent - handled by _handle_reasoning_events
            # Don't emit content_block_delta for reasoning to avoid incomplete events
            # Track skipped blocks so we also skip their content_block_stop
            elif "reasoningContent" in delta_obj:
                # Initialize skipped_blocks set if not present
                if "skipped_blocks" not in current_block_index:
                    current_block_index["skipped_blocks"] = set()
                current_block_index["skipped_blocks"].add(block_index)

    # contentBlockStop: End of a content block
    # Contains contentBlockIndex
    if "contentBlockStop" in inner_event:
        block_stop = inner_event["contentBlockStop"]

        # Get contentBlockIndex from provider, or use our tracked index
        # Some providers (like OpenAI) don't send contentBlockIndex
        if "contentBlockIndex" in block_stop:
            block_index = block_stop["contentBlockIndex"]
        else:
            # Provider didn't send index, use our tracked index
            block_index = current_block_index.get("index", 0)

        # Skip content_block_stop for reasoning blocks (which we skipped in delta)
        skipped_blocks = current_block_index.get("skipped_blocks", set())
        if block_index in skipped_blocks:
            # Don't emit stop event for skipped reasoning blocks
            pass
        else:
            events.append(_create_event("content_block_stop", {
                "contentBlockIndex": block_index
            }))

        # Increment block index for next block (when provider doesn't send it)
        # Only increment if provider didn't send index (they manage their own)
        if "contentBlockIndex" not in block_stop:
            current_block_index["index"] = block_index + 1

    # messageStop: End of a message
    # Contains stopReason (e.g., "end_turn", "tool_use", "max_tokens")
    if "messageStop" in inner_event:
        message_stop = inner_event["messageStop"]
        events.append(_create_event("message_stop", {
            "stopReason": message_stop.get("stopReason", "end_turn")
        }))

    return events


def _extract_tool_use_data(tool_use_data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and normalize tool use information.

    WHY THIS EXISTS:
    Tool use data comes in different formats from Strands (camelCase vs snake_case).
    This function normalizes it to a consistent format and extracts only the fields
    we need for the API.

    NORMALIZATION:
    - toolUseId / tool_use_id / id -> tool_use_id (standardized)
    - name -> name (always present)
    - input -> input (complete JSON object from Strands)

    ENHANCEMENTS (v2):
    - display_content: Rich display format for UI (code blocks, JSON, etc.)
    - message: Progress message (e.g., "Searching the web", "Running command")
    - context: Additional context for the tool call

    Args:
        tool_use_data: Raw tool use data from event (from "current_tool_use" key)

    Returns:
        Normalized tool use event data with consistent field names
    """
    # Start with the tool name (required field)
    tool_use_event_data = {"name": tool_use_data.get("name")}

    # Add toolUseId if available (useful for tracking multiple tool calls)
    # Handle multiple possible key names for compatibility
    # Strands may use camelCase (toolUseId) or snake_case (tool_use_id) or just "id"
    tool_use_id = (
        tool_use_data.get("toolUseId") or
        tool_use_data.get("tool_use_id") or
        tool_use_data.get("id")
    )
    if tool_use_id:
        tool_use_event_data["tool_use_id"] = tool_use_id

    # Add input if available (complete JSON object from Strands)
    # Since Strands yields complete JSON objects (not partial), this will be complete
    if "input" in tool_use_data:
        tool_use_event_data["input"] = tool_use_data["input"]

    # ENHANCEMENT: Add display_content for richer UI presentation
    # display_content contains formatted representations of tool calls/results
    # Examples:
    # - Code blocks: {"type": "json_block", "json_block": "{\"language\": \"python\", ...}"}
    # - Text blocks: {"type": "text", "text": "Result: ..."}
    if "display_content" in tool_use_data:
        tool_use_event_data["display_content"] = tool_use_data["display_content"]

    # ENHANCEMENT: Add message for progress indicators
    # message provides human-readable status updates
    # Examples: "Searching the web", "Running command", "Creating file"
    if "message" in tool_use_data:
        tool_use_event_data["message"] = tool_use_data["message"]

    # ENHANCEMENT: Add context for additional metadata
    # context may contain additional information about the tool call
    if "context" in tool_use_data:
        tool_use_event_data["context"] = tool_use_data["context"]

    # ENHANCEMENT: Add integration metadata (for external tool integrations)
    # integration_name: Name of the integration (e.g., "google_drive", "slack")
    # integration_icon_url: Icon URL for the integration
    if "integration_name" in tool_use_data:
        tool_use_event_data["integration_name"] = tool_use_data["integration_name"]
    if "integration_icon_url" in tool_use_data:
        tool_use_event_data["integration_icon_url"] = tool_use_data["integration_icon_url"]

    return tool_use_event_data


def _handle_tool_events(event: RawEvent) -> List[ProcessedEvent]:
    """Process tool-related events from the agent stream.

    TOOL EVENTS - RICH UX LAYER:
    These events provide rich user experience enhancements on top of the basic content block
    structure. While content_block events track the structural tool use (what the model wants),
    tool events provide execution results and UX enhancements (how to display it richly).

    WHY BOTH CONTENT BLOCKS AND TOOL EVENTS?
    - Content blocks: Structural foundation (toolUseId, name, input, contentBlockIndex)
    - Tool events: Rich UX layer (display_content, messages, execution results, errors)

    EVENT TYPES:
    - current_tool_use: Strands-enhanced tool call info with UX metadata
      * display_content: Formatted code blocks, JSON displays, rich formatting
      * message: Progress indicators ("Searching the web...", "Running command...")
      * context: Additional metadata for the tool call
      * integration_name/icon_url: External integration branding (Google Drive, Slack, etc.)

    - tool_result / toolResult: Tool execution completed with results
      * Includes display_content for formatted output presentation

    - tool_error: Tool execution failed with error details

    - tool_stream_event: Streaming events from tools themselves (for streaming tools)

    IMPORTANT: Since Strands yields complete JSON objects (not partial), current_tool_use
    events will contain complete tool inputs when emitted.

    ENHANCEMENTS (v2):
    - Extract display_content from tool results for formatted output
    - Extract message field for progress indicators
    - Handle integration metadata for external tools (icons, names)

    Args:
        event: Raw event from Strands Agents

    Returns:
        List of processed events (may be empty if no tool events found)
    """
    events = []

    # current_tool_use: Information about the current tool being used
    # This is emitted when the agent decides to call a tool
    # We check for "name" to ensure it's a valid tool use event
    # Since Strands yields complete JSON objects, the input will be complete
    if "current_tool_use" in event and event["current_tool_use"].get("name"):
        # Normalize the tool use data to a consistent format
        tool_use_event_data = _extract_tool_use_data(event["current_tool_use"])
        events.append(_create_event("tool_use", {"tool_use": tool_use_event_data}))

    # tool_stream_event: Information about an event streamed from a tool
    # Some tools can stream their results incrementally (like sub-agents)
    # This event type handles those streaming tool responses
    if "tool_stream_event" in event:
        events.append(_create_event("tool_stream_event", {
            "tool_stream_event": event["tool_stream_event"]
        }))

    # tool_result / toolResult: Tool execution completed with results
    # Handle both snake_case and camelCase variants for compatibility
    # ENHANCEMENT: Extract display_content from results for better UX
    tool_result = event.get("tool_result") or event.get("toolResult")
    if tool_result:
        result_data = {"tool_result": tool_result}

        # Extract display_content if present (for formatted output)
        if isinstance(tool_result, dict) and "display_content" in tool_result:
            result_data["display_content"] = tool_result["display_content"]

        events.append(_create_event("tool_result", result_data))

    # tool_error: Tool execution failed with an error
    # Handle both snake_case and camelCase variants for compatibility
    tool_error = event.get("tool_error") or event.get("toolError")
    if tool_error:
        events.append(_create_event("tool_error", {"tool_error": tool_error}))

    return events


def _handle_reasoning_events(event: RawEvent) -> List[ProcessedEvent]:
    """Process reasoning-related events from the agent stream.

    REASONING EVENTS:
    Some models (like Claude Sonnet 4.5) support "reasoning" - showing their
    internal thought process before generating the final answer. These events
    capture that reasoning process.

    EVENT TYPES:
    - reasoning: Boolean flag indicating reasoning is happening
    - reasoningText: The actual reasoning text content (top-level or nested)
    - reasoning_signature: Cryptographic signature for reasoning verification
    - redactedContent: Reasoning that was redacted (for privacy/security)
    - reasoningContent: Nested structure containing reasoning data

    NOTE: All reasoning events use the same "reasoning" event type.
    The client can distinguish by checking which keys exist in the data.

    Args:
        event: Raw event from Strands Agents

    Returns:
        List of processed events (may be empty if no reasoning events found)
    """
    events = []

    # reasoning: True for reasoning events
    # This is a boolean flag that reasoning is happening
    # Some models emit this as a separate signal
    if event.get("reasoning", False):
        events.append(_create_event("reasoning", {}))

    # reasoningContent: Nested structure containing reasoning data
    # This is the structured format used by some models
    # It can contain reasoningText, signature, and redactedContent
    if "reasoningContent" in event:
        reasoning_content = event["reasoningContent"]
        reasoning_data = {}

        # Extract nested reasoningText structure
        if isinstance(reasoning_content, dict):
            # Handle reasoningText.text and reasoningText.signature
            if "reasoningText" in reasoning_content:
                reasoning_text_data = reasoning_content["reasoningText"]
                if isinstance(reasoning_text_data, dict):
                    if "text" in reasoning_text_data:
                        reasoning_data["reasoningText"] = reasoning_text_data["text"]
                    if "signature" in reasoning_text_data:
                        reasoning_data["reasoning_signature"] = reasoning_text_data["signature"]
                elif isinstance(reasoning_text_data, str):
                    # Sometimes it's just a string
                    reasoning_data["reasoningText"] = reasoning_text_data

            # Handle redactedContent (binary data)
            if "redactedContent" in reasoning_content:
                reasoning_data["redactedContent"] = reasoning_content["redactedContent"]

            # Handle signature at top level of reasoningContent
            if "signature" in reasoning_content and "reasoning_signature" not in reasoning_data:
                reasoning_data["reasoning_signature"] = reasoning_content["signature"]

        if reasoning_data:
            events.append(_create_event("reasoning", reasoning_data))

    # reasoningText: Text from reasoning process (top-level, for backward compatibility)
    # This contains the actual reasoning content - the model's "thinking"
    # Only process if not already handled via reasoningContent
    if "reasoningText" in event and "reasoningContent" not in event:
        events.append(_create_event("reasoning", {"reasoningText": event["reasoningText"]}))

    # reasoning_signature: Signature from reasoning process (top-level, for backward compatibility)
    # Cryptographic signature that can be used to verify the reasoning wasn't tampered with
    # Only process if not already handled via reasoningContent
    if "reasoning_signature" in event and "reasoningContent" not in event:
        events.append(_create_event("reasoning", {
            "reasoning_signature": event["reasoning_signature"]
        }))

    # redactedContent: Reasoning content redacted by the model (top-level, for backward compatibility)
    # Some reasoning may be redacted for privacy/security reasons
    # This is binary data (bytes) that was redacted
    # Only process if not already handled via reasoningContent
    if "redactedContent" in event and "reasoningContent" not in event:
        events.append(_create_event("reasoning", {
            "redactedContent": event["redactedContent"]
        }))

    return events


def _handle_citation_events(event: RawEvent) -> List[ProcessedEvent]:
    """Process citation events from the agent stream.

    CITATION EVENTS:
    Some models support citations - references to sources or documents that
    support the generated content. These events capture citation information.

    EVENT TYPES:
    - citation: Citation data from models that support citations
    - citationsContent: Array of citations (from content block structure)
    - citation_start: Marks the beginning of cited text (inline citation)
    - citation_end: Marks the end of cited text (inline citation)

    ENHANCEMENTS (v2):
    - Rich citation support with start/stop deltas for inline citations
    - This matches Claude.ai's citation format for RAG/web search integration
    - Clients can highlight cited text and show source information on hover

    INLINE CITATION FLOW:
    1. citation_start emitted with citation metadata (uuid, title, url, sources)
    2. Text content is generated (this text is being cited)
    3. citation_end emitted with citation_uuid to close the citation

    Example UX:
    "According to [OpenAI's documentation](https://openai.com), GPT-4 is..."
                  ^--- citation_start              ^--- citation_end

    Args:
        event: Raw event from Strands Agents

    Returns:
        List of processed events (may be empty if no citation events found)
    """
    events = []

    # ENHANCEMENT: citation_start_delta - marks beginning of cited text
    # This event contains full citation metadata and marks where citation begins
    # The client should track this UUID and associate it with following text
    if "citation_start_delta" in event:
        citation_start = event["citation_start_delta"]

        # Extract citation metadata
        citation_data = {}

        # Handle both direct citation object and nested "citation" key
        citation_obj = citation_start.get("citation", citation_start)

        if isinstance(citation_obj, dict):
            # UUID: Unique identifier for this citation (used to match start/end)
            if "uuid" in citation_obj:
                citation_data["citation_uuid"] = citation_obj["uuid"]

            # Title: Title of the source document/webpage
            if "title" in citation_obj:
                citation_data["title"] = citation_obj["title"]

            # URL: Link to the source
            if "url" in citation_obj:
                citation_data["url"] = citation_obj["url"]

            # Metadata: Additional information about the source
            # (site_domain, favicon_url, site_name, etc.)
            if "metadata" in citation_obj:
                citation_data["metadata"] = citation_obj["metadata"]

            # Origin tool: Which tool generated this citation
            # (e.g., "web_search", "google_drive_search")
            if "origin_tool_name" in citation_obj:
                citation_data["origin_tool_name"] = citation_obj["origin_tool_name"]

            # Sources: Array of source documents that support this citation
            # Each source may have: title, url, content_body, etc.
            if "sources" in citation_obj:
                citation_data["sources"] = citation_obj["sources"]

        if citation_data:
            events.append(_create_event("citation_start", citation_data))

    # ENHANCEMENT: citation_end_delta - marks end of cited text
    # This event contains only the citation UUID to close the citation
    # The client should stop associating text with this citation
    if "citation_end_delta" in event:
        citation_end = event["citation_end_delta"]

        # Extract citation UUID to match with start event
        citation_uuid = citation_end.get("citation_uuid")
        if citation_uuid:
            events.append(_create_event("citation_end", {
                "citation_uuid": citation_uuid
            }))

    # citation: Single citation event (top-level, for backward compatibility)
    # This is emitted when a model includes citation information
    # Only process if not already handled via citation_start_delta
    if "citation" in event and "citation_start_delta" not in event:
        events.append(_create_event("citation", {"citation": event["citation"]}))

    # citationsContent: Array of citations (from content block structure)
    # Some models emit citations as an array within content blocks
    # Only process if not already handled via citation_start_delta
    if "citationsContent" in event and "citation_start_delta" not in event:
        citations = event["citationsContent"]
        # Handle both array and single citation formats
        if isinstance(citations, list):
            for citation in citations:
                events.append(_create_event("citation", {"citation": citation}))
        else:
            events.append(_create_event("citation", {"citation": citations}))

    return events


def _handle_metadata_events(event: RawEvent) -> List[ProcessedEvent]:
    """Extract usage metrics and latency information from metadata events.

    METADATA EVENTS:
    These events contain token usage statistics and performance metrics
    from the model provider. They are typically emitted at the end of
    model invocations or periodically during streaming.

    EVENT TYPES:
    - usage: Token usage information (inputTokens, outputTokens, totalTokens, cacheReadInputTokens, cacheWriteInputTokens)
    - metrics: Performance metrics (latencyMs, timeToFirstByteMs)
    - metadata: Combined metadata object containing both usage and metrics
    - result.metrics: AgentResult metrics (accumulated_usage, accumulated_metrics)
    - event.modelMetadataEvent: Nested metadata event from model stream
    - modelMetadataEvent: Direct event type from Strands streaming

    ENHANCEMENTS (v2):
    - Cache token tracking (cacheReadInputTokens, cacheWriteInputTokens)
    - Critical for prompt caching cost analysis and optimization
    - Extract metadata from result object (AgentResult.metrics)
    - Extract metadata from nested event structures

    NOTE: Metadata events are important for cost tracking and performance monitoring.
    They may appear in multiple locations:
    1. Top-level fields (metadata, usage, metrics)
    2. Nested in result object (result.metrics.accumulated_usage/accumulated_metrics)
    3. Nested in event structure (event.modelMetadataEvent)
    4. As event type (modelMetadataEvent)

    Args:
        event: Raw event from Strands Agents

    Returns:
        List of processed events (may be empty if no metadata events found)
    """
    events = []

    def _extract_usage_data(usage_obj: Any) -> Dict[str, Any]:
        """Extract and normalize usage data from various formats.

        Extracts:
        - inputTokens/input_tokens: Number of input tokens
        - outputTokens/output_tokens: Number of output tokens
        - totalTokens/total_tokens: Total number of tokens
        - cacheReadInputTokens/cache_read_input_tokens: Tokens read from cache (optional)
        - cacheWriteInputTokens/cache_write_input_tokens: Tokens written to cache (optional)

        Handles both camelCase (Bedrock API) and snake_case (Python SDK) formats.
        """
        if not usage_obj:
            return {}
        
        # Handle dict format
        if isinstance(usage_obj, dict):
            usage_data = {
                "inputTokens": usage_obj.get("inputTokens") or usage_obj.get("input_tokens", 0),
                "outputTokens": usage_obj.get("outputTokens") or usage_obj.get("output_tokens", 0),
                "totalTokens": usage_obj.get("totalTokens") or usage_obj.get("total_tokens", 0),
            }
            
            # Add cache token fields if present
            # Handle both camelCase and snake_case variants
            # Use 'is not None' check to distinguish between absent field and 0 value
            cache_read = usage_obj.get("cacheReadInputTokens")
            if cache_read is None:
                cache_read = usage_obj.get("cache_read_input_tokens")

            cache_write = usage_obj.get("cacheWriteInputTokens")
            if cache_write is None:
                cache_write = usage_obj.get("cache_write_input_tokens")

            # Include cache fields if they exist (even if 0)
            if cache_read is not None:
                usage_data["cacheReadInputTokens"] = cache_read
            if cache_write is not None:
                usage_data["cacheWriteInputTokens"] = cache_write
            
            return usage_data
        
        return {}

    def _extract_metrics_data(metrics_obj: Any) -> Dict[str, Any]:
        """Extract and normalize metrics data from various formats."""
        if not metrics_obj:
            return {}
        
        # Handle dict format
        if isinstance(metrics_obj, dict):
            metrics_data = {
                "latencyMs": metrics_obj.get("latencyMs") or metrics_obj.get("latency_ms", 0),
            }
            
            # Add timeToFirstByteMs if available
            ttfb = metrics_obj.get("timeToFirstByteMs") or metrics_obj.get("time_to_first_byte_ms")
            if ttfb is not None:
                metrics_data["timeToFirstByteMs"] = ttfb
            
            return metrics_data
        
        return {}

    # Check for combined metadata object first (top-level)
    if "metadata" in event:
        metadata = event["metadata"]
        metadata_data = {}

        # Extract usage information
        if "usage" in metadata:
            usage_data = _extract_usage_data(metadata["usage"])
            if usage_data:
                metadata_data["usage"] = usage_data

        # Extract metrics information
        if "metrics" in metadata:
            metrics_data = _extract_metrics_data(metadata["metrics"])
            if metrics_data:
                metadata_data["metrics"] = metrics_data

        if metadata_data:
            events.append(_create_event("metadata", metadata_data))

    # Check for standalone usage field (top-level)
    # Note: Using separate if (not elif) to check all sources
    if "usage" in event and "metadata" not in event:
        usage_data = _extract_usage_data(event["usage"])
        if usage_data:
            events.append(_create_event("metadata", {"usage": usage_data}))

    # Check for standalone metrics field (top-level)
    # Note: Using separate if (not elif) to check all sources
    if "metrics" in event and "metadata" not in event:
        metrics_data = _extract_metrics_data(event["metrics"])
        if metrics_data:
            events.append(_create_event("metadata", {"metrics": metrics_data}))

    # Check for metadata in result object (AgentResult.metrics)
    # This is where Strands Python SDK stores metrics
    if "result" in event:
        result = event["result"]
        
        # Handle both dict (serialized) and object (AgentResult) formats
        if isinstance(result, dict):
            result_metrics = result.get("metrics", {})
        else:
            # If result is an object, try to get metrics attribute
            result_metrics = getattr(result, "metrics", None)
            if result_metrics:
                # Convert to dict if it has to_dict or similar
                if hasattr(result_metrics, "to_dict"):
                    result_metrics = result_metrics.to_dict()
                elif hasattr(result_metrics, "__dict__"):
                    result_metrics = result_metrics.__dict__
                else:
                    result_metrics = {}

        if result_metrics:
            metadata_data = {}
            
            # Extract accumulated_usage from metrics
            accumulated_usage = result_metrics.get("accumulated_usage") or result_metrics.get("accumulatedUsage")
            if accumulated_usage:
                usage_data = _extract_usage_data(accumulated_usage)
                if usage_data:
                    metadata_data["usage"] = usage_data
            
            # Extract accumulated_metrics from metrics
            accumulated_metrics = result_metrics.get("accumulated_metrics") or result_metrics.get("accumulatedMetrics")
            if accumulated_metrics:
                metrics_data = _extract_metrics_data(accumulated_metrics)
                if metrics_data:
                    metadata_data["metrics"] = metrics_data
            
            if metadata_data:
                metadata_event = _create_event("metadata", metadata_data)
                events.append(metadata_event)

    # Check for metadata in nested event structure (like content blocks)
    # Some models emit metadata events nested in the event.event structure
    if "event" in event and isinstance(event["event"], dict):
        inner_event = event["event"]
        
        # Check for modelMetadataEvent (Strands TypeScript SDK format)
        if "modelMetadataEvent" in inner_event:
            model_metadata = inner_event["modelMetadataEvent"]
            metadata_data = {}
            
            if "usage" in model_metadata:
                usage_data = _extract_usage_data(model_metadata["usage"])
                if usage_data:
                    metadata_data["usage"] = usage_data
            
            if "metrics" in model_metadata:
                metrics_data = _extract_metrics_data(model_metadata["metrics"])
                if metrics_data:
                    metadata_data["metrics"] = metrics_data
            
            if metadata_data:
                events.append(_create_event("metadata", metadata_data))
        
        # Check for metadata/usage/metrics directly in nested event
        elif "metadata" in inner_event:
            metadata = inner_event["metadata"]
            metadata_data = {}
            
            if "usage" in metadata:
                usage_data = _extract_usage_data(metadata["usage"])
                if usage_data:
                    metadata_data["usage"] = usage_data
            
            if "metrics" in metadata:
                metrics_data = _extract_metrics_data(metadata["metrics"])
                if metrics_data:
                    metadata_data["metrics"] = metrics_data
            
            if metadata_data:
                events.append(_create_event("metadata", metadata_data))
        
        elif "usage" in inner_event:
            usage_data = _extract_usage_data(inner_event["usage"])
            if usage_data:
                events.append(_create_event("metadata", {"usage": usage_data}))
        
        elif "metrics" in inner_event:
            metrics_data = _extract_metrics_data(inner_event["metrics"])
            if metrics_data:
                events.append(_create_event("metadata", {"metrics": metrics_data}))

    # Check for modelMetadataEvent as direct event type
    if event.get("type") == "modelMetadataEvent" or event.get("event_type") == "modelMetadataEvent":
        metadata_data = {}
        
        if "usage" in event:
            usage_data = _extract_usage_data(event["usage"])
            if usage_data:
                metadata_data["usage"] = usage_data
        
        if "metrics" in event:
            metrics_data = _extract_metrics_data(event["metrics"])
            if metrics_data:
                metadata_data["metrics"] = metrics_data
        
        if metadata_data:
            events.append(_create_event("metadata", metadata_data))

    return events


async def process_agent_stream(
    agent_stream: AsyncGenerator[RawEvent, None]
) -> AsyncGenerator[ProcessedEvent, None]:
    """
    Process and transform raw agent stream events into standardized format.

    MAIN PROCESSING FLOW:
    ====================
    1. Iterate through raw events from agent.stream_async()
    2. For each event, process it through specialized handlers:
       a. Metadata events FIRST (extract before completion to ensure it's sent)
       b. Completion events (may break early after metadata is extracted)
       c. Lifecycle events (state tracking) - COMMENTED OUT for production, available for debug
       d. Content block events (structural foundation - message/block start/delta/stop)
       e. Tool events (rich UX layer - display_content, progress, execution results)
       f. Reasoning events (model thinking)
       g. Citation events (source references) - ENHANCED with inline citations
    3. Yield processed events one by one
    4. Accumulate metadata for final summary
    5. Yield "metadata_summary" event before "done"
    6. Send final "done" event when stream completes
    7. Handle errors gracefully
    8. Ensure proper generator cleanup on cancellation

    METADATA ACCUMULATION:
    =====================
    The processor accumulates metadata (token usage, metrics) during streaming
    and yields a final "metadata_summary" event before "done". This allows
    the caller to store metadata without adding side effects to the processor.

    DUAL-LAYER ARCHITECTURE FOR TOOLS:
    - Content blocks provide structural foundation (contentBlockIndex, basic tool info)
    - Tool events provide rich UX enhancements (display_content, messages, execution results)
    - Both layers work together for complete rich tool experience

    PROCESSING ORDER MATTERS:
    - Metadata events are processed FIRST to ensure they're extracted even if complete=True
    - Completion events are checked SECOND (after metadata) because they signal the end
    - If complete/error occurs, we break immediately (but metadata was already processed)
    - Other events are processed in order but can all occur in the same raw event

    CANCELLATION HANDLING:
    - Client disconnection is detected automatically at the HTTP layer
    - Strands force_stop events are handled via completion events

    EVENT TYPES HANDLED:
    - Lifecycle: init_event_loop, start_event_loop, message, complete, force_stop, result (DEBUG ONLY - commented out)
    - Content Blocks: message_start, content_block_start, content_block_delta, content_block_stop, message_stop
    - Tool: current_tool_use, tool_stream_event, tool_result, tool_error
    - Reasoning: reasoning, reasoningText, reasoning_signature, redactedContent, reasoningContent
    - Citations: citation, citationsContent, citation_start, citation_end (ENHANCED)
    - Metadata: usage, metrics, metadata (token usage with cache tracking, performance metrics)
    - Summary: metadata_summary (final accumulated metadata before done)

    ENHANCEMENTS (v2):
    ==================
    - Structured content block tracking (message_start, content_block_start/delta/stop, message_stop)
    - Content block indexing for handling multiple blocks per message
    - Rich inline citation support with citation_start/citation_end events
    - Tool display_content extraction for better UX
    - Tool message/progress indicator support
    - Integration metadata support for external tools
    - Cache token metrics for prompt caching cost analysis

    ENHANCEMENTS (v3):
    ==================
    - Metadata accumulation during streaming (in-memory)
    - metadata_summary event with complete token usage and timing
    - First token time tracking for latency calculation

    Args:
        agent_stream: Async generator yielding raw events from Strands Agents

    Yields:
        Dictionary events containing:
        - "type": Event type matching Strands event structure
        - "data": Event-specific data dictionary (always JSON-serializable)

    Example:
        ```python
        agent_stream = agent.stream_async(message)
        async for event in process_agent_stream(agent_stream):
            # event format: {"type": "content_block_delta", "data": {"text": "..."}}
            pass
        ```

    Testing:
        ```python
        # Mock a simple text stream with citations
        async def mock_stream():
            yield {"start_event_loop": True}
            yield {"event": {"contentBlockDelta": {"delta": {"text": "According to "}}}}
            yield {
                "citation_start_delta": {
                    "citation": {
                        "uuid": "abc-123",
                        "title": "Example Source",
                        "url": "https://example.com"
                    }
                }
            }
            yield {"event": {"contentBlockDelta": {"delta": {"text": "the documentation"}}}}
            yield {"citation_end_delta": {"citation_uuid": "abc-123"}}
            yield {"event": {"contentBlockDelta": {"delta": {"text": ", this is correct."}}}}
            yield {"complete": True}

        events = []
        async for event in process_agent_stream(mock_stream()):
            events.append(event)

        assert events[0]["type"] == "start_event_loop"
        assert events[1]["data"]["text"] == "According to "
        assert events[2]["type"] == "citation_start"
        assert events[2]["data"]["citation_uuid"] == "abc-123"
        ```
    """
    # Accumulate metadata during streaming (in-memory only, no side effects)
    accumulated_metadata: Dict[str, Any] = {
        "usage": {},
        "metrics": {}
    }
    first_token_time: Optional[float] = None

    # Track current content block index for providers that don't send it (like OpenAI)
    # Using dict for mutability across function calls
    current_block_index: Dict[str, int] = {"index": 0}

    try:
        # Track if we've seen result to know when it's safe to break on complete
        result_seen = False

        # Iterate through each raw event from the agent stream
        # The agent stream is an async generator that yields events as they occur
        async for event in agent_stream:

            # Validate event structure (basic check)
            if not isinstance(event, dict):
                logger.warning(f"Unexpected event type: {type(event)}, expected dict")
                continue

            # STEP 1: Process metadata events FIRST (before completion check)
            # IMPORTANT: Process metadata BEFORE checking completion to ensure
            # metadata is extracted even if complete=True is in the same event.
            # Metadata is critical for cost tracking and should always be sent.
            # ALSO accumulate metadata for final summary
            # NOTE: timeToFirstByteMs from provider will be stored in metrics
            # and the coordinator can use it as a fallback if first_token_time is not set
            for processed_event in _handle_metadata_events(event):
                # Accumulate metadata for summary
                if processed_event.get("type") == "metadata":
                    event_data = processed_event.get("data", {})
                    if "usage" in event_data:
                        accumulated_metadata["usage"].update(event_data["usage"])
                    if "metrics" in event_data:
                        accumulated_metadata["metrics"].update(event_data["metrics"])
                # Yield the metadata event
                yield processed_event

            # STEP 2: Process completion/error events (may break the loop)
            # These events signal the end of processing, so we check them after metadata
            # IMPORTANT: We check for 'result' before breaking - result may contain metrics
            # and might come in the same event as complete, or in the next event
            completion_events, should_break = _handle_completion_events(event)
            for processed_event in completion_events:
                yield processed_event

            # If we should break, check one more time for metadata
            # IMPORTANT: Don't break immediately if we haven't seen result yet
            # The result event (which contains metrics) might come after complete
            if should_break:
                # Check one more time for metadata in case result came with complete
                metadata_events_after_complete = _handle_metadata_events(event)
                for processed_event in metadata_events_after_complete:
                    # Accumulate metadata for summary
                    if processed_event.get("type") == "metadata":
                        event_data = processed_event.get("data", {})
                        if "usage" in event_data:
                            accumulated_metadata["usage"].update(event_data["usage"])
                        if "metrics" in event_data:
                            accumulated_metadata["metrics"].update(event_data["metrics"])
                    # Yield the metadata event
                    yield processed_event

                # If we've seen result, it's safe to break
                # Otherwise, continue to next iteration to catch result event
                if result_seen:
                    break
                # Don't break yet - continue to catch result event in next iteration

            # STEP 3: Process lifecycle events
            # NOTE: We process lifecycle events to capture the 'result' event which contains metrics
            # The result event is needed for metadata extraction, but we don't yield it to avoid
            # sending large result objects to the client. Metadata is extracted separately.
            lifecycle_events = _handle_lifecycle_events(event)
            # Only yield non-result lifecycle events (result is processed for metadata only)
            for processed_event in lifecycle_events:
                # Skip result events - we only use them for metadata extraction
                if processed_event.get("type") != "result":
                    yield processed_event

            # STEP 4: Process content block events
            # These track the lifecycle of content blocks (text and tool uses) in the model's response
            # Provides structured tracking with contentBlockIndex, messageStart/Stop, etc.
            # ALSO track first token time for latency calculation
            for processed_event in _handle_content_block_events(event, current_block_index):
                # Track first token time for latency calculation
                # Track ANY content block delta (text OR tool use) as first token
                # Reasoning events are tracked separately in STEP 6
                if first_token_time is None:
                    event_type = processed_event.get("type")
                    if event_type == "content_block_delta":
                        event_data = processed_event.get("data", {})
                        # Track first token for both text and tool use deltas
                        # Tool use deltas indicate the model is generating tool calls
                        if event_data.get("type") in ("text", "tool_use"):
                            first_token_time = time.time()
                            logger.debug(f"First token detected (content_block_delta, type={event_data.get('type')})")
                yield processed_event

            # STEP 5: Process tool events (ENHANCED with display_content)
            # These occur when the agent decides to use a tool
            # Since Strands yields complete JSON objects, inputs will be complete
            for processed_event in _handle_tool_events(event):
                yield processed_event

            # STEP 6: Process reasoning events
            # These show the model's internal thought process (if supported)
            # Not all models support reasoning
            # ALSO track first token time if reasoning comes before text (some models emit reasoning first)
            for processed_event in _handle_reasoning_events(event):
                # Track first token time for reasoning events
                # Reasoning text is the first output from reasoning-capable models
                if first_token_time is None:
                    event_type = processed_event.get("type")
                    if event_type == "reasoning":
                        event_data = processed_event.get("data", {})
                        # If there's reasoningText, this is the first token
                        if "reasoningText" in event_data or "reasoning" in event_data:
                            first_token_time = time.time()
                            logger.debug("First token detected (reasoning event)")
                yield processed_event

            # STEP 7: Process citation events (ENHANCED with inline citations)
            # These contain source references from models that support citations
            # Now includes citation_start and citation_end for inline citations
            for processed_event in _handle_citation_events(event):
                yield processed_event

        # STEP 8: Yield metadata summary before done event
        # This provides all accumulated metadata in one place for storage
        if accumulated_metadata.get("usage") or accumulated_metadata.get("metrics"):
            # Add first_token_time if we tracked it (caller will calculate latency)
            # NOTE: If first_token_time is None, the coordinator can use timeToFirstByteMs
            # from the metrics as a fallback (it's already in accumulated_metadata["metrics"])
            if first_token_time is not None:
                accumulated_metadata["first_token_time"] = first_token_time
            else:
                # Log if we couldn't detect first token time
                # This helps diagnose cases where no text/reasoning/tool deltas were detected
                logger.debug("first_token_time not detected - coordinator can use timeToFirstByteMs from metrics as fallback")
            yield _create_event("metadata_summary", accumulated_metadata)

        # STEP 9: Send final done event
        # This signals to the client that the stream has completed successfully
        # It's sent after all events have been processed
        yield _create_event("done", {})

    except GeneratorExit:
        # Handle generator cleanup when consumer cancels/closes
        # This ensures proper resource cleanup when the client disconnects
        logger.debug("Stream processor generator closed by consumer")
        # GeneratorExit should be re-raised to properly close the generator
        raise

    except StopAsyncIteration:
        # Normal end of iteration - stream completed naturally
        # This is expected and not an error
        logger.debug("Stream ended normally")
        # Send done event if we haven't already
        yield _create_event("done", {})

    except RuntimeError as e:
        # RuntimeError may occur during generator cleanup or cancellation
        # Check if it's related to generator state
        if "generator" in str(e).lower() or "async" in str(e).lower():
            logger.debug(f"Generator runtime error (likely cleanup): {e}")
        else:
            # Unexpected RuntimeError - treat as error
            logger.error(f"Runtime error processing agent stream: {e}", exc_info=True)
            # Create structured error event
            error_event = StreamErrorEvent(
                error="Runtime error during streaming",
                code=ErrorCode.STREAM_ERROR,
                detail=str(e),
                recoverable=False
            )
            yield _create_event("error", error_event.model_dump(exclude_none=True))

    except Exception as e:
        # ERROR HANDLING: If anything else goes wrong, log it and send an error event
        # This ensures the client always gets a response, even on failure
        logger.error(f"Error processing agent stream: {e}", exc_info=True)
        # Create structured error event
        error_event = StreamErrorEvent(
            error="An unexpected error occurred during streaming",
            code=ErrorCode.STREAM_ERROR,
            detail=str(e),
            recoverable=False
        )
        yield _create_event("error", error_event.model_dump(exclude_none=True))
