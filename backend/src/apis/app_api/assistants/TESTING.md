# Assistants API - Local Testing Guide

This guide shows how to test the Assistants CRUD API using local file storage.

## Prerequisites

- Backend server running (`python -m uvicorn apis.app_api.main:app --reload`)
- No `ASSISTANTS_TABLE_NAME` environment variable set (uses local storage by default)
- Valid JWT token for authentication

## Local Storage Location

Assistants are stored in: `backend/src/assistants/assistant_*.json`

## Testing with cURL

### 1. Create a Draft Assistant (for document uploads)

```bash
curl -X POST http://localhost:8000/api/assistants/draft \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My New Assistant"
  }'
```

**Expected Response:**
```json
{
  "assistantId": "AST-abc123def456",
  "ownerId": "user_123",
  "name": "My New Assistant",
  "description": "",
  "instructions": "",
  "vectorIndexId": "idx_assistants",
  "visibility": "PRIVATE",
  "tags": [],
  "usageCount": 0,
  "createdAt": "2025-01-15T10:30:00.000Z",
  "updatedAt": "2025-01-15T10:30:00.000Z",
  "status": "DRAFT"
}
```

**File Created:** `backend/src/assistants/assistant_AST-abc123def456.json`

### 2. Update Assistant (Complete the Draft)

```bash
curl -X PUT http://localhost:8000/api/assistants/AST-abc123def456 \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Biology Tutor",
    "description": "Helps with cell biology questions",
    "instructions": "You are a helpful biology tutor specializing in cell division.",
    "vectorIndexId": "idx_assistants",
    "visibility": "PRIVATE",
    "tags": ["biology", "education"],
    "status": "COMPLETE"
  }'
```

**Expected Response:**
```json
{
  "assistantId": "AST-abc123def456",
  "ownerId": "user_123",
  "name": "Biology Tutor",
  "description": "Helps with cell biology questions",
  "instructions": "You are a helpful biology tutor specializing in cell division.",
  "vectorIndexId": "idx_assistants",
  "visibility": "PRIVATE",
  "tags": ["biology", "education"],
  "usageCount": 0,
  "createdAt": "2025-01-15T10:30:00.000Z",
  "updatedAt": "2025-01-15T10:31:00.000Z",
  "status": "COMPLETE"
}
```

### 3. List Assistants

```bash
# List only COMPLETE assistants (default)
curl http://localhost:8000/api/assistants \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Include drafts
curl "http://localhost:8000/api/assistants?include_drafts=true" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# With pagination
curl "http://localhost:8000/api/assistants?limit=10" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

**Expected Response:**
```json
{
  "assistants": [
    {
      "assistantId": "AST-abc123def456",
      "name": "Biology Tutor",
      "description": "Helps with cell biology questions",
      "status": "COMPLETE",
      ...
    }
  ],
  "nextToken": null
}
```

### 4. Get Single Assistant

```bash
curl http://localhost:8000/api/assistants/AST-abc123def456 \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### 5. Archive Assistant (Soft Delete)

```bash
curl -X POST http://localhost:8000/api/assistants/AST-abc123def456/archive \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

**Result:** Status changes to `ARCHIVED`, file remains on disk but hidden from default listings

### 6. Delete Assistant (Hard Delete)

```bash
curl -X DELETE http://localhost:8000/api/assistants/AST-abc123def456 \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

**Result:** File `assistant_AST-abc123def456.json` is deleted permanently

## Verifying Local Storage

After each operation, you can inspect the files:

```bash
# View all assistant files
ls -la backend/src/assistants/

# View specific assistant
cat backend/src/assistants/assistant_AST-abc123def456.json
```

## Common Test Scenarios

### Scenario 1: Draft → Complete Workflow

1. POST `/assistants/draft` - Create draft
2. Upload documents with `assistantId` from step 1
3. PUT `/assistants/{id}` with `status: COMPLETE` - Finalize assistant

### Scenario 2: List with Filters

```bash
# Only COMPLETE assistants (default)
curl http://localhost:8000/api/assistants \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Include DRAFT assistants
curl "http://localhost:8000/api/assistants?include_drafts=true" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Include ARCHIVED assistants
curl "http://localhost:8000/api/assistants?include_archived=true" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### Scenario 3: Pagination

```bash
# Get first page (10 items)
curl "http://localhost:8000/api/assistants?limit=10" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"

# Use nextToken from response for next page
curl "http://localhost:8000/api/assistants?limit=10&next_token=ENCODED_TOKEN" \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

## Frontend Integration Testing

### Testing from Browser Console

```javascript
// Create draft
const response = await fetch('/api/assistants/draft', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + localStorage.getItem('token')
  },
  body: JSON.stringify({ name: 'Test Assistant' })
});
const draft = await response.json();
console.log('Draft created:', draft);

// Update to complete
const updateResponse = await fetch(`/api/assistants/${draft.assistantId}`, {
  method: 'PUT',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer ' + localStorage.getItem('token')
  },
  body: JSON.stringify({
    name: 'Completed Assistant',
    description: 'Test description',
    instructions: 'Test instructions',
    status: 'COMPLETE'
  })
});
const completed = await updateResponse.json();
console.log('Assistant completed:', completed);
```

## Error Cases to Test

### 404 - Assistant Not Found
```bash
curl http://localhost:8000/api/assistants/INVALID_ID \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

### 401 - Unauthorized
```bash
curl http://localhost:8000/api/assistants
# Missing Authorization header
```

### 404 - Wrong Owner
Try to access another user's assistant - should return 404 (not 403 for security)

## Cleanup

Remove all test assistants:

```bash
rm backend/src/assistants/assistant_*.json
```

## Next Steps

Once local testing is complete:
1. Set `ASSISTANTS_TABLE_NAME` environment variable
2. Implement DynamoDB TODO sections in `assistant_service.py`
3. Test with cloud storage

