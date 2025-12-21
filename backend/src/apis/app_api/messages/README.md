# Messages API

Provides endpoints to retrieve conversation history with JWT authentication.

## Endpoints

### GET /sessions/{session_id}/messages (Recommended)

Retrieve messages for a specific session with pagination support.

**Authentication Required:** Yes (JWT Bearer token)

**Path Parameters:**
- `session_id` (string, required): Session identifier

**Query Parameters:**
- `limit` (integer, optional): Maximum number of messages to return (1-1000, default: no limit)
- `next_token` (string, optional): Pagination token for retrieving the next page of results

**Headers:**
- `Authorization: Bearer <jwt_token>` (required): JWT authentication token

**Response:** `MessagesListResponse`
```json
{
  "messages": [
    {
      "messageId": "uuid",
      "sessionId": "uuid",
      "sequenceNumber": 0,
      "role": "user|assistant|system",
      "content": [
        {
          "type": "text",
          "text": "message content"
        }
      ],
      "createdAt": "2025-12-03T12:00:00Z",
      "metadata": {}
    }
  ],
  "nextToken": "base64-encoded-token-or-null"
}
```

**Status Codes:**
- `200 OK`: Messages retrieved successfully
- `401 Unauthorized`: Missing or invalid JWT token
- `403 Forbidden`: User doesn't have required roles
- `404 Not Found`: Session not found
- `500 Internal Server Error`: Server error

**Example Usage:**
```bash
# Get first 20 messages
curl -X GET "http://localhost:8000/sessions/{session_id}/messages?limit=20" \
  -H "Authorization: Bearer $JWT_TOKEN"

# Get next page using pagination token
curl -X GET "http://localhost:8000/sessions/{session_id}/messages?limit=20&next_token={next_token}" \
  -H "Authorization: Bearer $JWT_TOKEN"
```

## Testing

### With Authentication Enabled

If you have a valid JWT token:

```bash
# Set your JWT token
export JWT_TOKEN="your_jwt_token_here"

# Get messages for a session with pagination
curl -X GET "http://localhost:8000/sessions/your-session-id/messages?limit=20" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json"

# Get next page using pagination token
curl -X GET "http://localhost:8000/sessions/your-session-id/messages?limit=20&next_token={next_token}" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json"
```

### With Authentication Disabled (Development Only)

For local testing without Entra ID setup, set `ENABLE_AUTHENTICATION=false` in your `.env` file:

```bash
# In backend/src/.env
ENABLE_AUTHENTICATION=false
```

Then test without authentication:

```bash
# Get messages for a session (no auth required)
curl -X GET "http://localhost:8000/sessions/your-session-id/messages?limit=20" \
  -H "Content-Type: application/json"
```

## Architecture

### Storage Modes

The service automatically selects the appropriate storage backend:

1. **AgentCore Memory (Cloud)**: When `AGENTCORE_MEMORY_ID` environment variable is set
   - Uses AWS Bedrock AgentCore Memory service
   - Persistent, multi-session storage
   - Supports user preferences and facts across sessions

2. **Local File Storage (Development)**: When `AGENTCORE_MEMORY_ID` is not set
   - Uses FileSessionManager with local directory structure
   - Stored in `backend/src/sessions/` directory
   - Structure: `sessions/session_{session_id}/agents/agent_default/messages/message_N.json`
   - Each message is stored in a separate numbered JSON file

### User Authentication

- JWT tokens are validated using Entra ID (Azure AD)
- User ID is extracted from the token's `employeenumber` claim
- Users can only access their own messages (enforced by user_id matching)
- Authentication can be disabled for local development (not recommended for production)

### Message Format

Messages are stored in the Strands Agent format:
- `role`: "user" or "assistant"
- `content`: Array of content blocks
  - Text blocks: `{type: "text", text: "content"}`
  - Tool use blocks: `{type: "toolUse", toolUse: {...}}`
  - Tool result blocks: `{type: "toolResult", toolResult: {...}}`
  - Image blocks: `{type: "image", image: {...}}`
  - Document blocks: `{type: "document", document: {...}}`

## Error Handling

The service provides detailed error messages:

- **Configuration errors**: Missing environment variables
- **Not found**: Session doesn't exist
- **Permission errors**: User doesn't have required roles
- **Server errors**: Unexpected errors with logging

All errors are logged with context for debugging.
