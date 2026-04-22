# SSE Error Messaging Architecture

This document describes how error messages (quota exceeded, streaming errors) are delivered to users as conversational assistant responses rather than HTTP errors, providing a better user experience.

## Overview

Instead of returning HTTP error codes (e.g., 429 for quota exceeded, 500 for server errors), we stream error messages as SSE events that appear as assistant responses in the chat. This approach:

1. **Better UX** - Users see a friendly message in the conversation flow
2. **Persistence** - Messages are saved to session history
3. **Context** - Users can see what they asked and why it failed
4. **Consistency** - Error handling follows the same UI patterns as normal responses

## Architecture

### Backend Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                        /invocations Endpoint                         │
├─────────────────────────────────────────────────────────────────────┤
│  1. Check quota                                                      │
│     └─> If exceeded: build QuotaExceededEvent                       │
│                                                                      │
│  2. If error event exists:                                          │
│     └─> Stream as SSE (message_start → content_block_* → done)      │
│     └─> Save to AgentCore Memory via create_message()               │
│                                                                      │
│  3. Otherwise: Normal agent streaming                                │
└─────────────────────────────────────────────────────────────────────┘
```

### SSE Event Sequence

When an error occurs, we emit standard SSE events so the frontend processes them like normal assistant messages:

```
event: message_start
data: {"role": "assistant"}

event: content_block_start
data: {"contentBlockIndex": 0, "type": "text"}

event: content_block_delta
data: {"contentBlockIndex": 0, "type": "text", "text": "<markdown message>"}

event: content_block_stop
data: {"contentBlockIndex": 0}

event: message_stop
data: {"stopReason": "quota_exceeded"}  // or "error"

event: quota_exceeded  // or "stream_error" - additional metadata for UI
data: {"type": "quota_exceeded", "currentUsage": 10.5, ...}

event: done
data: {}
```

### Frontend Handling

The `stream-parser.service.ts` processes these events:

1. **Standard events** (`message_start`, `content_block_*`, `message_stop`) - Handled by existing message building logic, displays as assistant message
2. **Metadata events** (`quota_exceeded`, `stream_error`) - Parsed and stored in `QuotaWarningService` or `ErrorService` for UI enhancements

```typescript
// stream-parser.service.ts
case 'quota_exceeded':
  this.handleQuotaExceeded(data);
  break;

case 'stream_error':
  this.handleStreamError(data);
  break;
```

### Session Persistence

Messages are saved to AgentCore Memory using the `SessionMessage` API:

```python
from strands.types.session import SessionMessage

# Create session manager
session_manager = SessionFactory.create_session_manager(
    session_id=session_id,
    user_id=user_id,
    caching_enabled=False
)

# Build messages
user_message = {
    "role": "user",
    "content": [{"text": user_input}]
}

assistant_message = {
    "role": "assistant",
    "content": [{"text": error_message_markdown}]
}

# Save via base_manager.create_message()
if hasattr(session_manager, 'base_manager') and hasattr(session_manager.base_manager, 'create_message'):
    user_session_msg = SessionMessage.from_message(user_message, 0)
    assistant_session_msg = SessionMessage.from_message(assistant_message, 1)

    session_manager.base_manager.create_message(session_id, "default", user_session_msg)
    session_manager.base_manager.create_message(session_id, "default", assistant_session_msg)
```

## Implementation Details

### Quota Exceeded

**Backend**: `apis/shared/quota.py`
- `QuotaExceededEvent` - Pydantic model with `to_sse_format()` method
- `build_quota_exceeded_event()` - Builds event with markdown message

**Routes**: `apis/inference_api/chat/routes.py`, `apis/app_api/chat/routes.py`
- Check quota before processing
- If exceeded, stream `QuotaExceededEvent` instead of calling agent

**Frontend**: `services/quota/quota-warning.service.ts`
- `QuotaExceeded` interface
- `setQuotaExceeded()` method
- Signals: `isQuotaExceeded`, `severity`, `formattedUsage`, `resetInfo`

### Streaming Errors

**Backend**: `apis/shared/errors.py`
- `StreamErrorEvent` - Pydantic model with `to_sse_format()` method
- `build_stream_error_event()` - Builds event with markdown message

**Routes**: Wrap agent streaming in try/catch, emit error event on failure

**Frontend**: `services/error/error.service.ts`
- `StreamError` interface
- Error state signals for UI

### OAuth Required

**Backend**: `apis/shared/oauth/models.py`
- `OAuthRequiredEvent` - Pydantic model with `to_sse_format()` method
- Event name: `oauth_required`, payload: `{providerId, authorizationUrl}`

**Routes**: `apis/inference_api/chat/routes.py`
- After the agent stream drains, pull consent URLs from
  `ExternalMCPIntegration.drain_pending_consent(user_id)` and emit one
  `oauth_required` event per provider before `done`.
- Consent URLs are populated during external MCP tool loading whenever
  AgentCore Identity reports the user hasn't granted the required scopes.

## Adding New Error Types

1. **Create event model** in `apis/shared/errors.py`:
```python
class MyErrorEvent(BaseModel):
    type: str = "my_error"
    message: str
    # ... additional fields

    def to_sse_format(self) -> str:
        import json
        return f"event: my_error\ndata: {json.dumps(self.model_dump())}\n\n"
```

2. **Create builder function**:
```python
def build_my_error_event(...) -> MyErrorEvent:
    message = f"""**Error Title**

Details about what happened...

**What to do:**
- Step 1
- Step 2
"""
    return MyErrorEvent(message=message, ...)
```

3. **Update routes** to catch error condition and stream the event

4. **Update frontend** `stream-parser.service.ts`:
```typescript
case 'my_error':
  this.handleMyError(data);
  break;
```

5. **Create/update service** to manage error state with signals

## Markdown Styling Guidelines

Use consistent markdown formatting for error messages:

```markdown
**Error Title** or I apologize, but...

**Details Section**
| Field | Value |
|-------|-------|
| Key   | Value |

**What's Next?**
- Action item 1
- Action item 2

Friendly closing message.
```
