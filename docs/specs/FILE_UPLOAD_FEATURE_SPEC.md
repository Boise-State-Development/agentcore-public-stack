# Feature Specification
## File Upload for Conversations
### boisestate.ai Platform

| Field | Value |
|---------|-------------------------------|
| Version | 1.1 Draft                     |
| Date    | January 1, 2026               |
| Author  | Phil / Cloud Architecture Team |
| Status  | Draft — Pending Review        |

---

## 1. Executive Summary

This specification defines a file upload feature for boisestate.ai that enables users to attach Bedrock-compliant files to conversations. The feature leverages pre-signed URLs for secure, scalable uploads to S3, stores metadata in DynamoDB for browsing and management, and implements intelligent storage tiering for cost optimization.

> **Note:** This feature uses "files" terminology to accommodate future expansion to images and other file types beyond documents.

---

## 2. Scope

### 2.1 In Scope

- Document uploads (PDF, DOCX, TXT, HTML, CSV, XLS, XLSX, MD) to conversations
- Pre-signed URL upload flow with progress indication
- S3 storage with intelligent lifecycle tiering
- DynamoDB metadata storage with user browsing capabilities
- Drag-and-drop and file picker upload methods
- User storage quota enforcement (1GB per user)
- File deletion (manual and cascade on conversation delete)

### 2.2 Out of Scope

- Image uploads (future phase)
- Video/audio uploads
- Virus/malware scanning
- Text extraction for RAG/search
- File versioning
- Cross-region replication
- Upload cost tracking in cost accounting system

---

## 3. Functional Requirements

### 3.1 Upload Constraints

| Constraint              | Value                        |
|-------------------------|------------------------------|
| Maximum file size       | 4 MB per file                |
| Maximum files per message | 5 files                    |
| Per-user storage quota  | 1 GB total                   |
| File retention          | 365 days (matches session TTL) |

### 3.2 Supported File Types

Per AWS Bedrock documentation for document content blocks:

| Extension | MIME Type                                      | Notes                   |
|-----------|------------------------------------------------|-------------------------|
| .pdf      | application/pdf                                | Most common document type |
| .docx     | application/vnd.openxmlformats-officedocument.wordprocessingml.document | Microsoft Word |
| .txt      | text/plain                                     | Plain text              |
| .html     | text/html                                      | HTML documents          |
| .csv      | text/csv                                       | Spreadsheet data        |
| .xls      | application/vnd.ms-excel                       | Excel (legacy)          |
| .xlsx     | application/vnd.openxmlformats-officedocument.spreadsheetml.sheet | Excel |
| .md       | text/markdown                                  | Markdown files          |

### 3.3 Upload Behavior

1. Uploads are eager — files upload immediately upon selection/drop
2. Chat submit button is disabled until all pending uploads complete
3. Upload progress indicator displays for each file
4. Failed uploads show error notification (existing error notification component)
5. Users can remove attached files before sending; removal deletes the S3 object
6. Maximum 5 files can be attached per message

---

## 4. Technical Architecture

### 4.1 System Overview

The upload flow uses a two-phase approach: (1) client requests a pre-signed URL from the API, (2) client uploads directly to S3 using the pre-signed URL. This bypasses the API server for large file transfers, enabling horizontal scalability.

### 4.2 Upload Sequence

1. User selects or drops file(s) onto the chat-input component
2. Frontend validates file type and size client-side
3. Frontend calls `POST /api/files/presign` with file metadata
4. Backend validates quota, generates pre-signed URL, creates pending DynamoDB record
5. Frontend uploads file directly to S3 using pre-signed URL
6. Frontend calls `POST /api/files/{uploadId}/complete` on success
7. Backend updates DynamoDB record status to `ready`
8. File appears as attached in chat-input; submit button enables

### 4.3 S3 Configuration

#### 4.3.1 Bucket Structure

```
s3://{bucket}/user-files/{userId}/{sessionId}/{uploadId}/{filename}
```

This structure enables efficient access patterns: list all files for a user, list files for a session, and direct access by upload ID.

#### 4.3.2 Lifecycle Rules

Intelligent tiering optimizes storage costs while maintaining the 365-day retention requirement:

| Age         | Storage Class              | Rationale                              |
|-------------|----------------------------|----------------------------------------|
| 0–30 days   | S3 Standard                | Frequent access during active conversations |
| 31–90 days  | S3 Standard-IA             | Infrequent access, lower storage cost  |
| 91–365 days | S3 Glacier Instant Retrieval | Rare access, significant cost savings |
| >365 days   | Delete                     | Matches session TTL                    |

#### 4.3.3 Bucket Policy

- Block all public access
- Require SSL/TLS for all requests
- Enable server-side encryption (SSE-S3)
- CORS configured for frontend origins

### 4.4 DynamoDB Schema

#### 4.4.1 Table: UserFiles

| Attribute      | Type   | Description                           |
|----------------|--------|---------------------------------------|
| PK             | String | `USER#{userId}`                       |
| SK             | String | `FILE#{uploadId}`                     |
| GSI1PK         | String | `CONV#{sessionId}`                    |
| GSI1SK         | String | `FILE#{uploadId}`                     |
| uploadId       | String | ULID — sortable unique ID             |
| userId         | String | Owning user ID                        |
| sessionId      | String | Associated session/conversation       |
| filename       | String | Original filename                     |
| mimeType       | String | File MIME type                        |
| sizeBytes      | Number | File size in bytes                    |
| s3Key          | String | Full S3 object key                    |
| s3Uri          | String | `s3://{bucket}/{key}` for Bedrock     |
| status         | String | `pending` \| `ready`                  |
| createdAt      | String | ISO 8601 timestamp                    |
| updatedAt      | String | ISO 8601 timestamp                    |
| ttl            | Number | Unix epoch for DynamoDB TTL (365 days) |

#### 4.4.2 Access Patterns

1. **List all files for a user (paginated):** Query `PK = USER#{userId}`
2. **List files for a session:** Query `GSI1PK = CONV#{sessionId}` (SessionIndex)
3. **Get single file:** GetItem `PK = USER#{userId}, SK = FILE#{uploadId}`
4. **Sort by date:** ULID in SK provides natural chronological ordering
5. **Sort by size/type:** Perform in application layer (acceptable for UI pagination)

#### 4.4.3 User Quota Tracking

A separate item tracks aggregate storage per user:

```
PK: USER#{userId}, SK: QUOTA
```

- `totalBytes`: Number — current usage in bytes
- `fileCount`: Number — total files
- Updated atomically via DynamoDB `UpdateExpression` with `ADD`

### 4.5 Bedrock Integration

Files are passed to Bedrock using S3 URIs, which is supported by most models and avoids base64 encoding overhead:

```json
{
  "document": {
    "format": "pdf",
    "name": "report.pdf",
    "source": {
      "s3Location": {
        "uri": "s3://bucket/key"
      }
    }
  }
}
```

The API layer retrieves the `s3Uri` from DynamoDB and constructs the content block. IAM permissions must allow Bedrock to read from the S3 bucket.

> **Note:** Verify S3 URI support for each model in use. Some models may require base64 encoding — implement a fallback if needed.

---

## 5. API Specification

### 5.1 POST /api/files/presign

Request a pre-signed URL for uploading a file.

**Request Body:**
```json
{
  "sessionId": "string",
  "filename": "string",
  "mimeType": "string",
  "sizeBytes": 12345
}
```

**Response (200 OK):**
```json
{
  "uploadId": "string",
  "presignedUrl": "string",
  "expiresAt": "ISO8601"
}
```

**Error Responses:**
- `400 Bad Request` — Invalid file type or size exceeds 4MB
- `403 Forbidden` — User quota exceeded
- `429 Too Many Requests` — Rate limit exceeded (if implemented)

### 5.2 POST /api/files/{uploadId}/complete

Mark an upload as complete after successful S3 upload.

**Response (200 OK):**
```json
{
  "uploadId": "string",
  "status": "ready",
  "s3Uri": "string"
}
```

**Error Responses:**
- `404 Not Found` — Upload ID not found or not owned by user
- `409 Conflict` — Upload already completed or deleted

### 5.3 DELETE /api/files/{uploadId}

Delete a file. Used when user removes an attached file before sending, or manually deletes from file browser.

**Response (204 No Content):** Success — no body returned.

**Side Effects:**
- Deletes S3 object
- Deletes DynamoDB record
- Decrements user quota

### 5.4 GET /api/files

List files for the authenticated user.

**Query Parameters:**
- `sessionId` (optional) — filter by session/conversation
- `limit` (optional, default 20, max 100) — page size
- `cursor` (optional) — pagination cursor
- `sortBy` (optional) — `date` (default), `size`, `type`
- `sortOrder` (optional) — `asc`, `desc` (default)

**Response (200 OK):**
```json
{
  "files": [...],
  "nextCursor": "string | null",
  "totalCount": 123
}
```

### 5.5 GET /api/files/quota

Get current quota usage for the authenticated user.

**Response (200 OK):**
```json
{
  "usedBytes": 524288000,
  "maxBytes": 1073741824,
  "fileCount": 42
}
```

---

## 6. Frontend Specification

### 6.1 Chat Input Component Updates

1. Add drop zone overlay that appears on dragover with visual feedback (border highlight, icon change)
2. Extend existing attach button to support file types
3. Display attached files as cards above the text input (per Claude.ai reference screenshots)
4. File cards show: filename (truncated), file type badge, line count or size, remove (X) button
5. Disable submit button while any upload is in `pending` state
6. Show upload progress bar on each file card during upload

### 6.2 File Card Component

Based on the Claude.ai reference screenshots, each attached file displays as a card with:

- Filename (truncated with ellipsis if long)
- Metadata line (e.g., "83 lines")
- File type badge (e.g., "DOCX", "MD", "PDF")
- For PDFs: thumbnail preview of first page *(future enhancement — comment in code)*
- Hover state with remove (X) button

### 6.3 Conversation Message Display

When a message includes files (after sending):

- Display file cards inline with the user message (right-aligned)
- Cards are non-interactive (no remove button) once message is sent
- Clicking a card could open a preview modal *(future enhancement)*

### 6.4 Error Handling

- **Invalid file type:** Toast notification — "Unsupported file type. Supported: PDF, DOCX, TXT, HTML, CSV, XLS, XLSX, MD"
- **File too large:** Toast notification — "File exceeds 4MB limit"
- **Quota exceeded:** Toast notification — "Storage quota exceeded. Delete some files to upload more."
- **Upload failed:** Toast notification — "Upload failed. Please try again." (with retry option)
- **Too many files:** Toast notification — "Maximum 5 files per message"

---

## 7. Security Considerations

### 7.1 Pre-signed URL Security

1. URLs expire after 15 minutes (sufficient for 4MB upload even on slow connections)
2. URLs are single-use via S3 condition on ETag (prevents replay)
3. URLs are scoped to specific S3 key (user cannot upload to arbitrary paths)
4. PUT-only permission (no GET, DELETE, or LIST via pre-signed URL)

### 7.2 Content Validation

- **MIME type validation:** Check Content-Type header matches expected type
- **Extension validation:** Verify file extension matches MIME type
- **Magic bytes validation:** *(Optional, adds complexity)* Inspect first bytes to confirm file type

> **Recommendation:** Implement MIME type and extension validation. Magic bytes validation adds meaningful security but increases complexity — consider for v2 if abuse is observed.

### 7.3 Access Control

- All API endpoints require authentication via existing auth middleware
- Users can only access their own files (enforced via `PK = USER#{userId}`)
- Bedrock IAM role has read-only access to the files bucket
- No direct S3 access for users — all access mediated through API

### 7.4 Data at Rest

- **S3:** Server-side encryption enabled (SSE-S3)
- **DynamoDB:** Encryption at rest enabled (AWS managed key)

---

## 8. Cost Analysis

### 8.1 S3 Storage Costs (us-west-2, estimated)

Assuming 27,000 users, 10% active monthly, average 50MB storage per active user:

| Storage Class                | Rate       | Est. Monthly |
|------------------------------|------------|--------------|
| S3 Standard (0–30 days)      | $0.023/GB  | ~$31         |
| S3 Standard-IA (31–90 days)  | $0.0125/GB | ~$17         |
| Glacier Instant (91–365 days)| $0.004/GB  | ~$5          |

**Total estimated S3 cost:** ~$50–100/month at scale, with intelligent tiering providing ~60% savings over Standard-only.

### 8.2 DynamoDB Costs

On-demand pricing recommended for unpredictable workloads:

- Write: $1.25 per million writes
- Read: $0.25 per million reads
- Storage: $0.25/GB/month

**Estimated:** <$20/month for metadata storage and operations.

---

## 9. Quota Enforcement

### 9.1 Quota Check Flow

When a user attempts to upload a file, the system performs a quota check before generating a pre-signed URL:

1. Frontend sends upload request with file size (`sizeBytes`)
2. Backend reads current quota from DynamoDB (`PK: USER#{userId}, SK: QUOTA`)
3. Backend calculates: `currentUsage + newFileSize`
4. If projected usage > 1GB (1,073,741,824 bytes), reject with `403 Forbidden`
5. If within quota, proceed with pre-signed URL generation

### 9.2 User Experience When Quota Exceeded

When a user reaches their 1GB limit, they receive clear feedback and actionable options:

#### 9.2.1 Upload Rejection

- API returns `403` with body:
  ```json
  {
    "error": "QUOTA_EXCEEDED",
    "message": "Storage quota exceeded",
    "currentUsage": 1073741824,
    "maxAllowed": 1073741824,
    "requiredSpace": 4194304
  }
  ```
- Frontend displays toast: "Storage quota exceeded. You're using X of 1GB. Free up Y to upload this file."
- Toast includes action button: **"Manage Files"** — links to file browser

#### 9.2.2 Proactive Quota Warnings

- **At 80% usage (800MB):** Subtle indicator in file browser showing quota status
- **At 90% usage (900MB):** Warning banner in chat input area when attaching files
- **At 100%:** Upload button shows "Storage Full" state with link to manage files

#### 9.2.3 File Browser for Quota Management

Users can manage their storage through a file browser interface (accessible from profile/settings):

- Display quota usage bar (used / 1GB) prominently at top
- List all uploaded files with size, date, conversation link
- Support multi-select for bulk deletion
- Sort by size (largest first) to help users identify space-saving opportunities
- Filter by date range to find old files for cleanup

### 9.3 Quota Accounting

#### 9.3.1 Incrementing Quota

Quota is incremented when upload completes (`POST /api/files/{uploadId}/complete`):

```
UpdateExpression: ADD totalBytes :size, fileCount :one
```

This atomic operation ensures accurate counting even under concurrent uploads.

#### 9.3.2 Decrementing Quota

Quota is decremented when files are deleted:

- Manual deletion via `DELETE /api/files/{uploadId}`
- Cascade deletion when conversation is deleted
- TTL expiration (DynamoDB Stream triggers Lambda to decrement quota)

```
UpdateExpression: ADD totalBytes :negativeSize, fileCount :negativeOne
```

#### 9.3.3 Quota Reconciliation

A scheduled Lambda (weekly) reconciles quota by scanning actual S3 usage:

- Lists all objects for each user prefix
- Sums actual bytes stored
- Updates quota record if discrepancy found (with CloudWatch alarm on drift)

This handles edge cases like failed delete operations or missed stream events.

---

## 10. CDK Infrastructure

### 10.1 Stack Overview

The file upload feature is integrated into the **AppApiStack** (not a separate stack) to simplify deployment and IAM grant management. The following resources are added:

- S3 Bucket (`user-files`) with lifecycle rules and encryption
- DynamoDB Table (`user-files`) with GSI and TTL
- IAM grants for ECS task role
- Environment variables for container configuration

### 10.2 Configuration

Add to `cdk.context.json`:

```json
{
  "fileUpload": {
    "enabled": true,
    "maxFileSizeBytes": 4194304,
    "maxFilesPerMessage": 5,
    "userQuotaBytes": 1073741824,
    "retentionDays": 365
  }
}
```

Environment variable overrides:
- `CDK_FILE_UPLOAD_ENABLED`
- `CDK_FILE_UPLOAD_MAX_FILE_SIZE`
- `CDK_FILE_UPLOAD_MAX_FILES_PER_MESSAGE`
- `CDK_FILE_UPLOAD_USER_QUOTA`
- `CDK_FILE_UPLOAD_RETENTION_DAYS`
- `CDK_FILE_UPLOAD_CORS_ORIGINS`

### 10.3 S3 Bucket Configuration

```typescript
const userFilesBucket = new s3.Bucket(this, 'UserFilesBucket', {
  bucketName: getResourceName(config, 'user-files', config.awsAccount),
  encryption: s3.BucketEncryption.S3_MANAGED,
  blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
  enforceSSL: true,
  versioned: false,
  removalPolicy: config.environment === 'prod'
    ? cdk.RemovalPolicy.RETAIN
    : cdk.RemovalPolicy.DESTROY,
  cors: [{
    allowedOrigins: fileUploadCorsOrigins,
    allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.HEAD],
    allowedHeaders: ['Content-Type', 'Content-Length', 'x-amz-*'],
    exposedHeaders: ['ETag'],
    maxAge: 3600,
  }],
  lifecycleRules: [
    { id: 'transition-to-ia', transitions: [{ storageClass: s3.StorageClass.INFREQUENT_ACCESS, transitionAfter: cdk.Duration.days(30) }] },
    { id: 'transition-to-glacier', transitions: [{ storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL, transitionAfter: cdk.Duration.days(90) }] },
    { id: 'expire-objects', expiration: cdk.Duration.days(config.fileUpload?.retentionDays || 365) },
    { id: 'abort-incomplete-multipart', abortIncompleteMultipartUploadAfter: cdk.Duration.days(1) },
  ],
});
```

### 10.4 DynamoDB Table Configuration

```typescript
const userFilesTable = new dynamodb.Table(this, 'UserFilesTable', {
  tableName: getResourceName(config, 'user-files'),
  partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  pointInTimeRecovery: true,
  timeToLiveAttribute: 'ttl',
  stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
  encryption: dynamodb.TableEncryption.AWS_MANAGED,
  removalPolicy: config.environment === 'prod'
    ? cdk.RemovalPolicy.RETAIN
    : cdk.RemovalPolicy.DESTROY,
});

// GSI: SessionIndex - Query files by session
userFilesTable.addGlobalSecondaryIndex({
  indexName: 'SessionIndex',
  partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
  projectionType: dynamodb.ProjectionType.ALL,
});
```

### 10.5 Container Environment Variables

The ECS container receives the following environment variables:

| Variable | Description |
|----------|-------------|
| `DYNAMODB_USER_FILES_TABLE_NAME` | DynamoDB table name for file metadata |
| `S3_USER_FILES_BUCKET_NAME` | S3 bucket name for file storage |
| `FILE_UPLOAD_MAX_SIZE_BYTES` | Maximum file size in bytes |
| `FILE_UPLOAD_MAX_FILES_PER_MESSAGE` | Maximum files per message |
| `FILE_UPLOAD_USER_QUOTA_BYTES` | Per-user storage quota in bytes |

### 10.6 IAM Permissions

Granted automatically via CDK:

```typescript
userFilesTable.grantReadWriteData(taskDefinition.taskRole);
userFilesBucket.grantReadWrite(taskDefinition.taskRole);
```

### 10.7 SSM Parameters

Exported for cross-stack reference:

| Parameter | Value |
|-----------|-------|
| `/{projectPrefix}/file-upload/bucket-name` | S3 bucket name |
| `/{projectPrefix}/file-upload/bucket-arn` | S3 bucket ARN |
| `/{projectPrefix}/file-upload/table-name` | DynamoDB table name |
| `/{projectPrefix}/file-upload/table-arn` | DynamoDB table ARN |

---

## 11. Implementation Phases

### Phase 1: Core Upload Flow (MVP)

- [x] S3 bucket with lifecycle rules (in AppApiStack)
- [x] DynamoDB table and GSI (in AppApiStack)
- [ ] Pre-sign and complete API endpoints
- [ ] Frontend drag-and-drop and attach button
- [ ] File cards in chat input
- [ ] Basic validation (type, size)

### Phase 2: Management & Polish

- [ ] User quota tracking and enforcement
- [ ] Delete endpoint and cascade delete on conversation delete
- [ ] File browser UI for listing/sorting files
- [ ] Upload progress indicators

### Phase 3: Future Enhancements (Out of Scope)

- PDF thumbnail previews
- Image upload support
- Magic bytes validation
- Text extraction for search/RAG
- File preview modal

---

## 12. Appendix

### 12.1 Bedrock Document Block Reference

From AWS Bedrock documentation, document content blocks support:

- PDF, CSV, DOC, DOCX, XLS, XLSX, HTML, TXT, MD
- Maximum 4.5MB per document (we use 4MB for safety margin)
- Maximum 5 documents per request
- S3 URI format: `s3://bucket-name/object-key`

### 12.2 IAM Policy (Bedrock Access to S3)

```json
{
  "Effect": "Allow",
  "Action": ["s3:GetObject"],
  "Resource": "arn:aws:s3:::{bucket}/user-files/*"
}
```

### 12.3 Open Questions

*No open questions at this time.*

---

## 13. Sign-off

| Role             | Name | Date |
|------------------|------|------|
| Author           |      |      |
| Technical Review |      |      |
| Product Owner    |      |      |
