"""Assistants API request/response models"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Agent Designer Phase 1 (D3): the uniform binding kinds. Requests validate against
# this set; storage tolerates unknown kinds on read so records written by newer code
# survive a read/write round trip through older code (forward + rollback compat).
KNOWN_BINDING_KINDS = ("knowledge_base", "tool", "skill", "memory_space")
BindingKind = Literal["knowledge_base", "tool", "skill", "memory_space"]


class AgentModelConfig(BaseModel):
    """Governed single-select model for an Agent (D3).

    The model is NOT a binding — it is a required singleton on the Agent record.
    Optional in storage/compat, though: a legacy Assistant has no stored model, and
    an absent ``modelConfig`` means "resolve the model exactly as today" (request →
    user default → system default). The Agent Designer UI enforces single-select at
    write time (Phase 4).
    """

    model_config = ConfigDict(populate_by_name=True)

    model_id: str = Field(..., alias="modelId", description="Selected model identifier")
    provider: Optional[str] = Field(None, description="Model provider (e.g. 'bedrock'); mirrors InvocationRequest.provider")
    params: Optional[Dict[str, Any]] = Field(
        None, description="Model parameters (temperature, maxTokens, …); floats stored via Decimal"
    )


class AgentBinding(BaseModel):
    """A single primitive binding on an Agent (D3).

    ``kind`` is an open string on read (unknown kinds pass through untouched); the
    request layer validates it against ``KNOWN_BINDING_KINDS``. Phase 1 resolves only
    ``memory_space`` and ``knowledge_base``; ``tool`` and ``skill`` are accepted and
    stored but inert (not resolved) until Phase 2/3.
    """

    model_config = ConfigDict(populate_by_name=True)

    kind: str = Field(..., description="Binding kind (see KNOWN_BINDING_KINDS)")
    ref: str = Field(..., description="Primitive identifier this binding points at")
    config: Dict[str, Any] = Field(default_factory=dict, description="Kind-specific configuration")


class Assistant(BaseModel):
    """Complete assistant model (internal use)"""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    assistant_id: str = Field(..., alias="assistantId", description="Assistant identifier")
    owner_id: str = Field(..., alias="ownerId", description="User/owner identifier (internal, not returned in responses)")
    owner_name: str = Field(..., alias="ownerName", description="Owner display name (public)")
    name: str = Field(..., description="Assistant display name")
    description: str = Field(..., description="Short summary for UI cards")
    instructions: str = Field(..., description="System prompt for the assistant")
    vector_index_id: str = Field(..., alias="vectorIndexId", description="S3 vector index name")
    visibility: Literal["PRIVATE", "PUBLIC", "SHARED"] = Field(..., description="Access control level")
    tags: Optional[List[str]] = Field(default_factory=list, description="Search keywords")
    starters: Optional[List[str]] = Field(default_factory=list, description="Conversation starter prompts")
    emoji: Optional[str] = Field(None, description="Single emoji character for assistant avatar")
    usage_count: int = Field(0, alias="usageCount", description="Number of times used")
    last_used_at: Optional[str] = Field(
        None,
        alias="lastUsedAt",
        description="ISO 8601 timestamp of last chat use (any user); drives the KB-sync inactivity pause",
    )
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp of creation")
    updated_at: str = Field(..., alias="updatedAt", description="ISO 8601 timestamp of last update")
    status: Literal["DRAFT", "COMPLETE"] = Field(..., description="Assistant lifecycle status")
    image_url: Optional[str] = Field(None, alias="imageUrl", description="URL to assistant avatar/image")

    # Agent Designer Phase 1 (D3): additive, optional. Absent on every legacy row.
    # NOTE (R3): the model field cannot be named ``model_config`` — pydantic reserves
    # that for ConfigDict — so it is ``model_settings`` with the ``modelConfig`` alias.
    model_settings: Optional[AgentModelConfig] = Field(
        None, alias="modelConfig", description="Governed single-select model (D3); absent = resolve as today"
    )
    bindings: Optional[List[AgentBinding]] = Field(
        None, description="Uniform primitive bindings (D3); absent = synthesize legacy KB binding via compat"
    )


class CreateAssistantDraftRequest(BaseModel):
    """Request body for creating a draft assistant (minimal fields)"""

    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(None, description="Assistant name (defaults to 'Untitled Assistant')")


class CreateAssistantRequest(BaseModel):
    """Request body for creating a complete assistant"""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., description="Assistant display name")
    description: str = Field(..., description="Short summary")
    instructions: str = Field(..., description="System prompt")
    visibility: Literal["PRIVATE", "PUBLIC", "SHARED"] = Field("PRIVATE", description="Access control")
    tags: Optional[List[str]] = Field(default_factory=list, description="Search keywords")
    starters: Optional[List[str]] = Field(default_factory=list, description="Conversation starter prompts")
    emoji: Optional[str] = Field(None, description="Single emoji character for assistant avatar")
    image_url: Optional[str] = Field(None, alias="imageUrl", description="URL to assistant avatar/image")


class UpdateAssistantRequest(BaseModel):
    """Request body for updating an assistant (all fields optional)"""

    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(None, description="Assistant display name")
    description: Optional[str] = Field(None, description="Short summary")
    instructions: Optional[str] = Field(None, description="System prompt")
    visibility: Optional[Literal["PRIVATE", "PUBLIC", "SHARED"]] = Field(None, description="Access control")
    tags: Optional[List[str]] = Field(None, description="Search keywords")
    starters: Optional[List[str]] = Field(None, description="Conversation starter prompts")
    emoji: Optional[str] = Field(None, description="Single emoji character for assistant avatar")
    status: Optional[Literal["DRAFT", "COMPLETE"]] = Field(None, description="Lifecycle status")
    image_url: Optional[str] = Field(None, alias="imageUrl", description="URL to assistant avatar/image")


class AssistantResponse(BaseModel):
    """Response containing assistant data (owner_id excluded for privacy)"""

    model_config = ConfigDict(populate_by_name=True)

    assistant_id: str = Field(..., alias="assistantId", description="Assistant identifier")
    owner_name: str = Field(..., alias="ownerName", description="Owner display name")
    name: str = Field(..., description="Assistant display name")
    description: str = Field(..., description="Short summary")
    instructions: str = Field(..., description="System prompt")
    vector_index_id: str = Field(..., alias="vectorIndexId", description="S3 vector index name")
    visibility: Literal["PRIVATE", "PUBLIC", "SHARED"] = Field(..., description="Access control")
    tags: Optional[List[str]] = Field(default_factory=list, description="Search keywords")
    starters: Optional[List[str]] = Field(default_factory=list, description="Conversation starter prompts")
    emoji: Optional[str] = Field(None, description="Single emoji character for assistant avatar")
    usage_count: int = Field(..., alias="usageCount", description="Usage count")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 creation timestamp")
    updated_at: str = Field(..., alias="updatedAt", description="ISO 8601 update timestamp")
    status: Literal["DRAFT", "COMPLETE"] = Field(..., description="Lifecycle status")
    image_url: Optional[str] = Field(None, alias="imageUrl", description="URL to assistant avatar/image")

    # Share metadata (only present for shared assistants)
    first_interacted: Optional[bool] = Field(None, alias="firstInteracted", description="Whether user has interacted with this shared assistant")
    is_shared_with_me: Optional[bool] = Field(
        None, alias="isSharedWithMe", description="Whether this assistant is shared with the requesting user (not owned)"
    )
    user_permission: Optional[Literal["owner", "editor", "viewer"]] = Field(
        None, alias="userPermission", description="Requesting user's permission level on this assistant"
    )


class AssistantsListResponse(BaseModel):
    """Response for listing assistants with pagination support"""

    model_config = ConfigDict(populate_by_name=True)

    assistants: List[AssistantResponse] = Field(..., description="List of assistants for the user")
    next_token: Optional[str] = Field(None, alias="nextToken", description="Pagination token for next page")


class AssistantTestChatRequest(BaseModel):
    """Request body for testing assistant chat with RAG"""

    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(..., description="User message to test")
    session_id: Optional[str] = Field(None, description="Optional session ID for ephemeral chat")


class ShareAssistantRequest(BaseModel):
    """Request body for sharing an assistant with email addresses"""

    model_config = ConfigDict(populate_by_name=True)

    emails: List[str] = Field(..., min_length=1, description="Email addresses to share with")
    permission: Literal["viewer", "editor"] = Field(
        "viewer", description="Permission level granted to each shared user"
    )


class UnshareAssistantRequest(BaseModel):
    """Request body for removing shares from an assistant"""

    model_config = ConfigDict(populate_by_name=True)

    emails: List[str] = Field(..., min_length=1, description="Email addresses to remove from shares")


class UpdateSharePermissionRequest(BaseModel):
    """Request body for changing an existing share's permission level"""

    model_config = ConfigDict(populate_by_name=True)

    email: str = Field(..., description="Email address of the existing share to update")
    permission: Literal["viewer", "editor"] = Field(..., description="New permission level")


class ShareEntry(BaseModel):
    """A single share record (email + permission level)"""

    model_config = ConfigDict(populate_by_name=True)

    email: str = Field(..., description="Email address (normalized lowercase)")
    permission: Literal["viewer", "editor"] = Field(
        "viewer", description="Permission level granted to this user"
    )


class AssistantSharesResponse(BaseModel):
    """Response containing share records for an assistant"""

    model_config = ConfigDict(populate_by_name=True)

    assistant_id: str = Field(..., alias="assistantId", description="Assistant identifier")
    shared_with: List[ShareEntry] = Field(
        ..., alias="sharedWith", description="List of share records (email + permission)"
    )
