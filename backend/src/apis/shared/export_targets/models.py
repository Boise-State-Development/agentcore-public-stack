"""Normalized export-target domain models.

An export-target adapter translates a rendered document into a provider's
"create a file" API (Google Drive, etc.) behind a provider-agnostic contract,
so the rest of the system can offer a single generic "Save to…" action
regardless of which destination the user picks.

Mirrors `file_sources.models` for the write direction. Kept self-contained
(no import from `file_sources`) so the two capabilities stay decoupled even
when a single connector is mapped to both.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ExportFormat(str, Enum):
    """The document format an export produces.

    `GOOGLE_DOC` is a provider-native document the destination converts an
    uploaded HTML body into (so Markdown formatting maps to real styling).
    `MARKDOWN` and `PDF` are plain files uploaded as-is.
    """

    GOOGLE_DOC = "google_doc"
    MARKDOWN = "markdown"
    PDF = "pdf"


class ExportInclude(BaseModel):
    """Which conversation elements to include in an exported transcript.

    Surfaced as a checkbox group in the SPA's "Save to…" dialog. The two
    message flags are always on (the transcript itself); they exist so the
    contract is explicit and a future "redacted" mode has a place to live.
    Defaults match the dialog's default selection so the common case needs
    no body.
    """

    model_config = ConfigDict(populate_by_name=True)

    user_messages: bool = Field(True, alias="userMessages")
    assistant_messages: bool = Field(True, alias="assistantMessages")
    tool_calls: bool = Field(True, alias="toolCalls")
    images: bool = Field(True)
    citations: bool = Field(True)
    reasoning: bool = Field(False)
    timestamps: bool = Field(False)


class ExportDestination(BaseModel):
    """A top-level write location a destination exposes (e.g. My Drive).

    The optional folder picker lists these, then writes into the chosen one.
    Deliberately the same minimal shape as `file_sources.SourceRoot` without
    sharing the type — the two capabilities evolve independently.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="Provider-side opaque identifier")
    name: str = Field(..., description="Display name")


@dataclass
class CreatedFile:
    """The result of creating a document at the destination.

    `web_view_link` is surfaced to the SPA as an "Open in <destination>"
    affordance; it is None for providers that don't return a viewer URL.
    """

    file_id: str
    name: str
    web_view_link: Optional[str] = None


class ExportTargetError(Exception):
    """Base error raised by an export-target adapter when a provider call fails."""


class ExportTargetAuthError(ExportTargetError):
    """The access token was rejected or lacks the required scopes (401/403)."""


class ExportTargetNotFoundError(ExportTargetError):
    """The requested destination folder does not exist (404)."""
