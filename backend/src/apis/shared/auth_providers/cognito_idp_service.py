"""Cognito Identity Provider management for federated OIDC providers.

Handles registering, updating, and deleting federated identity providers
in a Cognito User Pool, and updating the App Client's supported providers list.
"""

import logging
import os
from typing import Dict, Iterable, List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Default attribute mappings from Cognito attributes to OIDC claims
DEFAULT_ATTRIBUTE_MAPPING: Dict[str, str] = {
    "email": "email",
    "name": "name",
    "given_name": "given_name",
    "family_name": "family_name",
    "picture": "picture",
    "custom:provider_sub": "sub",
}


class CognitoIdentityProviderService:
    """Manages federated OIDC identity providers in a Cognito User Pool."""

    def __init__(
        self,
        user_pool_id: Optional[str] = None,
        app_client_id: Optional[str] = None,
        region: Optional[str] = None,
    ):
        self._user_pool_id = user_pool_id or os.getenv("COGNITO_USER_POOL_ID")
        self._app_client_id = app_client_id or os.getenv("COGNITO_APP_CLIENT_ID")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._user_pool_id and self._app_client_id)

        if not self._enabled:
            logger.warning(
                "COGNITO_USER_POOL_ID or COGNITO_APP_CLIENT_ID not set. "
                "Cognito identity provider service is disabled."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._client = session.client("cognito-idp", region_name=self._region)
        else:
            self._client = boto3.client("cognito-idp", region_name=self._region)

        logger.info(
            f"Initialized Cognito IdP service: pool={self._user_pool_id}, "
            f"client={self._app_client_id}"
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def create_identity_provider(
        self,
        provider_name: str,
        issuer_url: str,
        client_id: str,
        client_secret: str,
        scopes: str = "openid profile email",
        attribute_mapping: Optional[Dict[str, str]] = None,
    ) -> None:
        """Register an OIDC identity provider in the Cognito User Pool.

        Args:
            provider_name: Unique name for the provider within the pool.
            issuer_url: The OIDC issuer URL.
            client_id: The OIDC client ID.
            client_secret: The OIDC client secret.
            scopes: Space-separated scopes string.
            attribute_mapping: Custom attribute mapping (Cognito attr -> provider claim).
                Falls back to DEFAULT_ATTRIBUTE_MAPPING if not provided.

        Raises:
            ClientError: On Cognito API failure.
        """
        if not self._enabled:
            raise RuntimeError("Cognito identity provider service is not enabled")

        mapping = attribute_mapping or DEFAULT_ATTRIBUTE_MAPPING

        self._client.create_identity_provider(
            UserPoolId=self._user_pool_id,
            ProviderName=provider_name,
            ProviderType="OIDC",
            ProviderDetails={
                "client_id": client_id,
                "client_secret": client_secret,
                "authorize_scopes": scopes,
                "oidc_issuer": issuer_url,
                "attributes_request_method": "GET",
            },
            AttributeMapping=mapping,
        )
        logger.info(f"Created Cognito identity provider: {provider_name}")

    def update_identity_provider(
        self,
        provider_name: str,
        issuer_url: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scopes: Optional[str] = None,
        attribute_mapping: Optional[Dict[str, str]] = None,
    ) -> None:
        """Update an OIDC identity provider in the Cognito User Pool.

        Only provided (non-None) fields are updated. Builds updated
        ProviderDetails and/or AttributeMapping from the supplied values
        merged with the existing provider configuration.

        Args:
            provider_name: The provider name to update.
            issuer_url: Updated OIDC issuer URL.
            client_id: Updated OIDC client ID.
            client_secret: Updated OIDC client secret.
            scopes: Updated space-separated scopes string.
            attribute_mapping: Updated attribute mapping (replaces existing).

        Raises:
            ClientError: On Cognito API failure.
        """
        if not self._enabled:
            raise RuntimeError("Cognito identity provider service is not enabled")

        # Fetch current provider config to merge with updates
        resp = self._client.describe_identity_provider(
            UserPoolId=self._user_pool_id,
            ProviderName=provider_name,
        )
        current = resp["IdentityProvider"]
        current_details = current.get("ProviderDetails", {})

        # Build updated ProviderDetails by merging
        updated_details: Dict[str, str] = {}
        updated_details["oidc_issuer"] = issuer_url if issuer_url is not None else current_details.get("oidc_issuer", "")
        updated_details["client_id"] = client_id if client_id is not None else current_details.get("client_id", "")
        updated_details["client_secret"] = client_secret if client_secret is not None else current_details.get("client_secret", "")
        updated_details["authorize_scopes"] = scopes if scopes is not None else current_details.get("authorize_scopes", "openid profile email")
        updated_details["attributes_request_method"] = current_details.get("attributes_request_method", "GET")

        update_kwargs: dict = {
            "UserPoolId": self._user_pool_id,
            "ProviderName": provider_name,
            "ProviderDetails": updated_details,
        }

        if attribute_mapping is not None:
            update_kwargs["AttributeMapping"] = attribute_mapping

        self._client.update_identity_provider(**update_kwargs)
        logger.info(f"Updated Cognito identity provider: {provider_name}")

    def delete_identity_provider(self, provider_name: str) -> None:
        """Delete an identity provider from the Cognito User Pool.

        Handles 'not found' gracefully for idempotent deletes.

        Args:
            provider_name: The provider name to delete.
        """
        if not self._enabled:
            return

        try:
            self._client.delete_identity_provider(
                UserPoolId=self._user_pool_id,
                ProviderName=provider_name,
            )
            logger.info(f"Deleted Cognito identity provider: {provider_name}")
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("ResourceNotFoundException", "UnsupportedIdentityProviderException"):
                logger.warning(
                    f"Cognito identity provider '{provider_name}' not found during delete (idempotent)."
                )
            else:
                raise

    def get_supported_identity_providers(self) -> List[str]:
        """Get the current list of supported identity providers on the App Client.

        Returns:
            List of provider names (e.g. ['COGNITO', 'okta-prod']).
        """
        if not self._enabled:
            return []

        response = self._client.describe_user_pool_client(
            UserPoolId=self._user_pool_id,
            ClientId=self._app_client_id,
        )
        return response["UserPoolClient"].get("SupportedIdentityProviders", [])

    def add_provider_to_app_client(self, provider_name: str) -> None:
        """Add a provider to the App Client's SupportedIdentityProviders.

        Fetches the current client config, appends the new provider,
        and updates the client. Preserves all existing client settings.

        Args:
            provider_name: The provider name to add.

        Raises:
            ClientError: On Cognito API failure.
        """
        if not self._enabled:
            raise RuntimeError("Cognito identity provider service is not enabled")

        # Get current client configuration
        response = self._client.describe_user_pool_client(
            UserPoolId=self._user_pool_id,
            ClientId=self._app_client_id,
        )
        client_config = response["UserPoolClient"]

        current_providers = client_config.get("SupportedIdentityProviders", [])
        if provider_name in current_providers:
            logger.info(f"Provider '{provider_name}' already in App Client supported providers.")
            return

        updated_providers = current_providers + [provider_name]

        # Build update params preserving existing settings
        update_params = self._build_client_update_params(client_config, updated_providers)
        self._client.update_user_pool_client(**update_params)
        logger.info(
            f"Updated App Client supported providers: {updated_providers}"
        )

    def remove_provider_from_app_client(self, provider_name: str) -> None:
        """Remove a provider from the App Client's SupportedIdentityProviders.

        Args:
            provider_name: The provider name to remove.
        """
        if not self._enabled:
            return

        response = self._client.describe_user_pool_client(
            UserPoolId=self._user_pool_id,
            ClientId=self._app_client_id,
        )
        client_config = response["UserPoolClient"]

        current_providers = client_config.get("SupportedIdentityProviders", [])
        if provider_name not in current_providers:
            logger.info(f"Provider '{provider_name}' not in App Client supported providers.")
            return

        updated_providers = [p for p in current_providers if p != provider_name]

        update_params = self._build_client_update_params(client_config, updated_providers)
        self._client.update_user_pool_client(**update_params)
        logger.info(
            f"Removed '{provider_name}' from App Client supported providers: {updated_providers}"
        )

    def list_identity_provider_names(self) -> List[str]:
        """Names of every identity provider registered in the user pool."""
        if not self._enabled:
            return []

        names: List[str] = []
        kwargs: dict = {"UserPoolId": self._user_pool_id, "MaxResults": 60}
        while True:
            response = self._client.list_identity_providers(**kwargs)
            names.extend(p["ProviderName"] for p in response.get("Providers", []))
            token = response.get("NextToken")
            if not token:
                return names
            kwargs["NextToken"] = token

    def ensure_providers_on_app_client(self, provider_names: Iterable[str]) -> List[str]:
        """Add any of ``provider_names`` missing from the App Client. Never removes.

        A PlatformStack deploy that touches the App Client sends CloudFormation's
        full property set to UpdateUserPoolClient, and the template's
        ``SupportedIdentityProviders`` holds only what CDK knows about (COGNITO
        plus ``CDK_COGNITO_SUPPORTED_IDPS``). Providers an admin added at runtime
        are dropped. This puts them back.

        A name with no identity provider in the pool is skipped: Cognito refuses
        an update naming one, and that would block every other name with it.

        Returns:
            The names added; empty when the client already listed all of them.

        Raises:
            ClientError: On Cognito API failure.
        """
        if not self._enabled:
            return []

        wanted = sorted({name for name in provider_names if name})
        if not wanted:
            return []

        response = self._client.describe_user_pool_client(
            UserPoolId=self._user_pool_id,
            ClientId=self._app_client_id,
        )
        client_config = response["UserPoolClient"]
        current = client_config.get("SupportedIdentityProviders", [])
        missing = [name for name in wanted if name not in current]
        if not missing:
            return []

        registered = set(self.list_identity_provider_names())
        orphaned = [name for name in missing if name not in registered]
        if orphaned:
            logger.warning(
                f"Not adding {orphaned} to the App Client: no such identity provider "
                "in the user pool."
            )
        to_add = [name for name in missing if name in registered]
        if not to_add:
            return []

        update_params = self._build_client_update_params(client_config, current + to_add)
        self._client.update_user_pool_client(**update_params)
        logger.warning(
            f"Restored {to_add} to the App Client's supported identity providers "
            "(dropped outside app-api, usually by a PlatformStack deploy)."
        )
        return to_add

    # Every UpdateUserPoolClient field other than the ids and
    # SupportedIdentityProviders. Cognito resets any field the request omits
    # to its default, so an update that sends a subset silently rewrites the
    # rest of the client (token revocation, auth-session length, attribute
    # permissions...).
    _PRESERVED_CLIENT_KEYS = (
        "ClientName",
        "RefreshTokenValidity",
        "AccessTokenValidity",
        "IdTokenValidity",
        "TokenValidityUnits",
        "ReadAttributes",
        "WriteAttributes",
        "ExplicitAuthFlows",
        "CallbackURLs",
        "LogoutURLs",
        "DefaultRedirectURI",
        "AllowedOAuthFlows",
        "AllowedOAuthScopes",
        "AllowedOAuthFlowsUserPoolClient",
        "AnalyticsConfiguration",
        "PreventUserExistenceErrors",
        "EnableTokenRevocation",
        "EnablePropagateAdditionalUserContextData",
        "AuthSessionValidity",
        "RefreshTokenRotation",
    )

    def _build_client_update_params(
        self, client_config: dict, supported_providers: List[str]
    ) -> dict:
        """Build UpdateUserPoolClient params preserving existing settings."""
        params: dict = {
            "UserPoolId": self._user_pool_id,
            "ClientId": self._app_client_id,
            "SupportedIdentityProviders": supported_providers,
        }
        for key in self._PRESERVED_CLIENT_KEYS:
            if key in client_config:
                params[key] = client_config[key]

        return params


# Singleton
_cognito_idp_service: Optional[CognitoIdentityProviderService] = None


def get_cognito_idp_service() -> CognitoIdentityProviderService:
    """Get the Cognito identity provider service singleton."""
    global _cognito_idp_service
    if _cognito_idp_service is None:
        _cognito_idp_service = CognitoIdentityProviderService()
    return _cognito_idp_service
