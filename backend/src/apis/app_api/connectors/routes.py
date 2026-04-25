"""User-facing connector routes.

Lets a signed-in user see which OAuth connectors are available to them
(role-filtered). Consent is initiated by the inference API, which has the
AgentCore Runtime workload context; this router is purely a data source.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from apis.shared.auth import User, get_current_user
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType
from apis.shared.oauth.provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)
from apis.shared.rbac.service import AppRoleService, get_app_role_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connectors", tags=["connectors"])


class UserConnector(BaseModel):
    """Connector as visible to a signed-in user.

    Drops admin-only fields (ARN, callback URL, role allow-list) and keeps
    only what the settings page needs to render.
    """

    provider_id: str
    display_name: str
    provider_type: OAuthProviderType
    icon_name: str
    icon_data: Optional[str] = None
    scopes: List[str]


class UserConnectorListResponse(BaseModel):
    connectors: List[UserConnector]


def _visible_to_user(provider: OAuthProvider, user_role_ids: List[str]) -> bool:
    """True when the user is allowed to use this connector.

    An empty `allowed_roles` list means unrestricted access. A non-empty
    list grants access to users who share at least one AppRole ID.
    """
    if not provider.enabled:
        return False
    if not provider.allowed_roles:
        return True
    return bool(set(provider.allowed_roles) & set(user_role_ids))


@router.get("/", response_model=UserConnectorListResponse)
async def list_connectors(
    current_user: User = Depends(get_current_user),
    provider_repo: OAuthProviderRepository = Depends(get_provider_repository),
    role_service: AppRoleService = Depends(get_app_role_service),
) -> UserConnectorListResponse:
    """List enabled connectors available to the current user."""
    permissions = await role_service.resolve_user_permissions(current_user)
    providers = await provider_repo.list_providers(enabled_only=True)
    visible = [p for p in providers if _visible_to_user(p, permissions.app_roles)]
    return UserConnectorListResponse(
        connectors=[
            UserConnector(
                provider_id=p.provider_id,
                display_name=p.display_name,
                provider_type=p.provider_type,
                icon_name=p.icon_name,
                icon_data=p.icon_data,
                scopes=p.scopes,
            )
            for p in visible
        ]
    )
