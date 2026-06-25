"""Export-target adapter contract.

An adapter is the per-provider code that makes a connector usable as a
*destination* — somewhere a rendered document (e.g. a conversation transcript)
can be written. It is bound to a connector by an admin (the connector record
stores the adapter's `key` in `export_target_adapter_id`) and implements a
uniform create/list contract so the rest of the system stays provider-agnostic.

The write-direction mirror of `file_sources.adapter.FileSourceAdapter`.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple

from apis.shared.oauth.models import OAuthProviderType

from apis.app_api.export_targets.models import (
    CreatedFile,
    ExportDestination,
    ExportFormat,
)


@dataclass(frozen=True)
class ExportTargetMetadata:
    """Static, code-defined description of an export-target adapter.

    Surfaced read-only to the admin UI so an admin can map a connector to an
    adapter from a dropdown. `compatible_provider_types` constrains which
    connectors an adapter may be mapped to; `required_scopes` lets the admin
    form warn when a connector's OAuth scopes don't cover the adapter (a
    write scope, e.g. Drive's `drive.file`); `supported_formats` tells the
    SPA which output formats to offer for this destination.
    """

    key: str
    display_name: str
    icon: str
    compatible_provider_types: Tuple[OAuthProviderType, ...]
    required_scopes: Tuple[str, ...]
    supported_formats: Tuple[ExportFormat, ...]


class ExportTargetAdapter(ABC):
    """Provider-specific implementation of the export-target contract.

    All methods receive an already-resolved OAuth access token for the
    exporting user — adapters never deal with token acquisition or refresh.
    """

    @property
    @abstractmethod
    def metadata(self) -> ExportTargetMetadata:
        """Return this adapter's static metadata."""

    @abstractmethod
    async def list_destinations(self, access_token: str) -> List[ExportDestination]:
        """Return the top-level write locations the user can save into."""

    @abstractmethod
    async def create_document(
        self,
        access_token: str,
        *,
        content: bytes,
        name: str,
        source_mime_type: str,
        target_format: ExportFormat,
        parent_id: Optional[str] = None,
    ) -> CreatedFile:
        """Create a document at the destination and return its identity.

        `content`/`source_mime_type` are the rendered bytes and their MIME
        type (e.g. `text/html`); `target_format` is how the file should land
        (e.g. a native Google Doc converted from that HTML). `parent_id` is an
        optional destination folder — None means the destination's default
        location.
        """
