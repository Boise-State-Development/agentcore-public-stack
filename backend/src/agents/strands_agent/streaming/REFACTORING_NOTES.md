# Stream Processor Refactoring Notes

## Summary

Replaced the stateful, business-logic-heavy `StreamEventProcessor` class with a clean, stateless `process_agent_stream()` function that follows functional programming principles.

## What Changed

### ✅ **New Implementation**

**File:** `stream_processor.py`

- **Pure function**: `process_agent_stream(agent_stream) -> AsyncGenerator`
- **Stateless**: No mutable state, no side effects
- **Modular**: Specialized handler functions for each event category
- **Type-safe**: Uses `TypedDict` for event structures
- **Well-documented**: Comprehensive inline documentation with architecture explanations

### ❌ **Old Implementation (Deprecated)**

**File:** `event_processor.py` (still exists for backward compatibility)

- Stateful class with `seen_tool_uses`, `pending_events`, `tool_use_registry`
- Mixed concerns: stream processing + business logic
- Session management, tool execution context creation
- XML parsing workarounds
- Complex deduplication logic

## Key Architectural Changes

### 1. **Dual-Layer Event Architecture**

The new processor recognizes two layers of events:

- **Layer 1 (Structural)**: Content block events
  - `message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_stop`
  - Provides `contentBlockIndex` for proper message reconstruction
  - Matches Bedrock Converse API structure

- **Layer 2 (Rich UX)**: Tool events
  - `tool_use`, `tool_result`, `tool_error`, `tool_stream_event`
  - Includes `display_content`, progress messages, integration metadata
  - Enhances UX without coupling to message structure

### 2. **Event Processing Flow**

```
Raw Agent Stream (from Strands)
    ↓
process_agent_stream()
    ├─> Completion events (break early if done/error)
    ├─> Content block events (structural foundation)
    ├─> Tool events (rich UX layer)
    ├─> Reasoning events (model thinking)
    ├─> Citation events (inline citations with start/end)
    └─> Metadata events (usage + cache tokens)
    ↓
Standardized Events: {"type": str, "data": dict}
    ↓
StreamCoordinator (formats as SSE)
    ↓
SSE Response to Client
```

### 3. **Removed Business Logic**

The following concerns were removed from stream processing:

1. **Session Management** → Remains in session manager
2. **Tool Execution Context** → Should be handled in routes/agent layer
3. **Tool Use Registry** → No longer needed (or move to separate service)
4. **XML Parsing** → Removed (not needed with current Strands SDK)
5. **Tool Input Validation** → Removed (Strands yields complete JSON objects)
6. **Deduplication** → Removed (should be handled at HTTP layer if needed)
7. **Image Extraction/File Saving** → Should be moved to separate `ToolResultProcessor`

## Event Types Handled

### Content Block Events (Structural)
- `message_start` - Beginning of assistant message
- `content_block_start` - Start of text or tool use block (with index)
- `content_block_delta` - Incremental text or tool input updates
- `content_block_stop` - End of content block
- `message_stop` - End of message (with stopReason)

### Tool Events (Rich UX)
- `tool_use` - Tool call with complete input (Strands yields complete JSON)
- `tool_result` - Tool execution completed
- `tool_error` - Tool execution failed
- `tool_stream_event` - Streaming tool responses

### Reasoning Events
- `reasoning` - Model thinking process (Claude Sonnet 4.5+)

### Citation Events (Enhanced)
- `citation_start` - Beginning of cited text (with metadata)
- `citation_end` - End of cited text
- `citation` - Legacy citation format

### Metadata Events
- `metadata` - Token usage (including **cache tokens**) and performance metrics

### Completion Events
- `complete` - Normal completion
- `error` - Error or force_stop
- `done` - Final event (sent by processor)

## Cache Token Support

**CRITICAL ADDITION**: The new processor includes cache token metrics for prompt caching cost analysis:

```python
# In metadata events
{
    "type": "metadata",
    "data": {
        "usage": {
            "inputTokens": 1000,
            "outputTokens": 500,
            "totalTokens": 1500,
            "cacheReadInputTokens": 800,    # ✅ NEW - Cache hits (90% cheaper)
            "cacheWriteInputTokens": 1000   # ✅ NEW - Cache writes
        }
    }
}
```

## Updated Files

### Core Implementation
- ✅ `stream_processor.py` - New pure function implementation (NEW)
- ✅ `stream_coordinator.py` - Updated to use new processor
- ✅ `strands_agent.py` - Removed stream processor dependency
- ✅ `global_state.py` - Deprecated (kept for backward compatibility)

### Exports
- ✅ `streaming/__init__.py` - Exports `process_agent_stream` function

## Migration Guide

### For New Code

```python
from agents.strands_agent.streaming import process_agent_stream

# Get raw agent stream
agent_stream = agent.stream_async(message)

# Process events
async for event in process_agent_stream(agent_stream):
    # event format: {"type": "content_block_delta", "data": {"text": "..."}}
    print(event["type"], event["data"])
```

### For Existing Code

The `StreamCoordinator` has been updated to use the new processor internally, so existing code using `StreamCoordinator.stream_response()` will work without changes.

## Testing

The new implementation includes testing examples in the docstring:

```python
# Mock a simple stream
async def mock_stream():
    yield {"event": {"contentBlockDelta": {"delta": {"text": "Hello"}}}}
    yield {"complete": True}

events = []
async for event in process_agent_stream(mock_stream()):
    events.append(event)

assert events[0]["type"] == "content_block_delta"
assert events[0]["data"]["text"] == "Hello"
assert events[1]["type"] == "complete"
assert events[2]["type"] == "done"
```

## Completed Refactoring Tasks

### 1. **Tool Result Processing** ✅ COMPLETED (2025-12-02)

Created a separate `ToolResultProcessor` class for handling tool result post-processing:

**File:** `tool_result_processor.py`

**Features:**
- Image extraction from various formats (MCP, JSON responses, base64)
- Base64 file saving to disk (Python MCP tools)
- JSON content processing and cleaning
- Text cleaning for display (removing large base64 data)
- Stateless, side-effect based processing (called by router layer)

**Usage:**
```python
from agents.strands_agent.streaming import ToolResultProcessor

# Process tool result (typically called by event formatter or router)
text, images = ToolResultProcessor.process_tool_result(
    tool_result={"content": [{"text": "Result"}]},
    session_id="abc123",
    tool_name="run_python_code"
)
```

**Benefits:**
- Separation of concerns: Stream processing vs. file I/O
- No coupling to global state or registries
- Explicit dependencies (session_id, tool_name passed as parameters)
- Easy to test and maintain

### 2. **Event Formatter Cleanup** ✅ COMPLETED (2025-12-02)

Refactored `event_formatter.py` to delegate tool result processing:

**Changes:**
- Removed `_extract_all_content()` - moved to `ToolResultProcessor`
- Removed `_extract_basic_content()` - moved to `ToolResultProcessor`
- Removed `_process_json_content()` - moved to `ToolResultProcessor`
- Removed `_extract_images_from_json_response()` - moved to `ToolResultProcessor`
- Removed `_clean_result_text_for_display()` - moved to `ToolResultProcessor`
- Removed `_process_base64_downloads()` - moved to `ToolResultProcessor`
- Removed `_handle_python_mcp_base64()` - moved to `ToolResultProcessor`
- Removed `_handle_tool_storage()` - moved to `ToolResultProcessor`
- Removed `_get_tool_info()` - no longer needed (no global registry dependency)

**New Signature:**
```python
@staticmethod
def create_tool_result_event(
    tool_result: Dict[str, Any],
    session_id: str = None,
    tool_name: str = None
) -> str:
    # Delegates to ToolResultProcessor
    result_text, result_images = ToolResultProcessor.process_tool_result(...)
    return StreamEventFormatter._build_tool_result_event(...)
```

### 3. **Deprecated File Cleanup** ✅ COMPLETED (2025-12-02)

Deleted the deprecated `event_processor.py` file from `agents/strands_agent/streaming/`.

**Note:** The `agentcore/utils/event_processor.py` file still exists and is used by `agentcore/agent/agent.py`. This is a separate implementation and should remain until the agentcore agent is fully migrated to the new architecture.

## Benefits of New Implementation

1. ✅ **Separation of Concerns** - Stream processing is pure transformation
2. ✅ **Testability** - Pure functions are easy to unit test
3. ✅ **Type Safety** - TypedDict provides IDE support and type checking
4. ✅ **Documentation** - Comprehensive inline docs explain the "why"
5. ✅ **Maintainability** - Modular handlers are easy to extend
6. ✅ **Performance** - No unnecessary state management overhead
7. ✅ **Cache Metrics** - Track prompt caching for cost optimization

## Code Quality Improvements

- **No mixed concerns**: Stream processing doesn't create tool contexts or save files
- **No global state**: Fully stateless, thread-safe
- **No side effects**: Pure transformation of events
- **No business logic**: Delegates to appropriate layers
- **Clean error handling**: Specific exception types, proper cleanup
- **Production-ready**: Handles all edge cases gracefully

## Questions?

If you encounter issues or have questions about the new implementation:

1. Check the inline documentation in `stream_processor.py`
2. Review the testing examples in the docstrings
3. Check this document for migration guidance
4. Consider whether you need stream processing or business logic

---

**Last Updated**: 2025-12-02
**Author**: Claude Code (via refactoring session)
