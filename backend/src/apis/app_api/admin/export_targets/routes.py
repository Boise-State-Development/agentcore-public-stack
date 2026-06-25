"""Admin API routes for export-target adapter discovery.

Exposes the export-target adapter registry read-only so the admin connector
form can render a dropdown for mapping a connector to a destination adapter.
The registry is code-defined and immutable at runtime — adapters ship in
releases and are never created through this API.

Mirror of `admin/file_sources/routes.py` for the write direction.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth import User, require_admin

from apis.app_api.export_targets.registry import registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/export-target-adapters", tags=["admin-export-targets"])


class ExportTargetAdapterInfo(BaseModel):
    """Read-only description of a registered export-target adapter."""

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
    supported_formats: List[str] = Field(
        ...,
        alias="supportedFormats",
        description="Output formats this destination can produce",
    )


class ExportTargetAdapterListResponse(BaseModel):
    adapters: List[ExportTargetAdapterInfo]


@router.get("/", response_model=ExportTargetAdapterListResponse)
async def list_export_target_adapters(
    admin: User = Depends(require_admin),
) -> ExportTargetAdapterListResponse:
    """List every export-target adapter shipped in this release. Admin only."""
    logger.info("Admin listing export-target adapters")
    adapters = [
        ExportTargetAdapterInfo(
            key=a.metadata.key,
            displayName=a.metadata.display_name,
            icon=a.metadata.icon,
            compatibleProviderTypes=[
                pt.value for pt in a.metadata.compatible_provider_types
            ],
            requiredScopes=list(a.metadata.required_scopes),
            supportedFormats=[fmt.value for fmt in a.metadata.supported_formats],
        )
        for a in registry.all()
    ]
    return ExportTargetAdapterListResponse(adapters=adapters)
