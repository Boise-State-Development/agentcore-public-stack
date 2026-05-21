"""Admin API routes for file-source adapter discovery.

Exposes the file-source adapter registry read-only so the admin connector
form can render a dropdown for mapping a connector to an adapter. The
registry is code-defined and immutable at runtime — adapters ship in
releases and are never created through this API.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth import User, require_admin

from apis.app_api.file_sources.registry import registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/file-source-adapters", tags=["admin-file-sources"])


class FileSourceAdapterInfo(BaseModel):
    """Read-only description of a registered file-source adapter."""

    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(..., description="Stable adapter key stored on a connector")
    display_name: str = Field(..., alias="displayName")
    icon: str = Field(..., description="Icon hint the admin UI maps to an asset")
    compatible_provider_types: List[str] = Field(
        ...,
        alias="compatibleProviderTypes",
        description="OAuth provider types this adapter may be mapped to",
    )
    required_scopes: List[str] = Field(
        ...,
        alias="requiredScopes",
        description="OAuth scopes the connector must grant for the adapter to work",
    )


class FileSourceAdapterListResponse(BaseModel):
    adapters: List[FileSourceAdapterInfo]


@router.get("/", response_model=FileSourceAdapterListResponse)
async def list_file_source_adapters(
    admin: User = Depends(require_admin),
) -> FileSourceAdapterListResponse:
    """List every file-source adapter shipped in this release. Admin only."""
    logger.info("Admin listing file-source adapters")
    adapters = [
        FileSourceAdapterInfo(
            key=a.metadata.key,
            displayName=a.metadata.display_name,
            icon=a.metadata.icon,
            compatibleProviderTypes=[
                pt.value for pt in a.metadata.compatible_provider_types
            ],
            requiredScopes=list(a.metadata.required_scopes),
        )
        for a in registry.all()
    ]
    return FileSourceAdapterListResponse(adapters=adapters)
