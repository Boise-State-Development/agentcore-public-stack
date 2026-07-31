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
# ``withdrawal_requested`` is a *live* state: the author has asked to pull the listing and an
# admin has not yet agreed, so it is still on the shelf (see ``listing.LISTED_STATES``).
ListingState = Literal[
    "private",
    "in_review",
    "published",
    "changes_requested",
    "taken_down",
    "withdrawal_requested",
]
PublisherKind = Literal["institution", "department", "individual"]

# Agent Marketplace Phase 8 (D15): problem reports. A small fixed set so the queue can be
# sorted by severity without reading every note — ``inappropriate`` is the one that should
# page a human rather than wait for a sweep.
ReportReason = Literal["inaccurate", "broken", "inappropriate", "other"]

# Deliberately NOT a mirror of ``ListingState``: a report is a note *about* an Agent, not
# a state *of* it. Resolving one never changes ``listing.state`` (D15.5) — if a report
# warrants delisting, the admin takes the Agent down and that is a separate recorded act.
ReportState = Literal["open", "resolved", "dismissed"]

# ``ListingDrift`` lived here: a marker for a published listing whose instructions no longer
# matched the hash recorded at approval. It is **gone rather than dormant**, because the
# condition it detected is now structurally impossible — the store serves an immutable
# ``AgentVersion``, so an author's post-approval edit cannot reach a published listing at all
# (see ``assistants.versions``). A governance marker that can never fire is worse than none:
# the next reader assumes it is doing something. Removed with `approvedInstructionsHash`.

# Who can actually *open* a listed Agent — `visibility` projected onto the question the
# store never asks.
#
# **Publication and reachability are separate axes, and the store only guards one of them.**
# `GET /agents/store` is a pure sparse-GSI5 read with no access check, while
# `GET /agents/{id}` enforces `get_assistant_with_access_check`. Nothing in the listing
# lifecycle touches `visibility` — deliberately, since approving a store listing is not
# consent to widen access — so a PRIVATE or SHARED Agent can be approved and shelved, and
# every user who is not the owner (or on the share list) gets a tile that 404s when tapped.
#
# Both live dev listings were in exactly this state when this was found: one PRIVATE, one
# SHARED. The admin copy half-knew ("members can only open one they could reach on their
# own"), but every guard was on listing *state*, never on visibility, and neither the
# author at submit nor the reviewer at approve was told.
#
# ⚠️ **This is a warning, not a gate.** SHARED-to-a-team is a legitimate thing to publish,
# and blocking it — or silently flipping visibility on approve — would be the
# `allowedAppRoles` trap again: a store decision quietly rewriting an access decision.
# Derived on read, never stored, so it cannot go stale against `visibility`.
ListingReachability = Literal["everyone", "shared_only", "owner_only"]

# Severity order for the queue sweep (D15). ``inappropriate`` first for the reason above.
REPORT_REASON_SEVERITY: Dict[str, int] = {
    "inappropriate": 0,
    "broken": 1,
    "inaccurate": 2,
    "other": 3,
}


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
    withdrawal_requested_at: Optional[str] = Field(
        None,
        alias="withdrawalRequestedAt",
        description=(
            "ISO 8601 when the author asked to pull a live listing (§5.1). Kept after the "
            "decision rather than cleared, so the author's card can say what happened and "
            "when — a request that vanishes on decline reads as though it was never made."
        ),
    )
    withdrawal_from: Optional[ListingState] = Field(
        None,
        alias="withdrawalFrom",
        description=(
            "The state a pending withdrawal request came *from*, so declining it restores "
            "exactly that rather than assuming ``published``.\n\n"
            "Two states can be on the shelf and therefore reach ``withdrawal_requested``: "
            "``published``, and a ``changes_requested`` listing that was published before the "
            "admin sent it back (``review_listing`` deliberately does not unpublish). Without "
            "this field, declining the second one would land it in ``published`` and silently "
            "discard the outstanding change request — and, worse, it would make "
            "``withdrawal_requested → published`` reachable from a listing that had never been "
            "approved. Recording the origin is what keeps ``ALLOWED_TRANSITIONS`` provably "
            "free of a second door into the store."
        ),
    )
    submitted_version: Optional[int] = Field(
        None,
        alias="submittedVersion",
        description=(
            "The ``AgentVersion`` cut at submission — what the reviewer is looking at. "
            "Approval promotes exactly this number, rather than inferring 'the latest', so "
            "any other write that cuts a version (an admin presentation edit, §6.2) cannot "
            "change what a pending review resolves to."
        ),
    )
    published_version: Optional[int] = Field(
        None,
        alias="publishedVersion",
        description=(
            "The ``AgentVersion`` number this listing serves — the snapshot approval blessed. "
            "``None`` means nothing is published. This is the pointer, but not the gate: the "
            "store index lives on the version row itself (``set_version_index``), so a listing "
            "whose pointer went stale still cannot serve draft content."
        ),
    )


class AgentVersion(BaseModel):
    """An immutable snapshot of an Agent's reviewable surface (version-snapshots §3.1).

    Cut at **submission**, not at approval: taking it at approval leaves a window where
    the author edits between the reviewer reading and the reviewer approving, which is a
    narrower instance of the very bug versioning exists to close. Approval then promotes
    an existing version rather than capturing a new one.

    **What is in here is exactly what determines behavior or presentation.** Instructions
    are the obvious one, but swapping a bound tool or skill changes behavior just as much,
    and the model choice changes cost and answer quality — so ``bindings`` and
    ``modelSettings`` are frozen alongside. ``name``/``description``/``tagline``/``emoji``/
    ``iconKey``/``starters`` are what the reviewer actually read on the shelf, and
    ``category``/``publisherId`` are the placement and attribution they approved.

    ⚠️ **Deliberately absent: ``ownerId``, ``visibility``, ``status``.** Ownership governs
    edit rights and ``visibility`` is the independent access gate the marketplace spec is
    emphatic about keeping separate from ``listing.state``. Freezing either into a version
    would fuse two axes this codebase has worked to keep apart — a snapshot could then
    out-vote a later access decision, which is precisely the ``allowedAppRoles`` trap.
    ``status`` is a draft/complete lifecycle flag on the author's record, not a property
    of a reviewed artifact.

    Every optional field mirrors ``Assistant``'s own optionality rather than defaulting to
    an empty container, because absent and empty are different claims there: absent
    ``bindings`` means "synthesize the legacy KB binding via compat", ``[]`` means "binds
    nothing". Collapsing them would silently change what a legacy Agent resolves to on the
    round trip back out (see ``assistants.versions``).

    ``extra="allow"`` mirrors ``AgentListing``: a field written by newer code survives a
    read/write round trip through older code.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    agent_id: str = Field(..., alias="agentId", description="Agent this version snapshots")
    version: Optional[int] = Field(
        None,
        description=(
            "Monotonic version number within the agent, 1-based. ``None`` means the "
            "snapshot has not been persisted yet — the repository allocates the number at "
            "write time, because only the conditional write can settle a race between two "
            "concurrent submissions."
        ),
    )
    created_at: Optional[str] = Field(
        None, alias="createdAt", description="ISO 8601 timestamp of when this version was cut"
    )
    created_by: Optional[str] = Field(
        None,
        alias="createdBy",
        description=(
            "User id of whoever cut it. Audit attribution, never authorization — an admin "
            "presentation edit cuts a version attributed to the admin (§6.2), and that must "
            "not read as a transfer of ownership."
        ),
    )

    # ── the frozen surface ───────────────────────────────────────────────────────────
    name: str = Field(..., description="Agent display name as reviewed")
    description: str = Field(..., description="Short summary as reviewed")
    instructions: str = Field(..., description="System prompt as reviewed")
    tagline: Optional[str] = Field(None, description="Shelf subtitle (D4) as reviewed")
    emoji: Optional[str] = Field(None, description="Emoji avatar as reviewed")
    icon_key: Optional[str] = Field(
        None, alias="iconKey", description="S3 object key for the square icon (D5) as reviewed"
    )
    starters: Optional[List[str]] = Field(
        None, description="Conversation starters shown on the launch card; part of the reviewed presentation"
    )
    model_settings: Optional[AgentModelConfig] = Field(
        None, alias="modelConfig", description="Governed single-select model (D3) as reviewed"
    )
    bindings: Optional[List[AgentBinding]] = Field(
        None, description="Uniform primitive bindings (D3) as reviewed; absent ≠ empty (see class docstring)"
    )
    category: Optional[str] = Field(
        None, description="Category id the reviewer approved this into; absent when the Agent carried no listing"
    )
    publisher_id: Optional[str] = Field(
        None,
        alias="publisherId",
        description=(
            "PublisherProfile the reviewer approved this attribution to (D12). DISPLAY ONLY, "
            "exactly as on ``AgentListing`` — freezing it never makes it an access grant."
        ),
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


# Two states, not three. The Marketplace spec's D6 sketched a middle "limits"
# (degraded — runs, but something it declares is missing), and it was never
# reachable: it required a binding to carry ``config.optional``, which no API
# accepted and no Designer control set. It was also the wrong model to build
# toward, because it contradicts the Designer spec's D5 — "No **downgrade** on
# missing capability (block-only v1)" — which is the rule
# ``agent_binding_resolver`` actually implements, raising for every kind. A
# preview that offered a third outcome the runtime cannot produce would be a
# preview that lies. Removed in #747; D6 records why.
RunnabilityState = Literal["ready", "blocked"]


class MissingCapability(BaseModel):
    """A capability the viewer cannot reach, named for the D6 availability line."""

    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(..., description="Display name of the unavailable primitive")
    kind: str = Field(..., description="'model' or a binding kind")


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
    icon_url: Optional[str] = Field(
        None,
        alias="iconUrl",
        description=(
            "Where to render the icon from (Phase 4). Derived from ``iconKey``, never stored: a "
            "relative app-api path carrying the key's digest as ``?v=``, so it caches immutably "
            "and busts the moment a new icon is uploaded. Absent → the generated gradient (D5)."
        ),
    )
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
    make_public: bool = Field(
        False,
        alias="makePublic",
        description=(
            "The author's explicit consent to widen this Agent's visibility to PUBLIC as "
            "part of publishing it. The marketplace is public-only, and every new Agent "
            "starts PRIVATE, so without this the common path is a dead end: the author is "
            "told to go set visibility on a different screen and come back. Defaulting to "
            "``False`` is what keeps this consent rather than a side door — an omitted "
            "flag is refused exactly as before, so a direct API caller cannot widen an "
            "Agent's access by accident."
        ),
    )
    tagline: Optional[str] = Field(
        None,
        max_length=80,
        description=(
            "Shelf subtitle (D4). Submission is where an author sets this, because it is "
            "the moment the shelf row comes into existence and the only point they are "
            "looking at what the store will say. Omit to leave the existing tagline "
            "untouched; the SPA prefills it from the description's first clause so a "
            "legacy Agent is not asked for a field it has never had."
        ),
    )


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


class ListingPreflightResponse(BaseModel):
    """What the submit dialog shows before the author commits (D7).

    The same checks ``submit_listing`` runs, answered without a transition: the skills
    publication would expose, the memory-space block if there is one, and whether the
    author has to consent to going public.

    ⚠️ ``blockReason`` and ``requiresPublic`` are deliberately **separate signals**, not
    one field. A block is "leave this dialog and go fix something"; ``requiresPublic`` is
    "tick the box and I will handle it". Folding the second into the first is what made
    the common path a dead end — every new Agent starts PRIVATE, so the author was being
    bounced to another screen on their very first submission.
    """

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    exposed_skills: List[SkillExposure] = Field(
        default_factory=list,
        alias="exposedSkills",
        description="Skills the author wrote that publication would make readable (D7.1)",
    )
    block_reason: Optional[str] = Field(
        None,
        alias="blockReason",
        description=(
            "Why this agent cannot be submitted at all (D7.2), and cannot be fixed from "
            "the dialog; null when it can. Submit is hidden, not merely disabled."
        ),
    )
    requires_public: bool = Field(
        False,
        alias="requiresPublic",
        description=(
            "This Agent is not PUBLIC yet, so submitting must widen it. The dialog shows "
            "the consent control and sends ``makePublic``; it is not a block."
        ),
    )
    reachability: ListingReachability = Field(
        ...,
        description=(
            "Who would be able to open this Agent once shelved (see ``ListingReachability``). "
            "Advisory: publishing a PRIVATE/SHARED Agent is allowed, but the author should "
            "know the tile will 404 for everyone it is not shared with."
        ),
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


class VersionFieldChange(BaseModel):
    """One field that differs between the approved snapshot and the pending one (§6.1).

    ``before``/``after`` are the raw snapshot values, JSON-serialized as stored. The SPA
    renders them per field — a tagline as text, ``bindings`` as a list of kinds and refs —
    so this stays a transport shape rather than a pre-rendered string, which would put
    presentation decisions on the wrong side of the wire.
    """

    model_config = ConfigDict(populate_by_name=True)

    field: str = Field(..., description="Snapshot field name (camelCase, as the SPA knows it)")
    before: Optional[Any] = Field(
        None, description="Value in the published version; absent on a first submission"
    )
    after: Optional[Any] = Field(None, description="Value in the pending version")
    behavior: bool = Field(
        False,
        description=(
            "Whether this field changes what the Agent *does* (instructions, bindings, "
            "model) rather than how it presents. Drives the reviewer's at-a-glance triage."
        ),
    )


class AgentVersionSummary(BaseModel):
    """One snapshot, described just enough to pick it out of a list (§8 rollback).

    Deliberately not an ``AgentVersion``: the rollback picker renders a number, a name and a
    date, and sending the full snapshot would put every past version's ``instructions`` on
    the wire to draw a dropdown — the whole approval history of an Agent, to an admin who
    asked which versions exist.
    """

    model_config = ConfigDict(populate_by_name=True)

    version: int = Field(..., description="Version number, 1-based")
    name: Optional[str] = Field(None, description="Name as of this snapshot")
    tagline: Optional[str] = Field(None, description="Tagline as of this snapshot")
    created_at: Optional[str] = Field(
        None, alias="createdAt", description="ISO 8601 when the snapshot was cut"
    )
    created_by: Optional[str] = Field(
        None, alias="createdBy", description="Who cut it — the author, or an admin for a D13 edit"
    )
    is_published: bool = Field(
        False, alias="isPublished", description="Whether this is the snapshot the store serves"
    )


class AgentVersionsResponse(BaseModel):
    """Every snapshot an Agent has, newest first, plus which one is live."""

    model_config = ConfigDict(populate_by_name=True)

    versions: List[AgentVersionSummary] = Field(default_factory=list)
    published_version: Optional[int] = Field(
        None, alias="publishedVersion", description="The number the listing currently serves"
    )


class RollbackListingRequest(BaseModel):
    """Repoint a published listing at an earlier snapshot (§8)."""

    model_config = ConfigDict(populate_by_name=True)

    version: int = Field(..., description="The snapshot to make live")
    reason: str = Field(
        ...,
        min_length=1,
        description=(
            "Why — rendered on the author's card. Required for the same reason takedown's is: "
            "an admin changing what users run is something the author must be able to see."
        ),
    )


class AgentVersionDiffResponse(BaseModel):
    """The pending version against the currently published one (§6.1).

    Answers the reviewer's real question — "what changed since I approved this?" — which
    the queue could not answer before: a submission arrived with no reference to what it
    replaces, so a typo fix and a full rewrite looked identical.
    """

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    published_version: Optional[int] = Field(
        None, alias="publishedVersion", description="The live snapshot; absent on a first submission"
    )
    pending_version: Optional[int] = Field(
        None, alias="pendingVersion", description="The snapshot under review"
    )
    first_submission: bool = Field(
        False,
        alias="firstSubmission",
        description=(
            "Nothing is published yet, so there is nothing to diff against and the reviewer "
            "is reading the whole thing rather than a change. A distinct signal rather than "
            "an empty ``changes`` list, which would read as 'nothing changed'."
        ),
    )
    behavior_changed: bool = Field(
        False,
        alias="behaviorChanged",
        description=(
            "Whether instructions, bindings or the model differ — the one line that decides "
            "whether this is a seconds-long approval or a careful read."
        ),
    )
    changes: List[VersionFieldChange] = Field(
        default_factory=list, description="Differing fields, behavior first"
    )
    instructions_diff: List[str] = Field(
        default_factory=list,
        alias="instructionsDiff",
        description=(
            "Unified line diff of the instructions; empty when unchanged or on a first "
            "submission. Computed server-side so the field-level answer and the line-level "
            "one can never disagree."
        ),
    )


class WithdrawalDecisionRequest(BaseModel):
    """Admin grants or declines an author's withdrawal request (§5.1).

    ``grant``/``decline`` rather than ``approve``/``reject``: "approve" already means
    "publish this" everywhere else in this surface, and an admin approving a *withdrawal*
    reads dangerously like approving the listing.
    """

    model_config = ConfigDict(populate_by_name=True)

    decision: Literal["grant", "decline"] = Field(..., description="Grant the withdrawal, or decline it")
    note: Optional[str] = Field(
        None,
        max_length=2000,
        description=(
            "Reason, rendered on the author's card. They asked for something — a decline "
            "without a reason is the thing that generates a follow-up email."
        ),
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
    icon_url: Optional[str] = Field(
        None, alias="iconUrl", description="Where to render the icon from (Phase 4); absent → generated gradient"
    )
    owner_name: str = Field(..., alias="ownerName", description="Author display name — who to talk to about behavior")
    publisher: Optional[PublisherProfile] = Field(None, description="Resolved attribution (D12), display only")
    category: str = Field(..., description="Category id")
    state: ListingState = Field(..., description="Publication state")
    usage_count: int = Field(0, alias="usageCount", description="Chats — admin reporting only, never shelf furniture (D4)")
    submitted_at: Optional[str] = Field(None, alias="submittedAt", description="ISO 8601 submission timestamp")
    withdrawal_requested_at: Optional[str] = Field(
        None,
        alias="withdrawalRequestedAt",
        description=(
            "ISO 8601 timestamp of a pending withdrawal request. The queue shows submissions "
            "and withdrawal requests together (§5.1), and without this the two are "
            "indistinguishable: a request would read as 'submitted <the original date>' and "
            "an admin would answer the wrong question about it."
        ),
    )
    reviewed_at: Optional[str] = Field(None, alias="reviewedAt", description="ISO 8601 timestamp of the last review")
    review_note: Optional[str] = Field(None, alias="reviewNote", description="Most recent reviewer note")
    updated_at: str = Field(..., alias="updatedAt", description="ISO 8601 of the last write to the Agent record")
    latest_version: Optional[int] = Field(
        None,
        alias="latestVersion",
        description=(
            "The highest snapshot number this Agent has, which for 1-based sequential "
            "versions is also how many exist. Present so the Listings table can tell "
            "'serving v1' apart from 'only ever had v1' — the rollback affordance depends "
            "on whether *another* version exists, not on which one is currently live, and "
            "after a rollback to v1 those two readings disagree. Derived, never stored."
        ),
    )
    published_version: Optional[int] = Field(
        None,
        alias="publishedVersion",
        description=(
            "Which snapshot the store is serving. Replaces the old ``drift`` marker: the "
            "reviewer's question was 'has this changed since I approved it?', and the honest "
            "answer is now a version number rather than a heuristic — the author's edits live "
            "on the draft and reach nobody until a new version is approved."
        ),
    )
    reachability: ListingReachability = Field(
        ...,
        description=(
            "Who can open this Agent if it is shelved (see ``ListingReachability``). "
            "``everyone`` = PUBLIC; ``shared_only`` / ``owner_only`` mean the store tile "
            "will 404 for most users. Advisory — never a gate."
        ),
    )
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
        description="Square icon URL (D5); absent → the SPA renders the generated gradient",
    )
    publisher: Optional[ListingPublisher] = Field(None, description="Attribution (D12), display only")
    category: str = Field(..., description="Category id")


class AgentIconResponse(BaseModel):
    """The result of setting or clearing an Agent's square icon (D5, Phase 4).

    Both fields are ``None`` after a remove, which is the signal the SPA needs to drop
    back to the generated gradient without re-reading the agent.
    """

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    icon_key: Optional[str] = Field(
        None, alias="iconKey", description="S3 object key now on the record; None after a remove"
    )
    icon_url: Optional[str] = Field(
        None, alias="iconUrl", description="Where to render it from; None → the generated gradient"
    )


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
        description="Curated store-front row, in the admin's order (D10)",
    )
    categories: List[AgentCategory] = Field(
        default_factory=list, description="Enabled categories in browse order"
    )


# ─────────────────────────────────────────────────────── Agent Marketplace (Phase 5)
class PinnedAgentRef(BaseModel):
    """One entry in a user's own pin list — a pointer, never a copy (D8)."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    order: int = Field(0, description="Position within the user's own pins, ascending")
    pinned_at: Optional[str] = Field(None, alias="pinnedAt", description="ISO 8601 timestamp")


class UserPinState(BaseModel):
    """The whole per-user pin item (D9's user side).

    ``dismissed`` is a tombstone list, and it is the single most important detail in the
    pin design: role-seeded pins (Phase 6) are resolved live rather than materialized, so
    without a tombstone a seed re-applies on the next resolution and the user can never
    remove it. It is written from Phase 5 so the resolver that arrives with role pins has
    a history to respect rather than starting from a blank slate.
    """

    model_config = ConfigDict(populate_by_name=True)

    pinned: List[PinnedAgentRef] = Field(default_factory=list, description="The user's own pins")
    dismissed: List[str] = Field(
        default_factory=list, description="Agent ids the user removed — the resolver MUST respect these (D9.3)"
    )


class PinnedAgentResponse(BaseModel):
    """One row on the Pinned shelf.

    The shelf projection (D4) plus the two fields that describe *why* it is pinned.
    ``source`` and ``locked`` are always ``"user"``/``False`` in Phase 5 — role-seeded
    pins are Phase 6 — and they are on the contract now so that phase adds rows rather
    than changing the shape the SPA already renders.
    """

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    name: str = Field(..., description="Agent display name")
    tagline: Optional[str] = Field(None, description="One-line subtitle (D4)")
    emoji: Optional[str] = Field(None, description="Emoji, for the generated icon fallback (D5)")
    icon_url: Optional[str] = Field(
        None, alias="iconUrl", description="Square icon URL (D5); absent → the generated gradient"
    )
    publisher: Optional[ListingPublisher] = Field(None, description="Attribution (D12), display only")
    category: str = Field("", description="Category id; empty when the agent carries no listing")
    source: Literal["user", "role"] = Field(
        "user", description="Whose pin this is — 'role' arrives with default pins (D9)"
    )
    locked: bool = Field(
        False, description="A locked role pin cannot be dismissed (D9.4); always false in Phase 5"
    )
    pinned_at: Optional[str] = Field(None, alias="pinnedAt", description="ISO 8601 timestamp")


class AgentPinsResponse(BaseModel):
    """A user's effective pin list (D9)."""

    model_config = ConfigDict(populate_by_name=True)

    pins: List[PinnedAgentResponse] = Field(default_factory=list, description="Resolved pins, in order")


# ── role-seeded default pins (D9, Phase 6) ──────────────────────────────────────────
# ⚠️ A pin is **not** a permission. These models never touch ``EffectivePermissions`` and
# never enter ``_compute_effective_permissions``: role pins live in their own items, are
# resolved by their own query, and do not inherit through ``inheritsFrom``. Folding them
# into the permission payload would pollute a structure the model call path depends on.
class RoleAgentPin(BaseModel):
    """One default pin stored against an AppRole (``SK = AGENT_PIN#{agent_id}``)."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    order: int = Field(0, description="Position within this role's seed list, ascending")
    locked: bool = Field(
        False,
        description=(
            "A locked seed cannot be dismissed (D9.4) — the remove control is hidden and "
            "the dismissal endpoint no-ops. For the one Agent a role genuinely must keep."
        ),
    )
    created_at: Optional[str] = Field(None, alias="createdAt", description="ISO 8601 timestamp")
    created_by: Optional[str] = Field(None, alias="createdBy", description="Admin user id")


class RoleAgentPinInput(BaseModel):
    """One entry of a role's pin list as the admin console submits it."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    locked: bool = Field(False, description="Members cannot dismiss this one (D9.4)")


class RoleAgentPinsUpdateRequest(BaseModel):
    """Replace a role's default pins, in order (D9).

    A whole-list PUT for the same reason as the store front: ``order`` is a property of
    the list, not of any one row, so a half-applied reorder is an order nobody chose.
    """

    model_config = ConfigDict(populate_by_name=True)

    pins: List[RoleAgentPinInput] = Field(
        default_factory=list, alias="pins", description="Seeded agents, in shelf order"
    )


class RoleAgentPinRow(BaseModel):
    """One default pin as the admin console sees it: the shelf projection plus the D9.5 diff."""

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent identifier")
    name: str = Field(..., description="Agent display name")
    tagline: Optional[str] = Field(None, description="Shelf subtitle")
    emoji: Optional[str] = Field(None, description="Emoji, for the generated icon fallback (D5)")
    icon_url: Optional[str] = Field(None, alias="iconUrl", description="Square icon URL (D5)")
    publisher: Optional[ListingPublisher] = Field(None, description="Attribution (D12), display only")
    category: str = Field("", description="Category id; empty when the agent carries no listing")
    order: int = Field(0, description="Position in the seed list")
    locked: bool = Field(False, description="Members cannot dismiss this one (D9.4)")
    listing_state: Optional[ListingState] = Field(
        None, alias="listingState", description="Publication state; absent when never submitted"
    )
    reachable: bool = Field(
        True,
        description=(
            "Whether an ordinary member of the role could open this Agent at all. False for "
            "a PRIVATE agent (owner only) — the seed would silently resolve to nothing."
        ),
    )
    visibility: str = Field("PUBLIC", description="PRIVATE | SHARED | PUBLIC — why ``reachable`` reads as it does")
    state: str = Field(
        "ready", description="D9.5 assignment-time check against the ROLE's permissions: ready | limits | blocked"
    )
    missing: List[MissingCapability] = Field(
        default_factory=list, description="Capabilities this role does not grant (D9.5)"
    )
    notes: List[str] = Field(
        default_factory=list,
        description=(
            "What the role-level check could not decide — a memory-space binding resolves "
            "per person, so 'ready' here never claims to have checked it"
        ),
    )


class RoleAgentPinsResponse(BaseModel):
    """A role's default pins, plus the two facts that decide whether they reach anyone."""

    model_config = ConfigDict(populate_by_name=True)

    role_id: str = Field(..., alias="roleId", description="AppRole identifier")
    role_label: str = Field("", alias="roleLabel", description="Display name of the role")
    fallback_only: bool = Field(
        False,
        alias="fallbackOnly",
        description=(
            "⚠️ D9.6 — the ``default`` role is consulted ONLY for users who matched zero "
            "AppRoles, and is never merged alongside a matched role. Pins seeded here "
            "reach nobody who holds any other role."
        ),
    )
    unmapped: bool = Field(
        False,
        description="The role carries no jwtRoleMappings, so no user matches it at all",
    )
    locked_elsewhere: int = Field(
        0,
        alias="lockedElsewhere",
        description=(
            "Locked seeds held by OTHER roles (D9 open question, #748). Pins merge as a "
            "union across every role a user holds and a lock from any one of them wins, so "
            "the shelf an individual actually gets is this plus whatever this role locks. "
            "Reported rather than capped: role membership resolves per user from Entra "
            "claims, so the union is not knowable at write time."
        ),
    )
    locked_elsewhere_roles: int = Field(
        0,
        alias="lockedElsewhereRoles",
        description="How many other roles lock at least one seed — the spread behind lockedElsewhere",
    )
    pins: List[RoleAgentPinRow] = Field(default_factory=list, description="Seeded agents, in order")
    unavailable: List[str] = Field(
        default_factory=list,
        description=(
            "Configured ids that no longer resolve to an Agent — reported rather than "
            "pruned on read, so a GET never rewrites an admin's curation as a side effect"
        ),
    )


class StoreFrontUpdateRequest(BaseModel):
    """Replace the featured row, in order (D10).

    A whole-list PUT rather than per-item ``order`` fields: the row is short and
    reordering has to be atomic, so the ordered array *is* the record.
    """

    model_config = ConfigDict(populate_by_name=True)

    agent_ids: List[str] = Field(
        default_factory=list, alias="agentIds", description="Featured agent ids, in render order"
    )


class AdminStoreFrontResponse(BaseModel):
    """The featured row as the admin console sees it (D10)."""

    model_config = ConfigDict(populate_by_name=True)

    featured: List[AgentListingResponse] = Field(
        default_factory=list, description="Resolved featured rows, in the configured order"
    )
    unavailable: List[str] = Field(
        default_factory=list,
        description=(
            "Configured ids that are no longer published — surfaced rather than silently "
            "dropped, so an admin can see why the row is short and clear them"
        ),
    )


# ─────────────────────────────────────────────────────── Agent Marketplace (Phase 8)
# Problem reports (D15). ⚠️ A report is a **private message to the curator**. Nothing in
# this section is ever rendered to another browsing user, and no count derived from it may
# reach ``usageCount``, the store front, or any ordering — the moment report volume
# influences placement, reporting becomes a way to bury a competitor's Agent.
class AgentReport(BaseModel):
    """One problem report, stored as a child row of the Agent it concerns (D15).

    ``extra="allow"`` mirrors ``AgentListing``: a field written by newer code survives a
    read/write round trip through older code.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    report_id: str = Field(..., alias="reportId", description="Deterministic per (agent, reporter) — see reports.py")
    agent_id: str = Field(..., alias="agentId", description="Agent this report concerns")
    reporter_id: str = Field(
        ...,
        alias="reporterId",
        description=(
            "Who filed it. Visible to the admin and NEVER to the author (D15.2) — admins "
            "need identity to spot a brigade or a grudge; authors need the substance."
        ),
    )
    reporter_name: str = Field(..., alias="reporterName", description="Reporter display name, for the queue")
    reason: ReportReason = Field(..., description="Fixed-set reason, so the queue sorts by severity")
    note: Optional[str] = Field(None, description="The reporter's free text")
    state: ReportState = Field("open", description="open → resolved | dismissed (D15.5)")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601; the GSI6 sort key while open")
    updated_at: Optional[str] = Field(None, alias="updatedAt", description="ISO 8601 of the last write")
    resolved_at: Optional[str] = Field(None, alias="resolvedAt", description="ISO 8601 of the triage decision")
    resolved_by: Optional[str] = Field(None, alias="resolvedBy", description="Display name of the deciding admin")
    resolution_note: Optional[str] = Field(
        None,
        alias="resolutionNote",
        description=(
            "The admin's own note. ⚠️ Never forwarded to the author (D15.1) — when a report "
            "is actionable the admin uses request-changes or takedown, whose reason field is "
            "the author-facing channel."
        ),
    )


class SubmitReportRequest(BaseModel):
    """A user reports a problem with a published Agent (D15)."""

    model_config = ConfigDict(populate_by_name=True)

    reason: ReportReason = Field(..., description="Why it is being reported")
    note: Optional[str] = Field(
        None, max_length=2000, description="What is wrong, in the reporter's words"
    )


class SubmitReportResponse(BaseModel):
    """What the reporter is told back.

    Deliberately thin. The reporter learns their report was recorded and whether it
    *replaced* one they already had open (D15.4) — and nothing else. Queue position,
    other reports, and every admin field are the curator's, not theirs.
    """

    model_config = ConfigDict(populate_by_name=True)

    agent_id: str = Field(..., alias="agentId", description="Agent this report concerns")
    reason: ReportReason = Field(..., description="The reason as recorded")
    state: ReportState = Field(..., description="Always 'open' for a freshly filed report")
    created_at: str = Field(..., alias="createdAt", description="When this report was filed")
    replaced_existing: bool = Field(
        False,
        alias="replacedExisting",
        description=(
            "True when this updated the reporter's already-open report rather than adding "
            "one (D15.4) — so the UI can say 'we updated your report' instead of implying a "
            "second one is now queued."
        ),
    )


class ResolveReportRequest(BaseModel):
    """Admin triages a report: resolve it or dismiss it (D15.5)."""

    model_config = ConfigDict(populate_by_name=True)

    decision: Literal["resolve", "dismiss"] = Field(..., description="Triage outcome")
    note: Optional[str] = Field(
        None,
        max_length=2000,
        description="The admin's record of what they did. Never forwarded to the author (D15.1)",
    )


class AdminReportRow(BaseModel):
    """One row in the admin Reports queue (D10/D15).

    Carries the reporter (D15.2) and enough of the Agent to triage without a second
    fetch. ``listingState`` is present but **never** written by this surface: resolving a
    report does not change it (D15.5). It is here so an admin can see that the Agent they
    are reading about has already been taken down by someone else.
    """

    model_config = ConfigDict(populate_by_name=True)

    report_id: str = Field(..., alias="reportId", description="Report identifier, within its agent")
    agent_id: str = Field(..., alias="agentId", description="Agent this report concerns")
    agent_name: str = Field(..., alias="agentName", description="Agent display name at read time")
    emoji: Optional[str] = Field(None, description="Emoji, for the generated icon fallback (D5)")
    icon_url: Optional[str] = Field(None, alias="iconUrl", description="Where to render the icon from")
    owner_name: Optional[str] = Field(
        None, alias="ownerName", description="The author — who to talk to about behavior"
    )
    listing_state: Optional[ListingState] = Field(
        None,
        alias="listingState",
        description="The Agent's publication state, read-only context; resolving never changes it (D15.5)",
    )
    agent_missing: bool = Field(
        False,
        alias="agentMissing",
        description=(
            "The Agent this report concerns no longer exists. Reports are deleted with "
            "their Agent, so this is a repair affordance for a row orphaned by an older "
            "delete path — the admin can still dismiss it."
        ),
    )
    reporter_id: str = Field(..., alias="reporterId", description="Reporter user id (admin-only, D15.2)")
    reporter_name: str = Field(..., alias="reporterName", description="Reporter display name (admin-only, D15.2)")
    reason: ReportReason = Field(..., description="Fixed-set reason")
    note: Optional[str] = Field(None, description="The reporter's free text")
    state: ReportState = Field(..., description="open / resolved / dismissed")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 when it was filed")
    resolved_at: Optional[str] = Field(None, alias="resolvedAt", description="ISO 8601 of the decision")
    resolved_by: Optional[str] = Field(None, alias="resolvedBy", description="Deciding admin's display name")
    resolution_note: Optional[str] = Field(None, alias="resolutionNote", description="The admin's own note")


class AdminReportsResponse(BaseModel):
    """Rows for the admin Reports queue, plus the nav badge count (D10)."""

    model_config = ConfigDict(populate_by_name=True)

    reports: List[AdminReportRow] = Field(default_factory=list, description="Matching reports")
    open_count: int = Field(
        0,
        alias="openCount",
        description="Reports awaiting triage; badges the admin nav alongside submissions (D10)",
    )


class AdminQueueCountsResponse(BaseModel):
    """The two numbers D10 puts on the admin nav, without loading either queue.

    Exists because the badge has to be right on *every* admin page, and making the nav
    fetch two full queues to render two integers would put a table scan behind every
    click in the console.
    """

    model_config = ConfigDict(populate_by_name=True)

    pending_count: int = Field(
        0, alias="pendingCount", description="Submissions awaiting review (D2)"
    )
    open_report_count: int = Field(
        0, alias="openReportCount", description="Problem reports awaiting triage (D15)"
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
