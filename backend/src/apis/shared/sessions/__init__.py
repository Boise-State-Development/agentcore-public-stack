"""Shared session management module for API projects.

This module provides session models, metadata operations, and message handling
that are shared between the app API and inference API.
"""

# Export models
from .models import (
    # Session models
    SessionMetadata,
    SessionPreferences,
    SessionMetadataResponse,
    SessionsListResponse,
    UpdateSessionMetadataRequest,
    BulkDeleteSessionsRequest,
    BulkDeleteSessionResult,
    BulkDeleteSessionsResponse,
    # Message models
    Message,
    MessageContent,
    MessageResponse,
    MessagesListResponse,
    MessageMetadata,
    LatencyMetrics,
    TokenUsage,
    ModelInfo,
    PricingSnapshot,
    Attribution,
    Citation,
)

# Export metadata operations
from .metadata import (
    store_message_metadata,
    store_session_metadata,
    ensure_session_metadata_exists,
    update_session_title,
    update_session_activity,
    get_session_metadata,
    get_all_message_metadata,
    list_user_sessions,
)

# Export message operations
from .messages import (
    get_messages,
    get_messages_from_cloud,
)

__all__ = [
    # Session models
    "SessionMetadata",
    "SessionPreferences",
    "SessionMetadataResponse",
    "SessionsListResponse",
    "UpdateSessionMetadataRequest",
    "BulkDeleteSessionsRequest",
    "BulkDeleteSessionResult",
    "BulkDeleteSessionsResponse",
    # Message models
    "Message",
    "MessageContent",
    "MessageResponse",
    "MessagesListResponse",
    "MessageMetadata",
    "LatencyMetrics",
    "TokenUsage",
    "ModelInfo",
    "PricingSnapshot",
    "Attribution",
    "Citation",
    # Metadata operations
    "store_message_metadata",
    "store_session_metadata",
    "ensure_session_metadata_exists",
    "update_session_title",
    "update_session_activity",
    "get_session_metadata",
    "get_all_message_metadata",
    "list_user_sessions",
    # Message operations
    "get_messages",
    "get_messages_from_cloud",
]
