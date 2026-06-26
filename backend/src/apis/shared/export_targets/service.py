"""Boundary-free export-target helpers shared across consumers.

Holds the parts of the export flow that have no FastAPI/HTTP coupling so both
app-api (`POST /sessions/{id}/export`) and the agent-side `save_conversation`
tool can use them. The FastAPI `HTTPException` mapping lives in the app-api
wrapper (`apis.app_api.export_targets.service`).
"""

import logging
from typing import List

from apis.shared.oauth.agentcore_identity import (
    TokenResult,
    custom_parameters_for,
    get_agentcore_identity_client,
)
from apis.shared.oauth.models import OAuthProvider

logger = logging.getLogger(__name__)


def connector_visible_to_user(
    provider: OAuthProvider, user_role_ids: List[str]
) -> bool:
    """True when an enabled connector is usable by a user with these roles.

    An empty `allowed_roles` list means unrestricted access; a non-empty list
    grants access to users who share at least one AppRole id. Mirrors the
    connector catalog's visibility rule.
    """
    if not provider.enabled:
        return False
    if not provider.allowed_roles:
        return True
    return bool(set(provider.allowed_roles) & set(user_role_ids))


async def resolve_export_target_token(
    provider: OAuthProvider, user_id: str
) -> TokenResult:
    """Fetch the user's OAuth token for an export-target connector.

    Returns a `TokenResult`: `access_token` is populated when the vault has a
    usable token, `authorization_url` when the user still needs to consent.

    `custom_parameters` is built with `force_authentication=True` so it matches
    the consent flow — AgentCore factors `customParameters` into whether
    `get_resource_oauth2_token` short-circuits to a vaulted token (see the
    file-source service for the full rationale). Pure read; `force_authentication`
    stays False on `get_token_for_user` itself.
    """
    identity = get_agentcore_identity_client()
    return await identity.get_token_for_user(
        provider_name=provider.provider_id,
        scopes=provider.scopes,
        user_id=user_id,
        custom_parameters=custom_parameters_for(
            provider.provider_type.value,
            provider.custom_parameters,
            force_authentication=True,
        ),
    )
