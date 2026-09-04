"""Request/response models for the artifacts API.

Covers the render-token, session-list and content endpoints, plus the
artifact-sharing surface (`artifacts/shares.py`).

JSON casing is split by design and matches what already shipped: the
render-token / list / content models are snake_case (this domain's
original REST shape), while the sharing models are camelCase aliases to
mirror the conversation-sharing API the SPA share modal is adapted from.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RenderTokenRequest(BaseModel):
    version: int = Field(..., ge=1, description="Artifact version to render")
    session_id: Optional[str] = Field(
        default=None,
        validation_alias="sessionId",
        description="Originating chat session id — audit correlation only",
    )


class RenderTokenResponse(BaseModel):
    url: str = Field(
        ...,
        description="Artifact origin URL with the embedded render token "
        "(set directly as the iframe src)",
    )
    expires_at: str = Field(..., description="ISO-8601 UTC token expiry")


class ArtifactSummary(BaseModel):
    """One artifact's current HEAD, for the session artifacts list.

    Snake-case JSON to match this domain's existing REST shape
    (RenderTokenResponse.expires_at). The SPA normalizes both this and
    the camelCase live SSE `artifact` event into one client model.
    """

    artifact_id: str
    version: int
    title: str
    content_type: str
    updated_at: str
    created_at: Optional[str] = None
    produced_by_message_index: Optional[int] = Field(
        default=None,
        description="0-based index of the assistant message that produced "
        "or last updated this artifact, matching the messages endpoint's "
        "`msg-{session_id}-{index}` id. Null for artifacts written before "
        "linkage existed — the SPA falls back to the end-of-chat strip.",
    )


class ArtifactListResponse(BaseModel):
    artifacts: list[ArtifactSummary] = Field(default_factory=list)


class LibraryArtifact(BaseModel):
    """One artifact at its current HEAD, for the user-wide library page.

    Distinct from `ArtifactSummary` in cardinality, not just fields: the
    session list returns one row per *version* so the SPA can anchor a
    card under the turn that produced it, while the library returns one
    row per *artifact*. Carries `session_id` so the library can link back
    to the conversation that produced it — the summary has no need for it
    (the caller already supplied the session id).
    """

    artifact_id: str
    version: int
    title: str
    content_type: str
    created_at: str
    updated_at: str
    session_id: str


class ArtifactLibraryResponse(BaseModel):
    artifacts: list[LibraryArtifact] = Field(default_factory=list)


class ArtifactContentResponse(BaseModel):
    """Raw source of one artifact version, for the panel's code view.

    `content` is inert text the SPA highlights client-side — never
    executed. For Markdown artifacts the stored S3 object is a rendered
    HTML wrapper; the service unwraps it back to the authored Markdown
    and `content_type` is normalized to `text/markdown` accordingly.
    """

    content: str
    content_type: str
    version: int


# ---------------------------------------------------------------------
# Artifact sharing
# ---------------------------------------------------------------------


class CreateArtifactShareRequest(BaseModel):
    """Request body for sharing one artifact version.

    `version` is required and never defaults to the artifact's HEAD: a
    share pins one immutable version, and a pointer that moves under the
    recipient is a different feature with different consent semantics.
    """

    model_config = ConfigDict(populate_by_name=True)

    version: int = Field(
        ..., ge=1, description="Artifact version to share (never #HEAD)"
    )
    access_level: Literal["public", "specific"] = Field(
        ...,
        alias="accessLevel",
        description="'public' = any authenticated tenant user; "
        "'specific' = email allowlist",
    )
    allowed_emails: Optional[List[str]] = Field(
        default=None,
        alias="allowedEmails",
        description="Email addresses allowed to view "
        "(required when accessLevel is 'specific')",
    )

    @model_validator(mode="after")
    def validate_allowed_emails(self) -> "CreateArtifactShareRequest":
        if self.access_level == "specific" and not self.allowed_emails:
            raise ValueError(
                "allowed_emails is required when access_level is 'specific'"
            )
        return self


class UpdateArtifactShareRequest(BaseModel):
    """Request body for changing an existing share's access controls.

    The share target — `(artifact_id, version)` — is immutable; only who
    may view it can change.
    """

    model_config = ConfigDict(populate_by_name=True)

    access_level: Optional[Literal["public", "specific"]] = Field(
        default=None, alias="accessLevel", description="New access level"
    )
    allowed_emails: Optional[List[str]] = Field(
        default=None,
        alias="allowedEmails",
        description="Updated email allowlist",
    )

    @model_validator(mode="after")
    def validate_allowed_emails(self) -> "UpdateArtifactShareRequest":
        if self.access_level == "specific" and not self.allowed_emails:
            raise ValueError(
                "allowed_emails is required when access_level is 'specific'"
            )
        return self


class ArtifactShareResponse(BaseModel):
    """An artifact share, as its owner sees it.

    Owner-only: `allowedEmails` is other people's addresses, so this
    shape must never be returned on a recipient-facing route.
    """

    model_config = ConfigDict(populate_by_name=True)

    share_id: str = Field(..., alias="shareId")
    artifact_id: str = Field(..., alias="artifactId")
    version: int = Field(..., description="Pinned artifact version")
    owner_id: str = Field(..., alias="ownerId")
    access_level: Literal["public", "specific"] = Field(
        ..., alias="accessLevel"
    )
    allowed_emails: Optional[List[str]] = Field(
        default=None, alias="allowedEmails"
    )
    title: str = Field(default="", description="Denormalized artifact title")
    content_type: str = Field(default="", alias="contentType")
    created_at: str = Field(..., alias="createdAt")
    updated_at: Optional[str] = Field(default=None, alias="updatedAt")
    share_url: str = Field(
        ...,
        alias="shareUrl",
        description="SPA-relative recipient route for this share",
    )


class ArtifactShareListResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    shares: List[ArtifactShareResponse] = Field(default_factory=list)


class SharedArtifactResponse(BaseModel):
    """Recipient-facing share metadata. Never carries artifact content.

    Deliberately omits `ownerId`, `artifactId` and `allowedEmails`: a
    recipient needs enough to render a header and decide what chrome to
    show, not the owner's internal ids or the rest of the allowlist.
    """

    model_config = ConfigDict(populate_by_name=True)

    share_id: str = Field(..., alias="shareId")
    title: str = Field(default="")
    content_type: str = Field(default="", alias="contentType")
    version: int
    created_at: str = Field(..., alias="createdAt")
    owner_email: str = Field(
        ..., alias="ownerEmail", description="Who shared this"
    )
    can_download: bool = Field(
        default=True,
        alias="canDownload",
        description="Whether the recipient may save a local copy. The "
        "download path is the same render URL with ?download=1, so a "
        "recipient who can view can also download — surfaced as a field "
        "so a future policy can withhold it without a shape change.",
    )
