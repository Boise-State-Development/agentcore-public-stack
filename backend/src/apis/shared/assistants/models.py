"""Assistants API request/response models"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Agent Designer Phase 1 (D3): the uniform binding kinds. Requests validate against
# this set; storage tolerates unknown kinds on read so records written by newer code
# survive a read/write round trip through older code (forward + rollback compat).
KNOWN_BINDING_KINDS = ("knowledge_base", "tool", "skill", "memory_space")
BindingKind = Literal["knowledge_base", "tool", "skill", "memory_space"]

# Agent Marketplace Phase 1 (D2): the listing lifecycle. The transition table and the
# sparse-index key derivation live in ``assistants.listing`` — this is only the wire type.
ListingState = Literal["private", "in_review", "published", "changes_requested", "taken_down"]
PublisherKind = Literal["institution", "department", "individual"]


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


class AdminEdit(BaseModel):
    """One admin edit to a listing's presentation fields (D13).

    Recorded so the author can be told what changed and when — "An admin updated the
    category on Jul 24" on their My Agents card. Editing someone's listing quietly is
    how you lose authors, so this list is append-only and never pruned on write.
    """

    model_config = ConfigDict(populate_by_name=True)

    field: str = Field(..., description="Presentation field that was edited")
    at: str = Field(..., description="ISO 8601 timestamp of the edit")
    by: str = Field(..., description="Display name of the admin who made the edit")


class AgentListing(BaseModel):
    """The publication state of an Agent in the marketplace (D2).

    **Absent means never submitted** — that is the D3 backfill default and the reason
    this is optional on ``Assistant``. Publication is an explicit forward act; nothing
    derives a listing from ``visibility``, which stays the independent access gate.

    ``extra="allow"`` mirrors ``Assistant``: a block written by newer code (a state or
    field this deployment does not know) survives a read/write round trip intact.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    state: ListingState = Field(..., description="Publication state (see assistants.listing)")
    category: str = Field(..., description="Category id; the GSI5 partition while published")
    publisher_id: str = Field(
        ...,
        alias="publisherId",
        description=(
            "PublisherProfile this listing is attributed to (D12). DISPLAY ONLY — never "
            "consult this in an access check; ownerId governs edit rights and Skills v2 "
            "invoke-through resolution."
        ),
    )
    submitted_at: Optional[str] = Field(None, alias="submittedAt", description="ISO 8601 submission timestamp")
    submitted_by: Optional[str] = Field(None, alias="submittedBy", description="User id of the submitting author")
    reviewed_at: Optional[str] = Field(None, alias="reviewedAt", description="ISO 8601 timestamp of the last review")
    reviewed_by: Optional[str] = Field(None, alias="reviewedBy", description="User id of the reviewing admin")
    review_note: Optional[str] = Field(
        None,
        alias="reviewNote",
        description="Reviewer's reason; rendered on the author's own card so they never have to ask",
    )
    admin_edits: List[AdminEdit] = Field(
        default_factory=list, alias="adminEdits", description="Append-only log of admin presentation edits (D13)"
    )


class PublisherProfile(BaseModel):
    """A display identity a listing can be attributed to (D12).

    Separate from ``ownerId`` on purpose: an institutional store needs Agents that speak
    as the institution rather than as whoever on staff built them. An individual profile
    is auto-created from the author's display name on their first submission.

    ⚠️ Publisher is display-only. ``verified`` drives a check mark, not a permission, and
    eligibility (below) is a *proposal* allowlist — an admin may set any publisher on any
    listing regardless of it. Neither ever appears in an access check.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="Publisher identifier")
    label: str = Field(..., description="Display name shown on the shelf and detail page")
    kind: PublisherKind = Field(..., description="institution | department | individual")
    verified: bool = Field(
        False, description="Admin-only check mark meaning 'a university team stands behind this'"
    )
    icon_key: Optional[str] = Field(None, alias="iconKey", description="S3 object key for the publisher icon")
    order: int = Field(0, description="Sort order in admin pickers")
    enabled: bool = Field(True, description="Whether this profile may be proposed or assigned")
    created_at: Optional[str] = Field(None, alias="createdAt", description="ISO 8601 creation timestamp")
    updated_at: Optional[str] = Field(None, alias="updatedAt", description="ISO 8601 update timestamp")


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

    # Agent Marketplace Phase 1: additive, optional, absent on every existing row. There
    # is no backfill — an absent ``listing`` IS the D3 default and means "never submitted".
    tagline: Optional[str] = Field(
        None, max_length=80, description="Shelf subtitle (D4); distinct from description, which is a summary"
    )
    icon_key: Optional[str] = Field(
        None, alias="iconKey", description="S3 object key for the square icon (D5); absent → generated fallback"
    )
    listing: Optional[AgentListing] = Field(
        None, description="Marketplace publication state (D2); absent = never submitted (D3)"
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
    # Agent Designer Phase 1 (D3): additive, optional. Validated by binding_validation.
    model_settings: Optional[AgentModelConfig] = Field(None, alias="modelConfig", description="Governed single-select model")
    bindings: Optional[List[AgentBinding]] = Field(None, description="Uniform primitive bindings")
    tagline: Optional[str] = Field(None, max_length=80, description="Shelf subtitle (D4)")


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
    # Agent Designer Phase 1 (D3): additive, optional. Validated by binding_validation.
    model_settings: Optional[AgentModelConfig] = Field(None, alias="modelConfig", description="Governed single-select model")
    bindings: Optional[List[AgentBinding]] = Field(None, description="Uniform primitive bindings")
    # Marketplace: the author owns their own tagline. Admins may also edit it, but only
    # through PATCH /admin/agents/{id}/listing, which records the edit (D13).
    tagline: Optional[str] = Field(None, max_length=80, description="Shelf subtitle (D4)")


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


class AgentCapability(BaseModel):
    """One thing an Agent can reach, as the detail page names it (Phase 3, D4/D6).

    ⚠️ **Names, never refs.** A capability is rendered to anyone who can see the Agent,
    so it carries the primitive's display name and its kind — not the ``binding.ref``,
    not a tool id, not a skill id. That is the same reasoning as the shelf projection in
    ``AgentListingResponse``, one level less narrow.

    Labels resolve against the **unfiltered** catalog rather than the viewer's, because
    D6 has to be able to name a capability the viewer *lacks*. Availability is the
    separate ``/runnability`` read; this list is what the Agent binds, full stop.
    """

    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(..., description="Display name of the bound primitive")
    kind: str = Field(..., description="Binding kind (tool | skill | memory_space)")


RunnabilityState = Literal["ready", "limits", "blocked"]


class MissingCapability(BaseModel):
    """A capability the viewer cannot reach, named for the D6 availability line."""

    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(..., description="Display name of the unavailable primitive")
    kind: str = Field(..., description="'model' or a binding kind")
    optional: bool = Field(
        False,
        description="Whether the binding was declared optional; only optional gaps degrade to 'limits'",
    )


class AgentRunnabilityResponse(BaseModel):
    """Whether this Agent will run for the requesting user (D6).

    Resolved by diffing the Agent's ``modelConfig`` + ``bindings`` against the viewer's
    own RBAC-filtered ``list_bindable`` results — composing the five existing
    per-primitive checks, never a sixth access service (Designer D4).
    """

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    state: RunnabilityState = Field(
        ..., description="ready | limits | blocked — the D6 three-way outcome"
    )
    missing: List[MissingCapability] = Field(
        default_factory=list, description="What the viewer cannot reach; empty when ready"
    )

class AgentResponse(BaseModel):
    """Agent read-shape (D3) — the projection served by the ``/agents/*`` surface.

    Consumes ``compat.to_agent_view`` output directly. ``agentId`` aliases the
    assistant id (legacy ids remain valid). Unlike ``AssistantResponse`` this carries
    ``modelConfig`` + ``bindings`` — the whole point of the Agent surface.
    """

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier (== legacy assistant id)")
    owner_name: str = Field(..., alias="ownerName", description="Owner display name")
    name: str = Field(..., description="Agent display name")
    description: str = Field(..., description="Short summary")
    instructions: Optional[str] = Field(
        None,
        description=(
            "System prompt. ⚠️ Marketplace Phase 3: gated to permission in ('owner', 'editor') "
            "and omitted otherwise. Under link-sharing, exposing it to any PUBLIC viewer was "
            "bounded; under a store it is not."
        ),
    )
    model_settings: Optional[AgentModelConfig] = Field(None, alias="modelConfig", description="Governed single-select model")
    bindings: List[AgentBinding] = Field(default_factory=list, description="Resolved bindings (legacy KB synthesized)")
    visibility: Literal["PRIVATE", "PUBLIC", "SHARED"] = Field(..., description="Access control")
    tags: Optional[List[str]] = Field(default_factory=list, description="Search keywords")
    starters: Optional[List[str]] = Field(default_factory=list, description="Conversation starter prompts")
    emoji: Optional[str] = Field(None, description="Single emoji character for agent avatar")
    image_url: Optional[str] = Field(None, alias="imageUrl", description="URL to agent avatar/image")
    usage_count: int = Field(..., alias="usageCount", description="Usage count")
    status: Literal["DRAFT", "COMPLETE"] = Field(..., description="Lifecycle status")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 creation timestamp")
    updated_at: str = Field(..., alias="updatedAt", description="ISO 8601 update timestamp")

    # Marketplace Phase 1. All three are ``None`` on an agent that has never been
    # submitted, and the routes serve this model with ``response_model_exclude_none``,
    # so an unsubmitted agent's payload is byte-identical to before this shipped.
    tagline: Optional[str] = Field(None, description="Shelf subtitle (D4)")
    icon_key: Optional[str] = Field(None, alias="iconKey", description="S3 object key for the square icon (D5)")
    listing: Optional[AgentListing] = Field(None, description="Publication state (D2); absent = never submitted")

    # Marketplace Phase 3 — the detail read. Both are populated only by GET /agents/{id};
    # the list route leaves them None (and serves ``response_model_exclude_none``), so
    # listing payloads are unchanged and no N-agent label fan-out happens on a list read.
    capabilities: Optional[List[AgentCapability]] = Field(
        None, description="What the Agent can reach, by display name (D4/D6)"
    )
    model_label: Optional[str] = Field(
        None,
        alias="modelLabel",
        description="Display name of the pinned model, for the detail Details panel; absent when no model is pinned",
    )
    publisher: Optional["ListingPublisher"] = Field(
        None,
        description=(
            "Attribution as the detail page renders it (D12), resolved from "
            "``listing.publisherId``. DISPLAY ONLY — the id itself is never returned and "
            "never gates access."
        ),
    )
    category_label: Optional[str] = Field(
        None,
        alias="categoryLabel",
        description="Human-readable category name; ``listing.category`` is an id an admin may have renamed",
    )

    # Share metadata (parity with AssistantResponse; set post-projection)
    first_interacted: Optional[bool] = Field(None, alias="firstInteracted")
    is_shared_with_me: Optional[bool] = Field(None, alias="isSharedWithMe")
    user_permission: Optional[Literal["owner", "editor", "viewer"]] = Field(None, alias="userPermission")



class AgentsListResponse(BaseModel):
    """Response for listing Agents."""

    model_config = ConfigDict(populate_by_name=True)

    agents: List[AgentResponse] = Field(..., description="Agents visible to the caller")
    next_token: Optional[str] = Field(None, alias="nextToken", description="Pagination token for next page")


class AgentSharesResponse(BaseModel):
    """Share records for an Agent (agentId == assistantId; same underlying records)."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    shared_with: List["ShareEntry"] = Field(..., alias="sharedWith", description="Share records (email + permission)")


class BindableItem(BaseModel):
    """A single bindable primitive in the Agent Designer palette (Phase 2, D4).

    The ``GET /agents/bindable?kind=…`` catalog returns a uniform, RBAC-filtered list
    of these so every picker in the Designer consumes the same shape. ``ref`` is the
    value the UI stores: ``modelConfig.modelId`` for ``kind == "model"``, otherwise a
    ``binding.ref``. ``meta`` carries kind-specific display extras (provider, MCP
    server sub-tools, memory-space role, …) that the picker renders but never has to
    understand structurally.
    """

    model_config = ConfigDict(populate_by_name=True)

    kind: str = Field(..., description="'model' or a binding kind (see KNOWN_BINDING_KINDS)")
    ref: str = Field(..., description="Value to store: modelConfig.modelId (model) or binding.ref")
    label: str = Field(..., description="Human-readable display name")
    description: str = Field("", description="Short summary for the picker")
    meta: Dict[str, Any] = Field(default_factory=dict, description="Kind-specific display extras")


class BindableListResponse(BaseModel):
    """RBAC-filtered catalog of bindable primitives of one kind (Phase 2)."""

    model_config = ConfigDict(populate_by_name=True)

    kind: str = Field(..., description="The requested primitive kind")
    items: List[BindableItem] = Field(default_factory=list, description="Primitives the caller may bind")


# ─────────────────────────────────────────────────────── Agent Marketplace (Phase 1)
# Behavior fields, named once so the D13 guard and its test agree on the list. An admin
# may edit everything the store *renders*; an admin may never edit what the agent *does*
# — that would make the reviewer responsible for behavior they did not write and cannot
# test, and would silently break an Agent its author still maintains.
BEHAVIOR_FIELDS = ("instructions", "bindings", "modelConfig", "model_settings", "starters", "visibility")

# Presentation fields an admin may edit on someone else's listing (D13).
ADMIN_EDITABLE_FIELDS = ("name", "tagline", "iconKey", "category", "publisherId")


class SubmitListingRequest(BaseModel):
    """Author submits an Agent for review (D2). Runs the D7 disclosure checks."""

    model_config = ConfigDict(populate_by_name=True)

    category: str = Field(..., description="Category id; must be a known category")
    publisher_id: Optional[str] = Field(
        None,
        alias="publisherId",
        description=(
            "Proposed PublisherProfile (D12). Omit to use the author's own individual "
            "profile, which is auto-created on first submission."
        ),
    )
    note: Optional[str] = Field(None, max_length=2000, description="Optional note to the reviewer")


class SkillExposure(BaseModel):
    """One skill that publication would make readable (D7.1)."""

    model_config = ConfigDict(populate_by_name=True)

    ref: str = Field(..., description="Skill identifier")
    label: str = Field(..., description="Skill display name, for the submit dialog's enumeration")


class ListingSubmissionResponse(BaseModel):
    """The resulting listing plus the disclosures the author was shown (D7)."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    listing: AgentListing = Field(..., description="The listing after the transition")
    exposed_skills: List[SkillExposure] = Field(
        default_factory=list,
        alias="exposedSkills",
        description="Skills the author wrote that become readable to anyone who runs this agent (D7.1)",
    )


class ReviewListingRequest(BaseModel):
    """Admin approves a submission or returns it with a reason (D2)."""

    model_config = ConfigDict(populate_by_name=True)

    decision: Literal["approve", "request_changes"] = Field(..., description="Reviewer's decision")
    note: Optional[str] = Field(
        None, max_length=2000, description="Reason; required for request_changes, renders on the author's card"
    )
    category: Optional[str] = Field(None, description="Optionally recategorize at approval (D12/D13)")
    publisher_id: Optional[str] = Field(
        None, alias="publisherId", description="Optionally reattribute at approval — approval makes it authoritative (D12)"
    )


class TakedownRequest(BaseModel):
    """Admin delists a published Agent (D2). A delisting, not a revocation."""

    model_config = ConfigDict(populate_by_name=True)

    reason: str = Field(..., min_length=1, max_length=2000, description="Reason sent to the author")


class AdminListingPatchRequest(BaseModel):
    """Presentation-only edit to a listing (D13).

    Every field the browse and detail pages render must be admin-editable without the
    author's involvement — a typo in a tagline or an off-brand icon should not require a
    round trip. Behavior stays with the author, and this model is where that line is
    enforced: ``extra="forbid"`` rejects anything not listed here, and the validator
    below turns the specific behavior-field case into a message that says *why*.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: Optional[str] = Field(None, min_length=1, description="Agent display name")
    tagline: Optional[str] = Field(None, max_length=80, description="Shelf subtitle")
    icon_key: Optional[str] = Field(None, alias="iconKey", description="S3 object key for the square icon")
    category: Optional[str] = Field(None, description="Category id; rewrites the GSI5 partition while published")
    publisher_id: Optional[str] = Field(
        None, alias="publisherId", description="Attribution (D12) — display only, never an access grant"
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_behavior_fields(cls, data: Any) -> Any:
        """Name the behavior fields explicitly rather than leaving a bare 'extra inputs'.

        ``extra="forbid"`` already blocks these; this exists so the reviewer is told the
        rule instead of the symptom.
        """
        if isinstance(data, dict):
            offending = [f for f in BEHAVIOR_FIELDS if f in data]
            if offending:
                raise ValueError(
                    f"Cannot edit agent behavior from the listing surface: {', '.join(sorted(set(offending)))}. "
                    "Admins may edit presentation only "
                    f"({', '.join(ADMIN_EDITABLE_FIELDS)}); behavior belongs to the agent's author."
                )
        return data


class AdminListingRow(BaseModel):
    """One row in the admin Review queue or Listings table."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    name: str = Field(..., description="Agent display name")
    tagline: Optional[str] = Field(None, description="Shelf subtitle")
    emoji: Optional[str] = Field(None, description="Emoji, for the generated icon fallback (D5)")
    icon_key: Optional[str] = Field(None, alias="iconKey", description="S3 object key for the square icon")
    owner_name: str = Field(..., alias="ownerName", description="Author display name — who to talk to about behavior")
    publisher: Optional[PublisherProfile] = Field(None, description="Resolved attribution (D12), display only")
    category: str = Field(..., description="Category id")
    state: ListingState = Field(..., description="Publication state")
    usage_count: int = Field(0, alias="usageCount", description="Chats — admin reporting only, never shelf furniture (D4)")
    submitted_at: Optional[str] = Field(None, alias="submittedAt", description="ISO 8601 submission timestamp")
    reviewed_at: Optional[str] = Field(None, alias="reviewedAt", description="ISO 8601 timestamp of the last review")
    review_note: Optional[str] = Field(None, alias="reviewNote", description="Most recent reviewer note")
    updated_at: str = Field(..., alias="updatedAt", description="Audit trail for post-approval drift (D2)")
    admin_edits: List[AdminEdit] = Field(default_factory=list, alias="adminEdits", description="Admin edit log (D13)")


class AdminListingsResponse(BaseModel):
    """Rows for the admin Review queue / Listings tables, plus the nav pending count."""

    model_config = ConfigDict(populate_by_name=True)

    listings: List[AdminListingRow] = Field(default_factory=list, description="Matching listings")
    pending_count: int = Field(
        0, alias="pendingCount", description="Submissions awaiting review; badges the admin nav (D2)"
    )


class PublisherCreateRequest(BaseModel):
    """Create an admin-managed publisher profile (D12)."""

    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(..., min_length=1, description="Display name")
    kind: PublisherKind = Field("department", description="institution | department | individual")
    verified: bool = Field(False, description="Admin-only check mark")
    icon_key: Optional[str] = Field(None, alias="iconKey", description="S3 object key")
    order: int = Field(0, description="Sort order")
    enabled: bool = Field(True, description="Whether the profile may be proposed or assigned")


class PublisherUpdateRequest(BaseModel):
    """Update a publisher profile (D12). All fields optional."""

    model_config = ConfigDict(populate_by_name=True)

    label: Optional[str] = Field(None, min_length=1, description="Display name")
    kind: Optional[PublisherKind] = Field(None, description="institution | department | individual")
    verified: Optional[bool] = Field(None, description="Admin-only check mark")
    icon_key: Optional[str] = Field(None, alias="iconKey", description="S3 object key")
    order: Optional[int] = Field(None, description="Sort order")
    enabled: Optional[bool] = Field(None, description="Whether the profile may be proposed or assigned")


class PublishersResponse(BaseModel):
    """All publisher profiles, ordered."""

    model_config = ConfigDict(populate_by_name=True)

    publishers: List[PublisherProfile] = Field(default_factory=list, description="Publisher profiles")


class PublisherEligibilityRequest(BaseModel):
    """Replace the set of users who may *propose* a publisher (D12).

    An allowlist for the submit dialog only. An admin may set any publisher on any
    listing regardless of eligibility, so this never appears in an access check.
    """

    model_config = ConfigDict(populate_by_name=True)

    user_ids: List[str] = Field(
        default_factory=list, alias="userIds", description="Users who may propose this publisher at submission"
    )


class PublisherEligibilityResponse(BaseModel):
    """Who may currently propose a publisher (D12)."""

    model_config = ConfigDict(populate_by_name=True)

    publisher_id: str = Field(..., alias="publisherId", description="Publisher identifier")
    user_ids: List[str] = Field(default_factory=list, alias="userIds", description="Eligible user ids")


class AgentCategory(BaseModel):
    """An admin-managed store category (D10).

    ⚠️ ``id`` is immutable — it is half of ``GSI5_PK = LISTED#{category}``, so renaming
    it would strand every published listing in a partition browse no longer queries.
    Renaming a category changes ``label`` only.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="Immutable category id; the GSI5 partition suffix")
    label: str = Field(..., description="Display name, renameable")
    order: int = Field(0, description="Browse order")
    enabled: bool = Field(True, description="Disabled categories hide from pickers and browse")
    created_at: Optional[str] = Field(None, alias="createdAt", description="ISO 8601 creation timestamp")
    updated_at: Optional[str] = Field(None, alias="updatedAt", description="ISO 8601 update timestamp")


class AgentCategoryCreateRequest(BaseModel):
    """Create a store category (D10)."""

    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(..., min_length=1, max_length=60, description="Display name")
    id: Optional[str] = Field(
        None, description="Optional explicit id; defaults to the label. Immutable once set."
    )
    order: int = Field(0, description="Browse order")
    enabled: bool = Field(True, description="Whether the category is offered")


class AgentCategoryUpdateRequest(BaseModel):
    """Update a store category. ``id`` is deliberately absent — it cannot change."""

    model_config = ConfigDict(populate_by_name=True)

    label: Optional[str] = Field(None, min_length=1, max_length=60, description="Display name")
    order: Optional[int] = Field(None, description="Browse order")
    enabled: Optional[bool] = Field(None, description="Whether the category is offered")


class AgentCategoriesResponse(BaseModel):
    """The category set a listing may reference (D10)."""

    model_config = ConfigDict(populate_by_name=True)

    categories: List[AgentCategory] = Field(default_factory=list, description="Categories in browse order")


class ListingPublisher(BaseModel):
    """The publisher as the store renders it — label, kind, and the verified mark.

    A narrowed projection of ``PublisherProfile``: browse has no use for ``order``,
    ``enabled`` or timestamps, and the less the store read carries the less there is to
    leak.
    """

    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(..., description="Display name")
    kind: PublisherKind = Field(..., description="institution | department | individual")
    verified: bool = Field(False, description="Check mark: a university team stands behind this")


class AgentListingResponse(BaseModel):
    """The shelf read-shape (D4).

    ⚠️ Deliberately **not** ``AgentResponse``. This carries no ``instructions``, no
    binding ``ref`` values, no binding ids, and no ``ownerId`` — a store row's job is to
    make you tap, and everything else is an unnecessary disclosure to every browsing
    user. The detail read (Phase 3) adds description, starters and capability *names*,
    and gates ``instructions`` to owner/editor.
    """

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    name: str = Field(..., description="Agent display name")
    tagline: Optional[str] = Field(None, description="One-line subtitle (D4)")
    emoji: Optional[str] = Field(None, description="Emoji, for the generated icon fallback (D5)")
    icon_url: Optional[str] = Field(
        None,
        alias="iconUrl",
        description="Square icon URL; absent until icons ship in Phase 4, then the SPA falls back to the generated gradient",
    )
    publisher: Optional[ListingPublisher] = Field(None, description="Attribution (D12), display only")
    category: str = Field(..., description="Category id")


class AgentStoreResponse(BaseModel):
    """One page of a category's shelf, newest-first."""

    model_config = ConfigDict(populate_by_name=True)

    listings: List[AgentListingResponse] = Field(default_factory=list, description="Published agents")
    next_cursor: Optional[str] = Field(
        None, alias="nextCursor", description="Opaque pagination cursor; absent at the end of the shelf"
    )


class AgentStoreFrontResponse(BaseModel):
    """The browse header: the featured row plus the categories to render (D10)."""

    model_config = ConfigDict(populate_by_name=True)

    featured: List[AgentListingResponse] = Field(
        default_factory=list,
        description="Curated store-front row; empty until the store-front admin ships in Phase 5",
    )
    categories: List[AgentCategory] = Field(
        default_factory=list, description="Enabled categories in browse order"
    )


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
