# Multimodal File Attachments Implementation

This document describes how file attachments (documents and images) work in the chat flow.

## Overview

Users can attach files to chat messages. Files are uploaded to S3 via pre-signed URLs, and when the user sends a message, the backend fetches file content from S3 and passes it to the AI agent as multimodal content blocks.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              UPLOAD FLOW                                     │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Frontend                      Backend                         AWS          │
│  ────────                      ───────                         ───          │
│                                                                             │
│  1. User drops file            2. POST /files/presign                       │
│     ─────────────────────────────────────────────►                          │
│                                   - Validate file type/size                 │
│                                   - Check user quota                        │
│                                   - Create DynamoDB metadata (PENDING)      │
│                                   - Generate pre-signed PUT URL             │
│                                ◄─────────────────────────────               │
│                                   {uploadId, presignedUrl}                  │
│                                                                             │
│  3. PUT to S3 presigned URL    ─────────────────────────────► S3 Bucket    │
│     (direct browser upload)                                                 │
│                                                                             │
│  4. POST /files/{id}/complete  5. Verify S3 object exists                   │
│     ─────────────────────────────────────────────►                          │
│                                   - Update status to READY                  │
│                                   - Increment user quota                    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                              CHAT FLOW                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Frontend                      Backend                         AWS          │
│  ────────                      ───────                         ───          │
│                                                                             │
│  1. POST /chat/agent-stream    2. FileResolver.resolve_files()              │
│     {message, file_upload_ids}    ─────────────────────────────► S3        │
│     ─────────────────────────►    - Fetch each file from S3                │
│                                   - Base64 encode content                   │
│                                   - Build ResolvedFileContent[]             │
│                                                                             │
│                                3. PromptBuilder.build_prompt()              │
│                                   - Create text block with message          │
│                                   - Add [Attached files: ...] marker        │
│                                   - Create image/document blocks            │
│                                                                             │
│                                4. Agent.stream_async(prompt)                │
│                                   ─────────────────────────────► Bedrock   │
│                                                                             │
│  ◄───────────────────────────  5. Stream SSE events                        │
│     SSE: message_start,                                                     │
│          content_block_delta,                                               │
│          message_stop                                                       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Supported File Types

| Category | MIME Types | Extensions |
|----------|------------|------------|
| Documents | `application/pdf`, `text/plain`, `text/csv`, `text/html`, `text/markdown`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | `.pdf`, `.txt`, `.csv`, `.html`, `.md`, `.docx`, `.xlsx` |
| Images | `image/png`, `image/jpeg`, `image/gif`, `image/webp` | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` |

**Limits:**
- Max file size: 4MB
- Max files per message: 5
- User storage quota: 1GB

## Key Files

### Backend

| File | Purpose |
|------|---------|
| `apis/app_api/files/models.py` | Pydantic models for file metadata, requests, responses |
| `apis/app_api/files/service.py` | Pre-signed URL generation, file management, quota tracking |
| `apis/app_api/files/repository.py` | DynamoDB operations for file metadata |
| `apis/app_api/files/routes.py` | REST endpoints: `/presign`, `/{id}/complete`, `/quota` |
| `apis/app_api/files/file_resolver.py` | Resolves upload IDs to file content from S3 |
| `agents/main_agent/multimodal/prompt_builder.py` | Builds multimodal prompts with text, images, documents |
| `agents/main_agent/multimodal/image_handler.py` | Creates Bedrock image content blocks |
| `agents/main_agent/multimodal/document_handler.py` | Creates Bedrock document content blocks |

### Frontend

| File | Purpose |
|------|---------|
| `services/file-upload/file-upload.service.ts` | Upload flow, quota, pending upload state |
| `session/services/chat/chat-request.service.ts` | Includes `file_upload_ids` in chat requests |
| `session/services/models/message.model.ts` | `FileAttachmentData` interface, `ContentBlock` with fileAttachment |
| `components/file-card/file-card.component.ts` | File card UI during upload (with progress, remove) |
| `session/components/.../file-attachment-badge.component.ts` | Read-only file badge in historical messages |

## Data Models

### DynamoDB Schema (user-files table)

```
PK: USER#{userId}
SK: FILE#{uploadId}
GSI1PK: CONV#{sessionId}   <- SessionIndex for fetching files by session
GSI1SK: FILE#{uploadId}

Attributes:
- uploadId, userId, sessionId
- filename, mimeType, sizeBytes
- s3Key, s3Bucket
- status: pending | ready | failed
- createdAt, updatedAt, ttl
```

### FileAttachmentData (Frontend)

```typescript
interface FileAttachmentData {
  uploadId: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
}
```

### ResolvedFileContent (Backend)

```python
@dataclass
class ResolvedFileContent:
    filename: str
    content_type: str
    bytes: str  # base64-encoded
```

## Session History & File Restoration

When a message with file attachments is sent:
1. The prompt includes a marker: `[Attached files: doc.pdf, image.png]`
2. File metadata is stored in DynamoDB (not in AgentCore Memory)

When loading a historical session:
1. Messages are fetched from AgentCore Memory
2. File metadata is fetched via `GET /files?sessionId={id}` (uses SessionIndex GSI)
3. Frontend enriches messages by matching filenames to metadata
4. File badges are rendered in the UI

## S3 Configuration

The pre-signed URL flow requires:
- **Regional S3 endpoint** (not global) to avoid CORS issues
- **CORS configuration** allowing PUT from frontend origin
- **SigV4 signing** for regional URLs

```python
# In service.py
s3_config = Config(
    signature_version='s3v4',
    s3={'addressing_style': 'virtual'},
)
self._s3_client = boto3.client(
    "s3",
    region_name=region,
    config=s3_config,
    endpoint_url=f"https://s3.{region}.amazonaws.com",
)
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| File not found in S3 | Skip file, log warning, continue with other files |
| File not owned by user | Skip file (security) |
| File not in READY status | Skip file |
| Unsupported file type | Rejected at upload time with 400 |
| Quota exceeded | Rejected at upload time with 403 |

## Common Issues

### CORS Errors on S3 Upload

**Symptom:** 500 on OPTIONS preflight to S3

**Cause:** Pre-signed URL uses global S3 endpoint (`s3.amazonaws.com`) instead of regional (`s3.us-west-2.amazonaws.com`)

**Fix:** Ensure `endpoint_url` is set in S3 client configuration (see S3 Configuration above)

### 307 Redirect on File List

**Symptom:** `GET /files?sessionId=...` returns 307 redirect to `/files/`

**Cause:** Route defined with trailing slash but called without

**Fix:** Define route as `@router.get("")` instead of `@router.get("/")`

### Pydantic Alias Warnings

**Symptom:** `UnsupportedFieldAttributeWarning` for alias fields

**Cause:** Using `alias=` in `Field()` for request models

**Fix:** Use `validation_alias=` for request models (input parsing only)
