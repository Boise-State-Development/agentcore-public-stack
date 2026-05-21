"""Normalized file-source domain models.

A file-source adapter translates a provider's API (Google Drive, etc.) into
this provider-agnostic shape so the assistant editor can render a single
generic file browser regardless of which source the files come from.

The Pydantic models double as the API response contract for the browse/search
endpoints; `DownloadedFile` carries raw bytes and is internal only.
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class FileEntryType(str, Enum):
    """Whether a browse entry is a navigable folder or a selectable file."""

    FOLDER = "folder"
    FILE = "file"


class FileEntry(BaseModel):
    """A single folder or file returned by a browse/search call."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., description="Provider-side opaque identifier")
    name: str = Field(..., description="Display name")
    type: FileEntryType = Field(..., description="Folder or file")
    mime_type: Optional[str] = Field(None, alias="mimeType", description="Provider MIME type")
    size_bytes: Optional[int] = Field(None, alias="sizeBytes", description="File size in bytes, when known")
    modified_at: Optional[str] = Field(None, alias="modifiedAt", description="ISO 8601 last-modified timestamp")
    etag: Optional[str] = Field(None, description="Provider version stamp for change detection")
    # False for folders and for files that cannot be ingested (e.g. Google
    # Forms). The generic browser greys these out instead of allowing select.
    selectable: bool = Field(True, description="Whether the entry can be picked for import")


class Breadcrumb(BaseModel):
    """One hop in the folder path shown above the browser."""

    id: str
    name: str


class BrowseResult(BaseModel):
    """A page of folder contents or search results."""

    model_config = ConfigDict(populate_by_name=True)

    entries: List[FileEntry] = Field(default_factory=list)
    breadcrumbs: List[Breadcrumb] = Field(default_factory=list)
    # Opaque pagination cursor; None when there are no further pages.
    next_cursor: Optional[str] = Field(None, alias="nextCursor")


class SourceRoot(BaseModel):
    """A top-level browsing root a provider exposes.

    Providers don't share a single tree — Google Drive has My Drive, Shared
    with me, and N shared drives as distinct roots. The generic browser lets
    the user pick a root, then `browse` from there.
    """

    id: str
    name: str


@dataclass
class DownloadedFile:
    """Raw file bytes plus the effective filename/MIME after any export.

    For Google-native docs the filename and content type reflect the export
    format (e.g. a Google Doc downloads as a .docx), not the original.
    """

    content: bytes
    filename: str
    content_type: str


class FileSourceError(Exception):
    """Base error raised by a file-source adapter when a provider call fails."""


class FileSourceAuthError(FileSourceError):
    """The access token was rejected or lacks the required scopes (401/403)."""


class FileSourceNotFoundError(FileSourceError):
    """The requested file or folder does not exist (404)."""
