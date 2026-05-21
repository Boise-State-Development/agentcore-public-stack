"""File-source adapter contract.

An adapter is the per-provider code that makes a connector usable as a file
source. It is bound to a connector by an admin (the connector record stores
the adapter's `key`), and it implements a uniform browse/search/download
contract so the rest of the system stays provider-agnostic.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple

from apis.shared.oauth.models import OAuthProviderType

from apis.app_api.file_sources.models import (
    BrowseResult,
    DownloadedFile,
    SourceRoot,
)


@dataclass(frozen=True)
class AdapterMetadata:
    """Static, code-defined description of an adapter.

    Surfaced read-only to the admin UI so an admin can map a connector to an
    adapter from a dropdown. `compatible_provider_types` constrains which
    connectors an adapter may be mapped to; `required_scopes` lets the admin
    form warn when a connector's OAuth scopes don't cover the adapter.
    """

    key: str
    display_name: str
    icon: str
    compatible_provider_types: Tuple[OAuthProviderType, ...]
    required_scopes: Tuple[str, ...]


class FileSourceAdapter(ABC):
    """Provider-specific implementation of the file-source contract.

    All methods receive an already-resolved OAuth access token for the
    importing user — adapters never deal with token acquisition or refresh.
    """

    @property
    @abstractmethod
    def metadata(self) -> AdapterMetadata:
        """Return this adapter's static metadata."""

    @abstractmethod
    async def list_roots(self, access_token: str) -> List[SourceRoot]:
        """Return the top-level browsing roots the user can see."""

    @abstractmethod
    async def browse(
        self, access_token: str, folder_id: str, cursor: Optional[str] = None
    ) -> BrowseResult:
        """List the contents of a folder (or root), one page at a time."""

    @abstractmethod
    async def search(
        self, access_token: str, query: str, cursor: Optional[str] = None
    ) -> BrowseResult:
        """Search the source by free-text query, one page at a time."""

    @abstractmethod
    async def download(self, access_token: str, file_id: str) -> DownloadedFile:
        """Fetch a file's bytes, exporting provider-native docs as needed."""
