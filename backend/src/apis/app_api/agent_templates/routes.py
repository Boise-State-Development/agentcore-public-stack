"""User-facing read endpoint for agent templates.

Any signed-in user who can create an agent fetches the enabled templates to
render the "Start from a template" picker. The response is the client
``TemplateCatalogEntry[]`` shape (``{ draft, pitch }`` per entry, camelCase),
so the existing picker / reconcile / form-population path is unchanged — only
the picker's data *source* moves from the hardcoded array to this endpoint.

Returns ``[]`` (never an error) when the catalog is empty, so a fork that has
not authored any templates degrades to a sensible empty state.

Admin writes go through ``/admin/agent-templates``.
"""

import logging

from fastapi import APIRouter, Depends

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.agent_templates.models import (
    PublicTemplateListResponse,
    TemplateCatalogEntryResponse,
)
from apis.shared.agent_templates.service import get_agent_templates_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/templates", tags=["agent-templates"])


@router.get(
    "/",
    response_model=PublicTemplateListResponse,
    summary="List available agent templates",
)
async def list_enabled_agent_templates(
    current_user: User = Depends(get_current_user_from_session),
) -> PublicTemplateListResponse:
    """Return all enabled templates as picker catalog entries, in display order."""
    service = get_agent_templates_service()
    templates = await service.list_templates(enabled_only=True)
    return PublicTemplateListResponse(
        templates=[TemplateCatalogEntryResponse.from_template(t) for t in templates],
        total=len(templates),
    )
