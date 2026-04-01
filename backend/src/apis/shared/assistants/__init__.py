"""Shared assistants module

This module provides assistant-related functionality shared between
app_api and inference_api deployments.
"""

from .models import (
    Assistant,
    AssistantResponse,
    AssistantsListResponse,
    AssistantTestChatRequest,
    CreateAssistantDraftRequest,
    CreateAssistantRequest,
    ShareAssistantRequest,
    UnshareAssistantRequest,
    AssistantSharesResponse,
    UpdateAssistantRequest,
)
from .service import (
    assistant_exists,
    check_share_access,
    create_assistant,
    create_assistant_draft,
    delete_assistant,
    get_assistant,
    get_assistant_with_access_check,
    list_assistant_shares,
    list_shared_with_user,
    list_user_assistants,
    mark_share_as_interacted,
    share_assistant,
    unshare_assistant,
    update_assistant,
)
from .rag_service import (
    augment_prompt_with_context,
    search_assistant_knowledgebase_with_formatting,
)

__all__ = [
    # Models
    "Assistant",
    "AssistantResponse",
    "AssistantsListResponse",
    "AssistantTestChatRequest",
    "CreateAssistantDraftRequest",
    "CreateAssistantRequest",
    "ShareAssistantRequest",
    "UnshareAssistantRequest",
    "AssistantSharesResponse",
    "UpdateAssistantRequest",
    # Service functions
    "assistant_exists",
    "check_share_access",
    "create_assistant",
    "create_assistant_draft",
    "delete_assistant",
    "get_assistant",
    "get_assistant_with_access_check",
    "list_assistant_shares",
    "list_shared_with_user",
    "list_user_assistants",
    "mark_share_as_interacted",
    "share_assistant",
    "unshare_assistant",
    "update_assistant",
    # RAG service functions
    "augment_prompt_with_context",
    "search_assistant_knowledgebase_with_formatting",
]
