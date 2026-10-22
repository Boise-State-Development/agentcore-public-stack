"""Admin API routes for agent template management.

Every endpoint requires the ``admin.agent_templates`` scope. Non-admin users
read the enabled catalog through the public ``GET /templates`` endpoint, which
returns the client ``TemplateDraft`` shape and never exposes the catalog/audit
fields.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.shared.auth import User, require_admin_scope
from apis.shared.agent_templates.models import (
    AgentTemplateAdminListResponse,
    AgentTemplateAdminResponse,
    AgentTemplateCreate,
    AgentTemplateUpdate,
)
from apis.shared.agent_templates.service import get_agent_templates_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent-templates", tags=["admin-agent-templates"])

# Every route in this package is guarded by this one scope, so the permission
# boundary is the package boundary. Enforced by
# tests/architecture/test_admin_scope_coverage.py.
require_agent_templates_admin = require_admin_scope("admin.agent_templates")


@router.get(
    "/",
    response_model=AgentTemplateAdminListResponse,
    summary="List all agent templates",
)
async def list_agent_templates(
    enabled_only: bool = Query(False, description="Filter to enabled templates only"),
    admin_user: User = Depends(require_agent_templates_admin),
) -> AgentTemplateAdminListResponse:
    """List all templates. Admin sees both enabled and disabled."""
    service = get_agent_templates_service()
    templates = await service.list_templates(enabled_only=enabled_only)
    return AgentTemplateAdminListResponse(
        templates=[AgentTemplateAdminResponse.from_template(t) for t in templates],
        total=len(templates),
    )


@router.get(
    "/{template_id}",
    response_model=AgentTemplateAdminResponse,
    summary="Get an agent template",
)
async def get_agent_template(
    template_id: str,
    admin_user: User = Depends(require_agent_templates_admin),
) -> AgentTemplateAdminResponse:
    service = get_agent_templates_service()
    template = await service.get_template(template_id)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent template '{template_id}' not found",
        )
    return AgentTemplateAdminResponse.from_template(template)


@router.post(
    "/",
    response_model=AgentTemplateAdminResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent template",
)
async def create_agent_template(
    data: AgentTemplateCreate,
    admin_user: User = Depends(require_agent_templates_admin),
) -> AgentTemplateAdminResponse:
    try:
        service = get_agent_templates_service()
        template = await service.create_template(data, created_by=admin_user.email)
        return AgentTemplateAdminResponse.from_template(template)
    except ValueError as e:
        # Duplicate id (slug collision) or validation failure.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.patch(
    "/{template_id}",
    response_model=AgentTemplateAdminResponse,
    summary="Update an agent template",
)
async def update_agent_template(
    template_id: str,
    updates: AgentTemplateUpdate,
    admin_user: User = Depends(require_agent_templates_admin),
) -> AgentTemplateAdminResponse:
    try:
        service = get_agent_templates_service()
        template = await service.update_template(template_id, updates)
        if not template:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent template '{template_id}' not found",
            )
        return AgentTemplateAdminResponse.from_template(template)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an agent template",
)
async def delete_agent_template(
    template_id: str,
    admin_user: User = Depends(require_agent_templates_admin),
) -> None:
    service = get_agent_templates_service()
    deleted = await service.delete_template(template_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent template '{template_id}' not found",
        )
