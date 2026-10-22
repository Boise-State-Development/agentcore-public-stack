# File Upload Implementation

This document describes the file upload feature for conversations, enabling users to attach documents and images to chat messages.

## Overview

Files are uploaded via pre-signed URLs directly to S3, bypassing the API server for scalability. Metadata is stored in DynamoDB for management and retrieval. The implementation supports both documents (PDF, DOCX, etc.) and images (PNG, JPEG, etc.).

## Constraints

| Constraint | Value |
|------------|-------|
| Max file size | 4 MB |
| Max files per message | 5 |
| User storage quota | 1 GB |
| File retention | 365 days (TTL) |

## Supported File Types

| Category | Extensions | MIME Types |
|----------|------------|------------|
| Documents | `.pdf`, `.docx`, `.txt`, `.html`, `.csv`, `.xls`, `.xlsx`, `.md` | `application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `text/plain`, `text/html`, `text/csv`, `application/vnd.ms-excel`, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `text/markdown` |
| Images | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` | `image/png`, `image/jpeg`, `image/gif`, `image/webp` |

## Upload Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. User drops/selects file                                                  │
│     └─► Frontend validates type & size client-side                          │
│                                                                             │
│  2. POST /files/presign                                                      │
│     └─► Backend: validate quota, create DynamoDB record (PENDING),          │
│         generate pre-signed PUT URL (15 min expiry)                         │
│                                                                             │
│  3. PUT to S3 pre-signed URL                                                │
│     └─► Direct browser-to-S3 upload with progress tracking                  │
│                                                                             │
│  4. POST /files/{uploadId}/complete                                         │
│     └─► Backend: verify S3 object exists, update status to READY,           │
│         increment user quota                                                │
│                                                                             │
│  5. File card shows "ready" state, submit button enabled                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

## API Endpoints

### POST /files/presign

Request a pre-signed URL for uploading.

**Request:**
```json
{
  "sessionId": "uuid",
  "filename": "report.pdf",
  "mimeType": "application/pdf",
  "sizeBytes": 1234567
}
```

**Response:**
```json
{
  "uploadId": "19b7ec1b4d8_7f5b11724a504dd8",
  "presignedUrl": "https://bucket.s3.us-west-2.amazonaws.com/...",
  "expiresAt": "2026-01-01T12:00:00Z"
}
```

**Errors:** `400` (invalid type/size), `403` (quota exceeded)

### POST /files/{uploadId}/complete

Mark upload as complete after S3 upload succeeds.

**Response:**
```json
{
  "uploadId": "19b7ec1b4d8_7f5b11724a504dd8",
  "status": "ready",
  "s3Uri": "s3://bucket/user-files/...",
  "filename": "report.pdf",
  "sizeBytes": 1234567
}
```

**Errors:** `404` (not found), `409` (already completed)

### DELETE /files/{uploadId}

Delete a file (S3 object + DynamoDB metadata + decrement quota).

**Response:** `204 No Content`

### GET /files

List user's files with optional session filter.

**Query params:** `sessionId`, `limit`, `cursor`, `sortBy`, `sortOrder`

**Response:**
```json
{
  "files": [{ "uploadId", "filename", "mimeType", "sizeBytes", "sessionId", "s3Uri", "status", "createdAt" }],
  "nextCursor": "...",
  "totalCount": 42
}
```

### GET /files/quota

Get user's quota usage.

**Response:**
```json
{
  "usedBytes": 524288000,
  "maxBytes": 1073741824,
  "fileCount": 42
}
```

## Storage Architecture

### S3 Structure

```
s3://{bucket}/user-files/{userId}/{sessionId}/{uploadId}/{filename}
```

### S3 Lifecycle Rules

| Age | Storage Class | Rationale |
|-----|---------------|-----------|
| 0-30 days | S3 Standard | Active conversations |
| 31-90 days | S3 Standard-IA | Lower cost |
| 91-365 days | Glacier Instant Retrieval | Rare access |
| >365 days | Delete | TTL expiration |

### DynamoDB Schema

**Table:** `{prefix}-user-files`

| Key | Pattern | Use |
|-----|---------|-----|
| PK | `USER#{userId}` | User's files |
| SK | `FILE#{uploadId}` | Individual file |
| GSI1PK | `CONV#{sessionId}` | SessionIndex |
| GSI1SK | `FILE#{uploadId}` | Files by session |

**Quota Item:**
```
PK: USER#{userId}, SK: QUOTA
{ totalBytes: number, fileCount: number }
```

## Key Files

### Backend

| File | Purpose |
|------|---------|
| `apis/app_api/files/models.py` | Pydantic models, allowed MIME types |
| `apis/app_api/files/service.py` | Pre-sign URLs, quota, file management |
| `apis/app_api/files/repository.py` | DynamoDB operations |
| `apis/app_api/files/routes.py` | REST endpoints |

### Frontend

| File | Purpose |
|------|---------|
| `services/file-upload/file-upload.service.ts` | Upload flow, signals, quota |
| `components/file-card/file-card.component.ts` | File card UI with progress |
| `session/components/chat-input/` | Drag-drop zone, attach button |

### Infrastructure

| File | Purpose |
|------|---------|
| `infrastructure/lib/app-api-stack.ts` | S3 bucket, DynamoDB table, IAM grants |
| `infrastructure/lib/config.ts` | `fileUpload` configuration schema |

## Configuration

### CDK Context (`cdk.context.json`)

```json
{
  "fileUpload": {
    "enabled": true,
    "maxFileSizeBytes": 4194304,
    "maxFilesPerMessage": 5,
    "userQuotaBytes": 1073741824,
    "retentionDays": 365,
    "corsOrigins": "http://localhost:4200,https://boisestate.ai"
  }
}
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `S3_USER_FILES_BUCKET_NAME` | S3 bucket name |
| `DYNAMODB_USER_FILES_TABLE_NAME` | DynamoDB table name |
| `FILE_UPLOAD_MAX_SIZE_BYTES` | Max file size (default 4MB) |
| `FILE_UPLOAD_MAX_FILES_PER_MESSAGE` | Max files per message (default 5). Enforced server-side by the inference API across both `files` and `file_upload_ids`; files beyond the cap are reported to the user in the attachment note, not silently dropped. `0` disables. |
| `FILE_UPLOAD_USER_QUOTA_BYTES` | User quota (default 1GB) |
| `INLINE_DOCUMENT_MAX_BYTES` | Per-file inline document cap (default 4MB); larger non-tabular files are skipped with a note |
| `INLINE_ATTACHMENTS_MAX_TOTAL_BYTES` | Per-turn budget for all inline attachments, images included (default 7.5MB = the 10MB AgentCore Memory event quota ÷ base64's 4/3). Files that would push the turn over it are skipped with a note instead of failing the history write. `0` disables. |
| `ATTACHMENT_TURN_GUARD_ENABLED` | Kill switch for the two caps above (default on; only `false` disables) |

## Quota Management

### Enforcement

1. **Pre-upload check:** Backend validates `currentUsage + fileSize <= quota` before issuing pre-signed URL
2. **Atomic increment:** Quota updated via DynamoDB `ADD` expression on completion
3. **Atomic decrement:** Quota decremented on file deletion

### User Experience

- **80% usage:** Indicator in file browser
- **90% usage:** Warning banner in chat input
- **100% usage:** "Storage Full" state, link to manage files
- **Quota exceeded response:**
  ```json
  {
    "error": "QUOTA_EXCEEDED",
    "currentUsage": 1073741824,
    "maxAllowed": 1073741824,
    "requiredSpace": 4194304
  }
  ```

## S3 CORS Configuration

The S3 bucket requires CORS for browser uploads:

```json
{
  "CORSRules": [{
    "AllowedOrigins": ["http://localhost:4200", "https://boisestate.ai"],
    "AllowedMethods": ["GET", "PUT", "HEAD"],
    "AllowedHeaders": ["Content-Type", "Content-Length", "x-amz-*"],
    "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
    "MaxAgeSeconds": 3600
  }]
}
```

**Important:** Pre-signed URLs must use regional S3 endpoint (`s3.us-west-2.amazonaws.com`) not global (`s3.amazonaws.com`) to avoid CORS issues with redirects.

## Cascade Delete

When a session/conversation is deleted, all associated files are also deleted:

1. Query `SessionIndex` GSI for all files with `GSI1PK = CONV#{sessionId}`
2. Delete each S3 object
3. Delete each DynamoDB metadata record
4. Decrement user quota for each file

See `FileUploadService.delete_session_files()` in `service.py`.

## Security

| Aspect | Implementation |
|--------|----------------|
| Pre-signed URL expiry | 15 minutes |
| Pre-signed URL scope | Single key, PUT only |
| User isolation | `PK = USER#{userId}` enforced on all queries |
| Encryption at rest | S3: SSE-S3, DynamoDB: AWS managed |
| Transport | HTTPS required (`enforceSSL: true`) |
| Public access | Blocked (`blockPublicAccess: BLOCK_ALL`) |

## Error Handling

| Error | HTTP | Response |
|-------|------|----------|
| Invalid file type | 400 | `"Unsupported file type: {type}. Supported: ..."` |
| File too large | 400 | `"File exceeds 4MB limit"` |
| Quota exceeded | 403 | `{ error: "QUOTA_EXCEEDED", currentUsage, maxAllowed, requiredSpace }` |
| File not found | 404 | `"Upload {id} not found or not owned by you"` |
| Already completed | 409 | `"Upload {id} is already ready, cannot complete"` |
| S3 upload failed | 409 | `"S3 object not found for upload {id}"` |
