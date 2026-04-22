"""OAuth provider administration.

Providers are registered and authenticated against AWS Bedrock AgentCore
Identity. This module exposes the provider metadata model, the DynamoDB
repository, and the AgentCore registrar used by admin CRUD routes.
"""

from .agentcore_registrar import (
    AgentCoreRegistrar,
    CredentialProviderConflictError,
    CredentialProviderInfo,
    CredentialProviderNotFoundError,
    InvalidCustomProviderConfigError,
    get_agentcore_registrar,
)
from .models import (
    OAuthProvider,
    OAuthProviderCreate,
    OAuthProviderListResponse,
    OAuthProviderResponse,
    OAuthProviderType,
    OAuthProviderUpdate,
    OAuthRequiredEvent,
    compute_scopes_hash,
)
from .provider_repository import (
    OAuthProviderRepository,
    get_provider_repository,
)

__all__ = [
    "OAuthProviderType",
    "OAuthProvider",
    "OAuthProviderCreate",
    "OAuthProviderUpdate",
    "OAuthProviderResponse",
    "OAuthProviderListResponse",
    "OAuthRequiredEvent",
    "compute_scopes_hash",
    "OAuthProviderRepository",
    "get_provider_repository",
    "AgentCoreRegistrar",
    "CredentialProviderInfo",
    "CredentialProviderConflictError",
    "CredentialProviderNotFoundError",
    "InvalidCustomProviderConfigError",
    "get_agentcore_registrar",
]
