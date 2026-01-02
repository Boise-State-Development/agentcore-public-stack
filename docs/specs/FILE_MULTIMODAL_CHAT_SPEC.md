# Implementation Plan: Multimodal Document/Image Support in Chat Flow

## Overview

This plan details the implementation of multimodal support (documents and images) for the chat flow, enabling users to attach files to messages and have the AI agent analyze them.

## Current State Analysis

### What Already Exists

| Component | Status | Location |
|-----------|--------|----------|
| File Upload Service (Frontend) | Complete | `frontend/.../services/file-upload/file-upload.service.ts` |
| Pre-signed URL Flow (Backend) | Complete | `backend/.../apis/app_api/files/` |
| S3 Storage & DynamoDB Metadata | Complete | `backend/.../apis/app_api/files/repository.py` |
| Multimodal Prompt Builder | Complete | `backend/.../agents/strands_agent/multimodal/prompt_builder.py` |
| Image/Document Handlers | Complete | `backend/.../agents/strands_agent/multimodal/` |
| Chat Input UI (Drag/Drop) | Complete | `frontend/.../session/components/chat-input/` |
| `InvocationRequest.files` field | Exists | `backend/.../apis/inference_api/chat/models.py` |

### Missing Integration Points

1. **Backend**: Chat endpoints don't fetch file content from S3 using `file_upload_ids`
2. **Frontend**: `file_upload_ids` are passed to backend but not processed
3. **Backend**: `/invocations` endpoint passes `files` to agent but expects base64-encoded `FileContent`, not upload IDs
4. **Images**: File upload service only allows documents (PDF, DOCX, etc.) - images not supported
5. **Session Load**: File metadata not restored when loading historical sessions (solved by Step 11)

---

## Implementation Steps

### Step 1: Extend Allowed File Types for Images

**Goal**: Allow image uploads (PNG, JPEG, GIF, WebP) in addition to documents.

**Files to Modify**:

1. **Backend**: `backend/src/apis/app_api/files/models.py`
   - Add image MIME types to `ALLOWED_MIME_TYPES`:
     ```python
     ALLOWED_MIME_TYPES = {
         # Existing documents...
         # Add images:
         "image/png": "png",
         "image/jpeg": "jpeg",
         "image/gif": "gif",
         "image/webp": "webp",
     }
     ```
   - Add image extensions to `ALLOWED_EXTENSIONS`:
     ```python
     ALLOWED_EXTENSIONS = {
         # Existing...
         ".png": "image/png",
         ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg",
         ".gif": "image/gif",
         ".webp": "image/webp",
     }
     ```

2. **Frontend**: `frontend/ai.client/src/app/services/file-upload/file-upload.service.ts`
   - Add image MIME types to `ALLOWED_MIME_TYPES`
   - Add image extensions to `ALLOWED_EXTENSIONS`

---

### Step 2: Create File Resolver Service (Backend)

**Goal**: Fetch file content from S3 given upload IDs and convert to `FileContent` objects.

**New File**: `backend/src/apis/app_api/files/file_resolver.py`

```python
"""
File Resolver Service

Resolves file upload IDs to FileContent objects with base64-encoded bytes.
Used by chat endpoints to fetch files from S3 before passing to agent.
"""

import base64
import logging
from typing import List, Optional

import boto3
from botocore.exceptions import ClientError

from apis.inference_api.chat.models import FileContent
from apis.app_api.files.service import get_file_upload_service
from apis.app_api.files.models import FileStatus

logger = logging.getLogger(__name__)


class FileResolverError(Exception):
    """Error resolving file content."""
    pass


class FileResolver:
    """
    Resolves file upload IDs to FileContent objects.

    Fetches file metadata from DynamoDB and content from S3,
    then encodes as base64 for the agent.
    """

    def __init__(self, s3_client=None):
        self._s3_client = s3_client or boto3.client("s3")
        self._file_service = get_file_upload_service()

    async def resolve_files(
        self,
        user_id: str,
        upload_ids: List[str],
        max_files: int = 5
    ) -> List[FileContent]:
        """
        Resolve upload IDs to FileContent objects.

        Args:
            user_id: Owner user ID (for authorization)
            upload_ids: List of upload IDs to resolve
            max_files: Maximum files to process (Bedrock limit is 5)

        Returns:
            List of FileContent objects with base64-encoded bytes

        Raises:
            FileResolverError: If file not found or access denied
        """
        resolved_files = []

        for upload_id in upload_ids[:max_files]:
            try:
                file_content = await self._resolve_single_file(user_id, upload_id)
                if file_content:
                    resolved_files.append(file_content)
            except Exception as e:
                logger.warning(f"Failed to resolve file {upload_id}: {e}")
                # Continue with other files rather than failing entirely
                continue

        return resolved_files

    async def _resolve_single_file(
        self,
        user_id: str,
        upload_id: str
    ) -> Optional[FileContent]:
        """Resolve a single file upload ID."""

        # Get file metadata
        file_meta = await self._file_service.get_file(user_id, upload_id)

        if not file_meta:
            logger.warning(f"File {upload_id} not found for user {user_id}")
            return None

        if file_meta.status != FileStatus.READY:
            logger.warning(f"File {upload_id} not ready: {file_meta.status}")
            return None

        # Fetch content from S3
        try:
            response = self._s3_client.get_object(
                Bucket=file_meta.s3_bucket,
                Key=file_meta.s3_key
            )
            file_bytes = response["Body"].read()
        except ClientError as e:
            logger.error(f"Failed to fetch file {upload_id} from S3: {e}")
            return None

        # Encode as base64
        base64_content = base64.b64encode(file_bytes).decode("utf-8")

        return FileContent(
            filename=file_meta.filename,
            content_type=file_meta.mime_type,
            bytes=base64_content
        )


# Global instance
_resolver_instance: Optional[FileResolver] = None


def get_file_resolver() -> FileResolver:
    """Get or create global FileResolver instance."""
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = FileResolver()
    return _resolver_instance
```

---

### Step 3: Update Chat Request Models

**Goal**: Add `file_upload_ids` field to request models.

**File**: `backend/src/apis/inference_api/chat/models.py`

```python
class InvocationRequest(BaseModel):
    """Input for /invocations endpoint with multi-provider support"""
    session_id: str
    message: str
    model_id: Optional[str] = None
    temperature: Optional[float] = None
    system_prompt: Optional[str] = None
    caching_enabled: Optional[bool] = None
    enabled_tools: Optional[List[str]] = None
    files: Optional[List[FileContent]] = None  # Direct file content (existing)
    file_upload_ids: Optional[List[str]] = None  # NEW: Upload IDs to resolve from S3
    provider: Optional[str] = None
    max_tokens: Optional[int] = None
```

---

### Step 4: Integrate File Resolution into `/invocations` Endpoint

**Goal**: Resolve `file_upload_ids` to `FileContent` objects before passing to agent.

**File**: `backend/src/apis/inference_api/chat/routes.py`

**Changes to `invocations()` function**:

```python
from apis.app_api.files.file_resolver import get_file_resolver

@router.post("/invocations")
async def invocations(
    request: InvocationRequest,
    current_user: User = Depends(get_current_user)
):
    # ... existing code ...

    # Resolve file upload IDs to FileContent objects
    files_to_send = request.files or []

    if request.file_upload_ids:
        logger.info(f"Resolving {len(request.file_upload_ids)} file upload IDs")
        file_resolver = get_file_resolver()
        resolved_files = await file_resolver.resolve_files(
            user_id=user_id,
            upload_ids=request.file_upload_ids,
            max_files=5  # Bedrock document limit
        )
        files_to_send.extend(resolved_files)
        logger.info(f"Resolved {len(resolved_files)} files from upload IDs")

    # ... existing agent creation code ...

    # Pass resolved files to agent stream
    async for event in agent.stream_async(
        input_data.message,
        session_id=input_data.session_id,
        files=files_to_send if files_to_send else None  # Use resolved files
    ):
        yield event
```

---

### Step 5: Update `/chat/stream` Endpoint

**Goal**: Add file resolution to the legacy `/chat/stream` endpoint.

**File**: `backend/src/apis/app_api/chat/routes.py`

**Changes**:
1. Add `file_upload_ids` to `ChatRequest` model import handling
2. Resolve files before calling agent

```python
from apis.app_api.files.file_resolver import get_file_resolver

@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    current_user: User = Depends(get_current_user)
):
    # ... existing RBAC and quota code ...

    # Resolve file upload IDs to FileContent objects
    files_to_send = request.files or []

    if hasattr(request, 'file_upload_ids') and request.file_upload_ids:
        logger.info(f"Resolving {len(request.file_upload_ids)} file upload IDs")
        file_resolver = get_file_resolver()
        resolved_files = await file_resolver.resolve_files(
            user_id=user_id,
            upload_ids=request.file_upload_ids,
            max_files=5
        )
        files_to_send.extend(resolved_files)
        logger.info(f"Resolved {len(resolved_files)} files")

    # ... existing agent creation ...

    # Update stream call to pass files
    stream_iterator = agent.stream_async(
        request.message,
        session_id=request.session_id,
        files=files_to_send if files_to_send else None
    )
```

Also update `ChatRequest` model:

```python
class ChatRequest(BaseModel):
    """Chat request from client"""
    session_id: str
    message: str
    files: Optional[List[FileContent]] = None
    file_upload_ids: Optional[List[str]] = None  # NEW
    enabled_tools: Optional[List[str]] = None
```

---

### Step 6: Store Multimodal Content in Session History

**Goal**: Persist user messages with file attachments to session history.

**File**: `backend/src/agents/strands_agent/session/turn_based_session_manager.py`

**Changes**:
- When building user message for session, include document/image references
- Format: Include file names in a structured way that can be reconstructed

```python
def _build_user_message_with_files(
    self,
    message: str,
    files: Optional[List[FileContent]]
) -> dict:
    """Build user message content blocks including file references."""
    content = [{"text": message}]

    if files:
        # Add file reference markers (actual content handled by agent)
        file_names = [f.filename for f in files]
        content.append({
            "text": f"\n[Attached files: {', '.join(file_names)}]"
        })

    return {
        "role": "user",
        "content": content
    }
```

---

### Step 7: Update Frontend Request Building

**Goal**: Ensure frontend sends `file_upload_ids` in the correct format.

**File**: `frontend/ai.client/src/app/session/services/chat/chat-request.service.ts`

**Current Implementation** (already correct):
```typescript
// Add file upload IDs if present
if (fileUploadIds && fileUploadIds.length > 0) {
  requestObject['file_upload_ids'] = fileUploadIds;
}
```

**Verification needed**: Confirm the field name matches backend expectation (`file_upload_ids`).

---

### Step 8: Handle Multimodal Content in Stream Response

**Goal**: Ensure image/document responses from agent are properly streamed.

**Files to verify**:
1. `backend/src/agents/strands_agent/streaming/stream_processor.py` - already handles tool results with images
2. `frontend/ai.client/src/app/session/services/chat/stream-parser.service.ts` - verify image block parsing

The streaming infrastructure should already support multimodal responses via tool results (e.g., Code Interpreter returning charts). Verify no additional changes needed.

---

### Step 9: Add Image Preview in Chat Input

**Goal**: Show image previews (thumbnails) for uploaded images.

**File**: `frontend/ai.client/src/app/components/file-card/file-card.component.ts`

**Changes**:
- Detect if file is an image based on MIME type
- Show thumbnail preview instead of document icon for images
- Use FileReader to create data URL for preview

```typescript
// Add to component
readonly isImage = computed(() => {
  const type = this.pendingUpload()?.file.type || '';
  return type.startsWith('image/');
});

readonly imagePreviewUrl = signal<string | null>(null);

ngOnInit() {
  if (this.isImage()) {
    const reader = new FileReader();
    reader.onload = (e) => {
      this.imagePreviewUrl.set(e.target?.result as string);
    };
    reader.readAsDataURL(this.pendingUpload()!.file);
  }
}
```

---

### Step 10: Display Attached Files in Chat Messages

**Goal**: Show file attachments in rendered chat messages.

**File**: `frontend/ai.client/src/app/session/components/message/` (or appropriate message component)

**Changes**:
- Parse message content for file references
- Display file chips/badges below message text
- For images, optionally show inline preview

---

### Step 11: File Metadata Restoration on Session Load

**Goal**: Restore file attachment metadata when loading historical sessions.

#### Problem Statement

When a user sends a message with file attachments:
1. Frontend has `FileAttachmentData` in memory (`uploadId`, `filename`, `mimeType`, `sizeBytes`)
2. Backend resolves `file_upload_ids` to file content and sends to Bedrock
3. AgentCore Memory stores the message text but **not** the file metadata

When the user reloads the page or navigates back to a session:
1. Messages are loaded from AgentCore Memory
2. File metadata is **lost** - only textual references like `[Attached files: document.pdf]` remain
3. UI cannot display proper file chips/badges without metadata

#### Solution: Fetch File Metadata from DynamoDB SessionIndex GSI

The `user-files` DynamoDB table already has a `SessionIndex` GSI (`GSI1PK=CONV#{sessionId}`) that allows fetching all files for a session. When loading a session, the frontend fetches file metadata in parallel with messages.

#### Backend Changes

**File**: `backend/src/apis/app_api/files/routes.py`

The endpoint already exists: `GET /files?sessionId={sessionId}` using `list_session_files()`.

Verify it returns the necessary fields:
```python
@router.get("/", response_model=FileListResponse)
async def list_files(
    session_id: Optional[str] = Query(None, alias="sessionId"),
    current_user: User = Depends(get_current_user)
):
    """
    List files for the current user.

    If sessionId is provided, returns files for that session only.
    Otherwise returns all user files with pagination.
    """
    user_id = current_user.user_id

    if session_id:
        # Use SessionIndex GSI - no user_id check needed as files are
        # already scoped to user via ownership
        files = await file_service.list_session_files(session_id)
        # Filter to only this user's files (security check)
        files = [f for f in files if f.user_id == user_id]
        return FileListResponse(
            files=[FileResponse.from_metadata(f) for f in files]
        )

    # ... existing pagination logic for all user files ...
```

#### Frontend Changes

**File**: `frontend/ai.client/src/app/services/file-upload/file-upload.service.ts`

Add method to fetch session files:
```typescript
/**
 * Fetch file metadata for a session.
 * Used to restore file attachment data when loading historical sessions.
 */
async getSessionFiles(sessionId: string): Promise<FileAttachmentData[]> {
  const response = await firstValueFrom(
    this.http.get<FileListResponse>(`${this.apiUrl}/files`, {
      params: { sessionId }
    })
  );

  return response.files.map(f => ({
    uploadId: f.uploadId,
    filename: f.filename,
    mimeType: f.mimeType,
    sizeBytes: f.sizeBytes
  }));
}
```

**File**: `frontend/ai.client/src/app/session/services/session/message-map.service.ts`

Update `loadMessagesForSession()` to fetch and merge file metadata:

```typescript
async loadMessagesForSession(sessionId: string): Promise<void> {
  // Check if messages already exist
  const existingMessages = this.messageMap()[sessionId];
  if (existingMessages && existingMessages().length > 0) {
    return;
  }

  this._isLoadingSession.set(sessionId);

  try {
    // Fetch messages and file metadata in parallel
    const [messagesResponse, sessionFiles] = await Promise.all([
      this.sessionService.getMessages(sessionId),
      this.fileUploadService.getSessionFiles(sessionId)
    ]);

    // Create a lookup map: uploadId -> FileAttachmentData
    const fileMetadataMap = new Map<string, FileAttachmentData>();
    for (const file of sessionFiles) {
      fileMetadataMap.set(file.uploadId, file);
    }

    // Process messages and enrich file attachments with metadata
    const processedMessages = this.matchToolResultsToToolUses(messagesResponse.messages);
    const enrichedMessages = this.enrichFileAttachments(processedMessages, fileMetadataMap);

    // Update the message map
    this.messageMap.update(map => {
      const updated = { ...map };
      if (!updated[sessionId]) {
        updated[sessionId] = signal(enrichedMessages);
      } else {
        updated[sessionId].set(enrichedMessages);
      }
      return updated;
    });
  } catch (error) {
    console.error('Failed to load messages for session:', sessionId, error);
    throw error;
  } finally {
    this._isLoadingSession.set(null);
  }
}

/**
 * Enrich file attachment content blocks with metadata from DynamoDB.
 *
 * User messages may contain fileAttachment blocks with only uploadId.
 * This method populates the full metadata (filename, mimeType, sizeBytes).
 */
private enrichFileAttachments(
  messages: Message[],
  fileMetadataMap: Map<string, FileAttachmentData>
): Message[] {
  return messages.map(message => {
    if (message.role !== 'user') {
      return message;
    }

    const enrichedContent = message.content.map(block => {
      if (block.type === 'fileAttachment' && block.fileAttachment) {
        const uploadId = block.fileAttachment.uploadId;
        const fullMetadata = fileMetadataMap.get(uploadId);

        if (fullMetadata) {
          return {
            ...block,
            fileAttachment: fullMetadata
          };
        }
      }
      return block;
    });

    return {
      ...message,
      content: enrichedContent
    };
  });
}
```

#### Alternative: Parse Text References

If AgentCore Memory doesn't store `fileAttachment` content blocks, messages may only contain text like `[Attached files: report.pdf, image.png]`. In this case:

1. Parse the text reference to extract filenames
2. Match filenames to session files from DynamoDB
3. Reconstruct `fileAttachment` content blocks

```typescript
/**
 * Parse file references from message text and create fileAttachment blocks.
 * Handles legacy messages that only have text references.
 */
private parseFileReferencesFromText(
  messages: Message[],
  fileMetadataMap: Map<string, FileAttachmentData>
): Message[] {
  // Create filename -> metadata lookup
  const filenameMap = new Map<string, FileAttachmentData>();
  for (const file of fileMetadataMap.values()) {
    filenameMap.set(file.filename, file);
  }

  return messages.map(message => {
    if (message.role !== 'user') {
      return message;
    }

    const newContent: ContentBlock[] = [];

    for (const block of message.content) {
      if (block.type === 'text' && block.text) {
        // Check for file reference pattern
        const match = block.text.match(/\[Attached files?: (.+)\]/);

        if (match) {
          // Extract just the main text (before the file reference)
          const mainText = block.text.replace(/\n?\[Attached files?: .+\]/, '').trim();
          if (mainText) {
            newContent.push({ type: 'text', text: mainText });
          }

          // Parse filenames and create fileAttachment blocks
          const filenames = match[1].split(',').map(f => f.trim());
          for (const filename of filenames) {
            const metadata = filenameMap.get(filename);
            if (metadata) {
              newContent.push({
                type: 'fileAttachment',
                fileAttachment: metadata
              });
            }
          }
        } else {
          newContent.push(block);
        }
      } else {
        newContent.push(block);
      }
    }

    return {
      ...message,
      content: newContent.length > 0 ? newContent : message.content
    };
  });
}
```

#### Data Flow Summary

```
Session Load Flow:
┌─────────────────────────────────────────────────────────────────┐
│  User navigates to /s/{sessionId}                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  MessageMapService.loadMessagesForSession(sessionId)            │
│                                                                 │
│  ┌──────────────────────┐    ┌──────────────────────┐          │
│  │ GET /sessions/{id}/  │    │ GET /files?sessionId │          │
│  │     messages         │    │     ={sessionId}     │          │
│  │                      │    │                      │          │
│  │ (AgentCore Memory)   │    │ (DynamoDB GSI)       │          │
│  └──────────┬───────────┘    └──────────┬───────────┘          │
│             │                           │                       │
│             │      Promise.all()        │                       │
│             └───────────┬───────────────┘                       │
│                         │                                       │
│                         ▼                                       │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ enrichFileAttachments(messages, fileMetadataMap)         │  │
│  │                                                          │  │
│  │ - Match uploadId in fileAttachment blocks                │  │
│  │ - Populate filename, mimeType, sizeBytes                 │  │
│  │ - OR parse text references and create blocks             │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  UI renders messages with proper file chips/badges              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Testing Strategy

### Unit Tests

1. **FileResolver**: Test file resolution with mocked S3/DynamoDB
2. **PromptBuilder**: Test multimodal prompt construction
3. **Image/Document Handlers**: Verify correct ContentBlock format
4. **enrichFileAttachments**: Test metadata merging with various scenarios
5. **parseFileReferencesFromText**: Test text parsing and block reconstruction

### Integration Tests

1. Upload image -> Send message -> Verify agent receives image
2. Upload PDF -> Send message -> Verify agent receives document
3. Upload 5+ files -> Verify limit enforcement
4. Upload to session A, try to use in session B -> Verify access denied
5. **File metadata restoration**: Load session with files -> Verify file metadata populated
6. **SessionIndex GSI query**: Verify `GET /files?sessionId=X` returns correct files

### E2E Tests

1. Complete flow: Upload PDF -> Ask question about it -> Verify response references content
2. Image analysis: Upload image -> Ask to describe -> Verify description
3. Mixed content: Upload image + document + text message -> Verify all processed
4. **Session reload with files**: Upload file -> Send message -> Reload page -> Verify file chips display correctly
5. **Navigate between sessions**: Session A (with files) -> Session B -> Session A -> Verify file metadata restored

---

## Error Handling

| Scenario | Handling |
|----------|----------|
| File not found in S3 | Skip file, log warning, continue with other files |
| File not owned by user | Skip file, log warning (security) |
| File not in READY status | Skip file, log info |
| S3 fetch timeout | Return error event in stream |
| Base64 encoding fails | Skip file, log error |
| Bedrock rejects file format | Stream error as assistant message |

---

## Security Considerations

1. **Authorization**: Always verify `user_id` matches file owner before fetching
2. **File Size**: S3 files are already validated on upload (4MB limit)
3. **Content Type Validation**: Trust MIME type stored at upload time
4. **Rate Limiting**: File resolution inherits request rate limits
5. **Temporary URLs**: Don't expose S3 pre-signed URLs in responses

---

## Rollout Plan

1. **Phase 1**: Backend file resolution (Steps 2-6)
   - Deploy behind feature flag if needed
   - Test with documents only first

2. **Phase 2**: Image support (Steps 1, 9)
   - Add image types to allowed list
   - Add image preview in UI

3. **Phase 3**: Message display & history (Steps 10-11)
   - Show attachments in chat history
   - Restore file metadata on session load
   - Handle legacy messages with text-only file references

---

## Dependencies

- Existing file upload infrastructure (complete)
- S3 bucket with appropriate permissions
- DynamoDB tables for file metadata
- Bedrock models with document/image support

---

## Estimated Complexity

| Step | Complexity | LOC Estimate |
|------|------------|--------------|
| Step 1: Image types | Low | ~20 lines |
| Step 2: File resolver | Medium | ~100 lines |
| Step 3: Model update | Low | ~5 lines |
| Step 4: /invocations | Medium | ~30 lines |
| Step 5: /chat/stream | Medium | ~30 lines |
| Step 6: Session history | Low | ~20 lines |
| Step 7: Frontend verify | Low | ~5 lines |
| Step 8: Stream verify | Low | ~0 lines |
| Step 9: Image preview | Medium | ~40 lines |
| Step 10: Message display | Medium | ~50 lines |
| Step 11: File metadata restoration | Medium | ~80 lines |

**Total**: ~380 lines of new code
