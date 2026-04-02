# Requirements: Share Conversations via Shareable URL

## Introduction

Enable users to share conversations by generating a shareable URL that provides a read-only snapshot of the conversation at the time of sharing. Users control access via a modal with three options: "Keep private" (only the owner can access, default), "Public link" (any authenticated user with the link), or "Specific people" (only designated email addresses). The snapshot is point-in-time; future messages are not included.

## Glossary

- **Share_Modal**: The dialog overlay component that presents access options, email input controls, and displays the generated shareable URL
- **Share_Service**: The frontend Angular service responsible for making API calls to the share endpoints
- **Share_API**: The backend FastAPI router handling share creation, retrieval, update, and revocation
- **Shared_Conversations_Table**: The DynamoDB table storing shared conversation snapshots (partition key: share_id)
- **Snapshot**: A point-in-time copy of conversation metadata and messages, frozen at the moment of sharing
- **Share_ID**: A UUID that uniquely identifies a shared conversation snapshot
- **Access_Level**: The visibility setting for a shared conversation: "public" (any authenticated user with the link), "specific" (only designated email addresses), or "private" (only the owner can access)
- **Allowed_Emails**: A list of email addresses permitted to view a shared conversation when access_level is "specific"; the owner's email is always automatically included
- **Session_List**: The sidebar component displaying the user's conversation list with ellipsis menus
- **Shared_View**: The read-only page component that renders a shared conversation snapshot at the `/shared/{share_id}` route

## Requirements

### Requirement 1: Share Conversation Snapshot Creation

**User Story:** As a user, I want to create a shareable snapshot of a conversation, so that I can share a point-in-time copy with others.

#### Acceptance Criteria

1. WHEN a user sends a POST request to `/conversations/{session_id}/share` with access_level "public", THE Share_API SHALL create a Snapshot containing the conversation metadata and all messages at that point in time, generate a unique Share_ID, store the Snapshot in the Shared_Conversations_Table, and return the Share_ID and shareable URL in the response.
2. WHEN a user sends a POST request to `/conversations/{session_id}/share` with access_level "specific" and a non-empty allowed_emails list, THE Share_API SHALL automatically include the owner's email in the allowed_emails list if not already present, create a Snapshot, store the Snapshot along with the allowed_emails list in the Shared_Conversations_Table, and return the Share_ID and shareable URL in the response.
3. WHEN a user sends a POST request to `/conversations/{session_id}/share` with access_level "specific" and an empty or missing allowed_emails list, THE Share_API SHALL return a 422 Validation Error indicating that allowed_emails is required for "specific" access.
4. WHEN a user sends a POST request to `/conversations/{session_id}/share` with access_level "private", THE Share_API SHALL create a Snapshot accessible only to the owner, store the Snapshot in the Shared_Conversations_Table without an allowed_emails list, and return the Share_ID and shareable URL in the response.
5. WHEN a user sends a POST request to `/conversations/{session_id}/share` for a session the user does not own, THE Share_API SHALL return a 403 Forbidden error.
6. WHEN a user sends a POST request to `/conversations/{session_id}/share` for a session that does not exist, THE Share_API SHALL return a 404 Not Found error.
7. WHEN a user sends a POST request to `/conversations/{session_id}/share` for a session that already has an active share, THE Share_API SHALL revoke the existing share (delete the old Snapshot from the Shared_Conversations_Table), create a fresh Snapshot with a new Share_ID and URL, and return the new share details. The old share link SHALL no longer be accessible.
8. THE Share_API SHALL store the following fields in the Shared_Conversations_Table for each Snapshot: share_id (PK), session_id, owner_id, access_level, allowed_emails (list, stored only when access_level is "specific"), created_at (ISO 8601), metadata (conversation metadata snapshot), and messages (conversation messages snapshot).

---

### Requirement 2: Shared Conversation Retrieval

**User Story:** As an authenticated user, I want to view a shared conversation via its shareable URL, so that I can read the conversation content.

#### Acceptance Criteria

1. WHEN an authenticated user sends a GET request to `/shared/{share_id}` for a share with access_level "public", THE Share_API SHALL return the Snapshot metadata and messages in a read-only format.
2. WHEN an authenticated user sends a GET request to `/shared/{share_id}` for a share with access_level "specific" and the requesting user's email is in the allowed_emails list, THE Share_API SHALL return the Snapshot metadata and messages in a read-only format.
3. WHEN an authenticated user sends a GET request to `/shared/{share_id}` for a share with access_level "specific" and the requesting user's email is not in the allowed_emails list and the user is not the owner, THE Share_API SHALL return a 403 Forbidden error.
4. WHEN an authenticated user sends a GET request to `/shared/{share_id}` for a share with access_level "private" and the user is not the owner, THE Share_API SHALL return a 403 Forbidden error.
5. WHEN an authenticated user sends a GET request to `/shared/{share_id}` for a share the user owns, THE Share_API SHALL return the Snapshot metadata and messages regardless of access_level.
6. WHEN an unauthenticated request is sent to GET `/shared/{share_id}`, THE Share_API SHALL return a 401 Unauthorized error.
7. WHEN a GET request is sent to `/shared/{share_id}` with a Share_ID that does not exist, THE Share_API SHALL return a 404 Not Found error.

---

### Requirement 3: Share Link Revocation

**User Story:** As a user, I want to revoke a shared link, so that the conversation is no longer accessible to others.

#### Acceptance Criteria

1. WHEN a user sends a DELETE request to `/conversations/{session_id}/share`, THE Share_API SHALL remove the Snapshot from the Shared_Conversations_Table and return a 204 No Content response.
2. WHEN a user sends a DELETE request to `/conversations/{session_id}/share` for a session the user does not own, THE Share_API SHALL return a 403 Forbidden error.
3. WHEN a user sends a DELETE request to `/conversations/{session_id}/share` for a session with no active share, THE Share_API SHALL return a 404 Not Found error.
4. WHEN a share has been revoked, THE Share_API SHALL return a 404 Not Found error for subsequent GET requests to `/shared/{share_id}` using the revoked Share_ID.

---

### Requirement 4: Share Access Level and Allowed Emails Update

**User Story:** As a user, I want to change the access level and allowed email list of an existing share, so that I can adjust who can view the shared conversation without creating a new link.

#### Acceptance Criteria

1. WHEN a user sends a PATCH request to `/conversations/{session_id}/share` with a new access_level value of "public", THE Share_API SHALL update the access_level field in the Shared_Conversations_Table, clear the allowed_emails field, and return the updated share details.
2. WHEN a user sends a PATCH request to `/conversations/{session_id}/share` with access_level "specific" and a non-empty allowed_emails list, THE Share_API SHALL automatically include the owner's email in the allowed_emails list if not already present, update both the access_level and allowed_emails fields in the Shared_Conversations_Table, and return the updated share details.
3. WHEN a user sends a PATCH request to `/conversations/{session_id}/share` with access_level "specific" and an empty or missing allowed_emails list, THE Share_API SHALL return a 422 Validation Error indicating that allowed_emails is required for "specific" access.
4. WHEN a user sends a PATCH request to `/conversations/{session_id}/share` with a new access_level value of "private", THE Share_API SHALL update the access_level field in the Shared_Conversations_Table, clear the allowed_emails field, and return the updated share details.
5. WHEN a user sends a PATCH request to `/conversations/{session_id}/share` with only an updated allowed_emails list (no access_level change), THE Share_API SHALL automatically include the owner's email in the allowed_emails list if not already present, update the allowed_emails field, and return the updated share details.
6. WHEN a user sends a PATCH request to `/conversations/{session_id}/share` for a session the user does not own, THE Share_API SHALL return a 403 Forbidden error.
7. WHEN a user sends a PATCH request to `/conversations/{session_id}/share` for a session with no active share, THE Share_API SHALL return a 404 Not Found error.
8. WHEN a user sends a PATCH request with an invalid access_level value (not "public", "specific", or "private"), THE Share_API SHALL return a 422 Validation Error.

---

### Requirement 5: Share Modal UI

**User Story:** As a user, I want a modal dialog to configure and create a share link, so that I can control access and copy the URL conveniently.

#### Acceptance Criteria

1. WHEN a user clicks "Share conversation" from the ellipsis menu on a session in the Session_List, THE Share_Modal SHALL open as a dialog overlay.
2. THE Share_Modal SHALL display three access options as radio-style selections: "Keep private" (only the owner can access, selected by default), "Public link" (any authenticated user with the link can view), and "Specific people" (only designated email addresses can view).
3. WHEN the user selects "Specific people", THE Share_Modal SHALL display an email input field that allows the user to add one or more email addresses to the allowed list, with the current user's email shown as a non-removable entry.
4. THE Share_Modal SHALL allow the user to remove individual email addresses from the allowed list by clicking a remove control next to each email.
5. WHEN the user clicks "Create share link" with "Specific people" selected and no email addresses added, THE Share_Modal SHALL display a validation message indicating at least one email address is required.
6. WHEN the user clicks "Create share link", THE Share_Modal SHALL call the Share_Service to create the share with the selected access_level and allowed_emails (when applicable) and display the generated URL with a "Copy link" button.
7. WHEN the user clicks the "Copy link" button, THE Share_Modal SHALL copy the shareable URL to the clipboard and display a confirmation indicator.
8. WHEN a share is successfully created, THE Share_Modal SHALL display a "Chat shared" confirmation with the note "Future messages aren't included".
9. IF the Share_Service returns an error during share creation, THEN THE Share_Modal SHALL display an error message and allow the user to retry.
10. WHEN the Share_Modal opens for a session that already has an active share, THE Share_Modal SHALL display the existing share URL, current access level, and current allowed email list (when access_level is "specific"), along with a "Create new share link" button to replace the existing share with a fresh snapshot.
11. WHEN the user modifies the access level or allowed email list for an existing share, THE Share_Modal SHALL call the Share_Service to update the share and reflect the updated settings.
12. WHEN the user clicks "Create new share link" for a session with an existing share, THE Share_Modal SHALL display a note "This will replace the existing share link" and call the Share_Service to create a new share, which revokes the old link and generates a fresh snapshot with a new URL.

---

### Requirement 6: Session List Menu Integration

**User Story:** As a user, I want a "Share" option in the conversation ellipsis menu, so that I can initiate sharing from the sidebar.

#### Acceptance Criteria

1. THE Session_List SHALL display a "Share" menu item in the ellipsis menu for each conversation, positioned between the "Rename" and "Delete" options.
2. WHEN the user clicks the "Share" menu item, THE Session_List SHALL open the Share_Modal for the selected conversation.
3. THE "Share" menu item SHALL display a share icon (heroArrowUpOnSquare or equivalent) alongside the text label.

---

### Requirement 7: Shared Conversation Read-Only View

**User Story:** As a user viewing a shared conversation, I want a read-only view that displays the conversation content, so that I can read the shared messages without editing capabilities.

#### Acceptance Criteria

1. THE Shared_View SHALL be accessible at the route `/shared/{share_id}` and protected by the existing auth guard.
2. THE Shared_View SHALL display the conversation title, the share creation timestamp, and all messages from the Snapshot.
3. THE Shared_View SHALL render message content identically to the existing conversation view, including text, code blocks, images, and tool results.
4. THE Shared_View SHALL NOT display a message input field or any controls that allow modifying the conversation.
5. THE Shared_View SHALL display a visual indicator (banner or badge) that the conversation is a shared read-only snapshot.
6. IF the Share_API returns a 404 or 403 for the requested Share_ID, THEN THE Shared_View SHALL display an appropriate error message ("Conversation not found" or "Access denied").

---

### Requirement 8: Share Frontend Service

**User Story:** As a frontend developer, I want a dedicated service for share API calls, so that share logic is encapsulated and reusable.

#### Acceptance Criteria

1. THE Share_Service SHALL expose a method to create a share (POST `/conversations/{session_id}/share`) that accepts a session ID, access level, and an optional list of allowed emails, and returns the share details including the shareable URL.
2. THE Share_Service SHALL expose a method to retrieve a shared conversation (GET `/shared/{share_id}`) that accepts a Share_ID and returns the Snapshot data.
3. THE Share_Service SHALL expose a method to revoke a share (DELETE `/conversations/{session_id}/share`) that accepts a session ID.
4. THE Share_Service SHALL expose a method to update share settings (PATCH `/conversations/{session_id}/share`) that accepts a session ID, an optional new access level, and an optional updated allowed emails list.
5. THE Share_Service SHALL ensure the user is authenticated before making API calls by calling `AuthService.ensureAuthenticated()`.

---

### Requirement 9: Shared Conversations DynamoDB Table

**User Story:** As a platform operator, I want a DynamoDB table for shared conversation snapshots, so that shared data is stored reliably and independently from the original conversation.

#### Acceptance Criteria

1. THE Shared_Conversations_Table SHALL be defined in the CDK InfrastructureStack with the table name `{projectPrefix}-shared-conversations`.
2. THE Shared_Conversations_Table SHALL use `share_id` (String) as the partition key.
3. THE Shared_Conversations_Table SHALL use PAY_PER_REQUEST billing mode.
4. THE Shared_Conversations_Table SHALL have point-in-time recovery enabled.
5. THE Shared_Conversations_Table SHALL use AWS managed encryption.
6. THE Shared_Conversations_Table SHALL have a Global Secondary Index named `SessionShareIndex` with partition key `session_id` (String) to enable lookup of shares by original session.
7. THE Shared_Conversations_Table name and ARN SHALL be exported to SSM parameters at `/{projectPrefix}/shares/shared-conversations-table-name` and `/{projectPrefix}/shares/shared-conversations-table-arn`.
8. THE Shared_Conversations_Table SHALL have a Global Secondary Index named `OwnerShareIndex` with partition key `owner_id` (String) and sort key `created_at` (String) to enable listing shares by owner.
9. THE Shared_Conversations_Table SHALL store the allowed_emails field as a List of Strings for items where access_level is "specific".

---

### Requirement 10: Backend Share Data Models

**User Story:** As a backend developer, I want Pydantic models for share requests and responses, so that API contracts are validated and documented.

#### Acceptance Criteria

1. THE Share_API SHALL define a `CreateShareRequest` model with a required `access_level` field accepting "public", "specific", or "private", and an optional `allowed_emails` field (List of strings) that is required when access_level is "specific".
2. THE Share_API SHALL define a `ShareResponse` model containing: share_id, session_id, owner_id, access_level, allowed_emails (optional list), created_at, and share_url.
3. THE Share_API SHALL define a `SharedConversationResponse` model containing: share_id, title, access_level, created_at, owner_id, and messages (list of MessageResponse).
4. THE Share_API SHALL define an `UpdateShareRequest` model with an optional `access_level` field accepting "public", "specific", or "private", and an optional `allowed_emails` field (List of strings) that is required when access_level is "specific".
5. FOR ALL valid ShareResponse objects, serializing to JSON then deserializing back SHALL produce an equivalent object (round-trip property).