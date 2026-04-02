# Implementation Plan: Share Conversations via Shareable URL

## Overview

Incremental implementation starting with infrastructure (DynamoDB table + SSM exports), then backend (models → service → routes → registration), then frontend (service → modal → session list integration → shared view → routing). Each task builds on the previous, ensuring no orphaned code.

## Tasks

- [x] 1. Add Shared Conversations DynamoDB table to infrastructure stack
  - [x] 1.1 Define the `{projectPrefix}-shared-conversations` DynamoDB table in `infrastructure/lib/infrastructure-stack.ts`
    - Partition key: `share_id` (String), PAY_PER_REQUEST billing, point-in-time recovery enabled, AWS_MANAGED encryption
    - Add GSI `SessionShareIndex` with partition key `session_id` (String)
    - Add GSI `OwnerShareIndex` with partition key `owner_id` (String) and sort key `created_at` (String)
    - Export table name and ARN to SSM at `/{projectPrefix}/shares/shared-conversations-table-name` and `/{projectPrefix}/shares/shared-conversations-table-arn`
    - Follow existing table patterns (e.g., UsersTable, AppRolesTable) in the same file
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9_

- [x] 2. Implement backend share data models
  - [x] 2.1 Create `backend/src/apis/app_api/shares/__init__.py` and `backend/src/apis/app_api/shares/models.py`
    - Define `CreateShareRequest` with `access_level: Literal["public", "specific", "private"]` and `allowed_emails: Optional[List[str]]`
    - Define `UpdateShareRequest` with optional `access_level` and optional `allowed_emails`
    - Define `ShareResponse` with share_id, session_id, owner_id, access_level, allowed_emails, created_at, share_url
    - Define `SharedConversationResponse` with share_id, title, access_level, created_at, owner_id, messages (List[MessageResponse])
    - Add Pydantic validator: when access_level is "specific", allowed_emails must be non-empty
    - Follow patterns from `backend/src/apis/shared/sessions/models.py` (ConfigDict, Field aliases, by_alias)
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

  - [ ]* 2.2 Write property test for ShareResponse serialization round-trip
    - **Property 9: ShareResponse serialization round-trip**
    - Generate random valid ShareResponse objects with Hypothesis, serialize to JSON, deserialize back, verify equality
    - Place test in `backend/tests/apis/app_api/shares/test_share_properties.py`
    - **Validates: Requirements 10.5**

- [x] 3. Implement backend share service
  - [x] 3.1 Create `backend/src/apis/app_api/shares/service.py` with `ShareService` class
    - `create_share(session_id, user_id, user_email, access_level, allowed_emails)`: snapshot messages via `get_messages()` and metadata via `get_session_metadata()`, generate UUID share_id, store in DynamoDB, return ShareResponse
    - `get_shared_conversation(share_id, requester_id, requester_email)`: fetch from DynamoDB, check access control, return SharedConversationResponse
    - `update_share(session_id, user_id, user_email, access_level, allowed_emails)`: lookup via SessionShareIndex GSI, verify ownership, update fields in DynamoDB
    - `revoke_share(session_id, user_id)`: lookup via SessionShareIndex GSI, verify ownership, delete from DynamoDB
    - Auto-include owner email in allowed_emails when access_level is "specific"
    - On create: check SessionShareIndex for existing share, delete old share before creating new one
    - Clear allowed_emails when access_level changes to "public" or "private"
    - Read table name from `SHARED_CONVERSATIONS_TABLE_NAME` environment variable
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6, 1.7, 1.8, 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.4, 4.5_

  - [ ]* 3.2 Write property test for owner email auto-inclusion
    - **Property 2: Owner email auto-inclusion invariant**
    - Generate random email lists and owner emails with Hypothesis, verify owner always in resulting allowed_emails
    - **Validates: Requirements 1.2, 4.2, 4.5**

  - [ ]* 3.3 Write property test for access control matrix
    - **Property 5: Access control matrix**
    - Generate random (access_level, requester_email, owner_id, allowed_emails) tuples, verify access decision matches the matrix
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

  - [ ]* 3.4 Write property test for non-specific access levels clearing allowed_emails
    - **Property 8: Non-specific access levels clear allowed_emails**
    - Generate random shares, update to public/private, verify allowed_emails cleared
    - **Validates: Requirements 4.1, 4.4**

- [x] 4. Implement backend share routes and register in main.py
  - [x] 4.1 Create `backend/src/apis/app_api/shares/routes.py` with FastAPI router
    - `POST /conversations/{session_id}/share` — create share, validate request, call service, return ShareResponse
    - `GET /shared/{share_id}` — retrieve shared conversation (on a separate router with `/shared` prefix)
    - `PATCH /conversations/{session_id}/share` — update access level/emails
    - `DELETE /conversations/{session_id}/share` — revoke share, return 204
    - All endpoints use `Depends(get_current_user)` for authentication
    - Return proper HTTP status codes: 403 for non-owner, 404 for not found, 422 for validation errors
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 3.1, 3.2, 3.3, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [x] 4.2 Register share routers in `backend/src/apis/app_api/main.py`
    - Import and include the conversations share router and the shared view router
    - Follow existing router registration pattern
    - _Requirements: 1.1, 2.1_

  - [ ]* 4.3 Write property test for "specific" access requires non-empty allowed_emails
    - **Property 3: "Specific" access requires non-empty allowed_emails**
    - Generate create/update requests with access_level "specific" and empty/None allowed_emails, verify 422 response
    - **Validates: Requirements 1.3, 4.3**

  - [ ]* 4.4 Write property test for non-owner operations return 403
    - **Property 4: Non-owner operations return 403**
    - Generate random non-owner user IDs, verify 403 on create/update/delete operations
    - **Validates: Requirements 1.5, 3.2, 4.6**

  - [ ]* 4.5 Write property test for re-share replaces existing share
    - **Property 6: Re-share replaces existing share**
    - Create share, then create again for same session, verify new share_id and old share_id returns 404
    - **Validates: Requirements 1.7**

  - [ ]* 4.6 Write property test for revocation removes access
    - **Property 7: Revocation removes access**
    - Create then delete share, verify retrieval returns 404
    - **Validates: Requirements 3.1, 3.4**

- [x] 5. Checkpoint - Backend complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement frontend share service
  - [x] 6.1 Create `frontend/ai.client/src/app/session/services/share/share.service.ts`
    - Injectable service with `HttpClient`, `ConfigService`, `AuthService`
    - `createShare(sessionId, accessLevel, allowedEmails?)`: POST to `/conversations/{sessionId}/share`
    - `getSharedConversation(shareId)`: GET `/shared/{shareId}`
    - `updateShare(sessionId, accessLevel?, allowedEmails?)`: PATCH `/conversations/{sessionId}/share`
    - `revokeShare(sessionId)`: DELETE `/conversations/{sessionId}/share`
    - Call `AuthService.ensureAuthenticated()` before each request
    - Define TypeScript interfaces: `ShareResponse`, `SharedConversationResponse`, `CreateShareRequest`, `UpdateShareRequest`
    - Follow patterns from `session.service.ts`
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

- [x] 7. Implement share modal component
  - [x] 7.1 Create `frontend/ai.client/src/app/session/components/share-modal/` with standalone Angular component
    - Dialog overlay with three radio-style access options: "Keep private" (default), "Public link", "Specific people"
    - When "Specific people" selected: email input field with tag-style chips, owner email as non-removable chip
    - Remove button on each added email chip
    - Validation: at least one email required when "Specific people" selected
    - "Create share link" button calls ShareService.createShare()
    - On success: display generated URL with "Copy link" button, "Chat shared" confirmation, "Future messages aren't included" note
    - On error: display error message with retry option
    - When opened for session with existing share: display current settings, existing URL, "Create new share link" button with "This will replace the existing share link" note
    - Modify access level/emails for existing share calls ShareService.updateShare()
    - Use Tailwind CSS, ng-icons, follow existing component patterns
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 5.11, 5.12_

- [x] 8. Integrate share into session list menu
  - [x] 8.1 Add "Share" menu item to session list ellipsis menu in `frontend/ai.client/src/app/components/sidenav/components/session-list/`
    - Add "Share" button between "Rename" and "Delete" in the `sessionMenu` template
    - Use `heroArrowUpOnSquare` icon
    - Click handler opens the Share Modal for the selected session
    - _Requirements: 6.1, 6.2, 6.3_

- [x] 9. Implement shared conversation read-only view and route
  - [x] 9.1 Create `frontend/ai.client/src/app/shared/shared-view.page.ts` standalone page component
    - Fetch shared conversation via `ShareService.getSharedConversation(shareId)` using route param
    - Display conversation title, share creation timestamp, all messages from snapshot
    - Reuse existing message rendering components for text, code blocks, images, tool results
    - No message input field or editing controls
    - Display "Shared read-only snapshot" banner/badge
    - Error states: "Conversation not found" for 404, "Access denied" for 403
    - Loading state while fetching
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [x] 9.2 Add `/shared/:shareId` route to `frontend/ai.client/src/app/app.routes.ts`
    - Lazy-load SharedViewPage component
    - Protected by `authGuard`
    - _Requirements: 7.1_

  - [ ]* 9.3 Write property test for shared view rendering all snapshot data
    - **Property 10: Shared view renders all snapshot data**
    - Use fast-check to generate random SharedConversationResponse data, render component, verify title/timestamp/messages present in DOM
    - **Validates: Requirements 7.2**

- [x] 10. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Backend uses Python (FastAPI, Pydantic, Hypothesis), frontend uses TypeScript (Angular, fast-check)
- Infrastructure uses TypeScript (AWS CDK)
