"""Wire models for the knowledge base upgrade surface.

Field names are camelCase on the wire (``populate_by_name`` + aliases), matching
every other app_api surface the Angular client consumes.

The word "vector" appears nowhere in any user-facing string in this module, per
Requirement 23.6. It is fine in comments; it is not fine in ``message``.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

#: The derived, UI-facing phase. Deliberately NOT the record's ``migrationState``:
#: the client should not have to know that ``shadow``, ``verify`` and ``promote``
#: are all "working on it", nor that absence means legacy.
#:
#: ``none`` is the state that renders nothing at all (Requirement 23.1).
UpgradePhase = Literal["none", "available", "in_progress", "succeeded", "failed"]

#: Why a document will not be carried across. Requirement 21.4 requires an
#: unsupported format to be distinguishable from a processing failure, because the
#: user's next action differs: convert and re-upload, versus just retry.
DocumentIssueKind = Literal[
    "unsupported_format",
    "processing_failure",
    "still_processing",
    "being_removed",
]


class UpgradeProgress(BaseModel):
    """Non-blocking progress for the ``in_progress`` phase (Requirement 23.3)."""

    model_config = ConfigDict(populate_by_name=True)

    completed: int = Field(0, description="Documents carried across so far")
    total: int = Field(0, description="Documents in the snapshot being carried")
    skipped: int = Field(0, description="Documents the snapshot could not include")


class DocumentNotCarried(BaseModel):
    """A document the upgrade will not carry across (Requirement 21.1).

    Surfaced *before* the user commits, not after, so the choice to retry or
    accept the loss is made with the facts in hand.
    """

    model_config = ConfigDict(populate_by_name=True)

    document_id: str = Field(..., alias="documentId")
    filename: str
    status: str = Field(..., description="The stored processing status, verbatim")
    kind: DocumentIssueKind
    message: str = Field(
        ...,
        description="Plain-language explanation, safe to render directly",
    )
    retryable: bool = Field(
        ...,
        description="Whether re-processing this document could succeed as-is",
    )


class UpgradeStatusResponse(BaseModel):
    """Everything the card needs to render, in one round trip."""

    model_config = ConfigDict(populate_by_name=True)

    phase: UpgradePhase
    #: True only for an owner/editor looking at an ``available`` knowledge base.
    #: The client hides the control on this alone; the server re-checks on write,
    #: so a client that ignores it gains nothing (Requirement 23.7).
    can_upgrade: bool = Field(False, alias="canUpgrade")
    progress: Optional[UpgradeProgress] = None
    #: Plain-language failure reason for the ``failed`` phase (Requirement 23.5).
    reason: Optional[str] = None
    #: Whether the one-time success notice is still owed (Requirement 23.4).
    #: Never sticky: dismissing it sets a timestamp and this goes false forever.
    notice_pending: bool = Field(False, alias="noticePending")
    documents_not_carried: List[DocumentNotCarried] = Field(
        default_factory=list, alias="documentsNotCarried"
    )


class EnrollResponse(BaseModel):
    """Result of an enrol or retry."""

    model_config = ConfigDict(populate_by_name=True)

    phase: UpgradePhase
    #: True when this call is what started the upgrade, false when it found one
    #: already running. Both are successes — a double-click is not an error.
    started: bool
    message: str
