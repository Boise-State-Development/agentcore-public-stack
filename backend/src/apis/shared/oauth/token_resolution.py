"""Shared "token or consent URL?" query against AgentCore Identity.

Two places need to ask AgentCore the same question about a
(user, OAuth provider) pair:

  * ``OAuthConsentHook`` — at tool-call time, to gate a tool the model is
    about to run.
  * ``ExternalMCPIntegration.load_external_tools`` — at agent-build time,
    when an OAuth-gated MCP server refuses the pre-flight ``tools/list``
    because the in-process token cache is cold.

Both must ask with the **same** ``scopes`` and ``customParameters``.
AgentCore folds both into the token-vault key, so querying with a
different set looks up a *different* vault entry and hands back a consent
URL for a user who has already authorized — an infinite "please connect"
loop for a connector that is in fact connected. Reading the provider
record in one place is what keeps the two callers in agreement; see
``docs`` on the connector's admin-configured Custom OAuth Parameters.

The hook keeps its own injected-lookup structure (it memoizes per turn and
is unit-tested with fakes); this module is the path used by callers that
have no hook instance to borrow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, TypedDict

from apis.shared.oauth.agentcore_identity import (
    CallbackUrlUnavailableError,
    WorkloadTokenUnavailableError,
    custom_parameters_for,
    get_agentcore_identity_client,
)

logger = logging.getLogger(__name__)


class TokenOrConsent(TypedDict):
    """Exactly one of these is populated by AgentCore Identity."""

    token: Optional[str]
    url: Optional[str]


async def resolve_token_or_consent_url(
    provider_id: str,
    user_id: str,
    *,
    force_authentication: bool = False,
) -> Optional[TokenOrConsent]:
    """Ask AgentCore Identity for a vaulted token, else a consent URL.

    Returns ``{"token": ..., "url": ...}`` with exactly one side populated,
    or ``None`` on a hard error (missing workload token / callback URL /
    unexpected failure). ``None`` means "couldn't ask" — it is NOT the same
    as "user must consent", and callers must not prompt on it.

    Args:
        provider_id: Credential provider name registered with AgentCore
            Identity, as stored on the tool's ``requires_oauth_provider``.
        user_id: The platform user the token is federated for.
        force_authentication: Bypass the vault and force a fresh consent —
            used only for an explicit user disconnect.
    """
    from apis.shared.oauth.provider_repository import get_provider_repository

    try:
        provider = await get_provider_repository().get_provider(provider_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Failed to read OAuth provider record for provider=%s", provider_id
        )
        return None

    # A missing provider record is a misconfiguration, not a consent gap.
    # Returning None keeps the caller from prompting for a connector that
    # the admin has since deleted.
    if provider is None:
        logger.warning(
            "No OAuth provider record for provider=%s; cannot resolve a token",
            provider_id,
        )
        return None

    identity_client = get_agentcore_identity_client()
    try:
        result = await identity_client.get_token_for_user(
            provider_name=provider_id,
            scopes=provider.scopes or [],
            user_id=user_id,
            force_authentication=force_authentication,
            custom_parameters=custom_parameters_for(provider.custom_parameters),
        )
    except WorkloadTokenUnavailableError:
        logger.error(
            "No workload token on context for provider=%s — "
            "AgentCoreContextMiddleware may be misconfigured",
            provider_id,
        )
        return None
    except CallbackUrlUnavailableError as err:
        logger.error("No OAuth2 callback URL for provider=%s: %s", provider_id, err)
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Failed to fetch OAuth token for provider=%s", provider_id)
        return None

    return {"token": result.access_token, "url": result.authorization_url}
