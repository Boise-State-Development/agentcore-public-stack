"""Cognito Identity Provider management for federated OIDC providers.

Handles registering, updating, and deleting federated identity providers in a
Cognito User Pool, and keeping every App Client's supported-providers list in
sync with them.

"Every App Client" is the important part: the pool has more than one client
(the confidential BFF client used by app-api, and the public PKCE client used
by the terminal client), and they must offer the same federated providers. A
provider added to only one of them still works for that client's users, so the
gap is invisible until someone signs in from the other surface and finds their
IdP missing.
"""

import logging
import os
from typing import Dict, List, Optional

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
        app_client_ids: Optional[List[str]] = None,
    ):
        """Initialise the service.

        Args:
            user_pool_id: Defaults to ``COGNITO_USER_POOL_ID``.
            app_client_id: A single client id. Convenience for tests and for
                callers that only care about the primary client.
            region: Defaults to ``AWS_REGION``.
            app_client_ids: Every app client that must carry federated
                providers. When omitted, resolved from the environment.

        A user pool can have several app clients that all need the same
        federated providers — today the confidential BFF client and the public
        CLI client. A provider registered on only one of them silently fails to
        appear for users of the other, so every mutation fans out across the
        whole list.
        """
        self._user_pool_id = user_pool_id or os.getenv("COGNITO_USER_POOL_ID")
        self._app_client_ids = self._resolve_client_ids(app_client_id, app_client_ids)
        # The first entry is the "primary" client, used for reads.
        self._app_client_id = self._app_client_ids[0] if self._app_client_ids else None
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._user_pool_id and self._app_client_ids)

        if not self._enabled:
            logger.warning("COGNITO_USER_POOL_ID or COGNITO_APP_CLIENT_ID not set. " "Cognito identity provider service is disabled.")
            return

        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._client = session.client("cognito-idp", region_name=self._region)
        else:
            self._client = boto3.client("cognito-idp", region_name=self._region)

        logger.info(f"Initialized Cognito IdP service: pool={self._user_pool_id}, " f"clients={self._app_client_ids}")

    @staticmethod
    def _resolve_client_ids(
        app_client_id: Optional[str],
        app_client_ids: Optional[List[str]],
    ) -> List[str]:
        """Resolve the app clients to keep in sync, preserving order.

        Explicit arguments win over the environment so tests stay hermetic.
        ``COGNITO_CLI_APP_CLIENT_ID`` is empty when the CLI client is not
        deployed, which is why blanks are filtered rather than trusted.
        """
        if app_client_ids is not None:
            candidates = list(app_client_ids)
        elif app_client_id is not None:
            candidates = [app_client_id]
        else:
            candidates = [
                os.getenv("COGNITO_APP_CLIENT_ID") or "",
                os.getenv("COGNITO_CLI_APP_CLIENT_ID") or "",
            ]

        resolved: List[str] = []
        for candidate in candidates:
            value = (candidate or "").strip()
            if value and value not in resolved:
                resolved.append(value)
        return resolved

    @property
    def app_client_ids(self) -> List[str]:
        """Every app client this service keeps in sync."""
        return list(self._app_client_ids)

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
                logger.warning(f"Cognito identity provider '{provider_name}' not found during delete (idempotent).")
            else:
                raise

    def get_supported_identity_providers(self) -> List[str]:
        """Get the supported identity providers on the primary App Client.

        Reads the primary client only. Every mutation here keeps all clients in
        lockstep, so the primary is representative; a divergence means someone
        edited a client out of band.

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

    def _set_providers_on_client(self, client_id: str, provider_name: str, *, present: bool) -> bool:
        """Add or remove one provider on one client.

        Returns True when the client was changed, False when it was already in
        the desired state. Preserves all other client settings, because
        UpdateUserPoolClient replaces the whole configuration.
        """
        response = self._client.describe_user_pool_client(
            UserPoolId=self._user_pool_id,
            ClientId=client_id,
        )
        client_config = response["UserPoolClient"]
        current = client_config.get("SupportedIdentityProviders", [])

        if present:
            if provider_name in current:
                return False
            updated = current + [provider_name]
        else:
            if provider_name not in current:
                return False
            updated = [p for p in current if p != provider_name]

        self._client.update_user_pool_client(**self._build_client_update_params(client_config, updated, client_id))
        return True

    def add_provider_to_app_client(self, provider_name: str) -> None:
        """Add a provider to every App Client's SupportedIdentityProviders.

        All clients must end up carrying the provider, or none should: the
        caller's rollback deletes the identity provider itself, and a client
        left pointing at a deleted provider breaks its hosted UI. So a failure
        part-way through unwinds the clients already updated before re-raising.

        Raises:
            ClientError: On Cognito API failure.
        """
        if not self._enabled:
            raise RuntimeError("Cognito identity provider service is not enabled")

        changed: List[str] = []
        try:
            for client_id in self._app_client_ids:
                if self._set_providers_on_client(client_id, provider_name, present=True):
                    changed.append(client_id)
        except ClientError:
            logger.error(
                "Failed adding provider '%s'; reverting %d client(s) already updated",
                provider_name,
                len(changed),
                exc_info=True,
            )
            for client_id in changed:
                try:
                    self._set_providers_on_client(client_id, provider_name, present=False)
                except ClientError:
                    # Best effort: the original error is the one worth raising,
                    # but a stranded client must be visible in the logs.
                    logger.error(
                        "Revert failed for client %s; it may still list provider '%s'",
                        client_id,
                        provider_name,
                        exc_info=True,
                    )
            raise

        logger.info(f"Added '{provider_name}' to {len(changed)} of " f"{len(self._app_client_ids)} app client(s)")

    def remove_provider_from_app_client(self, provider_name: str) -> None:
        """Remove a provider from every App Client.

        Unlike the add path this keeps going after a failure: this runs during
        cleanup, and stopping early would strand the provider on the remaining
        clients. The first error is re-raised once every client has been tried.
        """
        if not self._enabled:
            return

        first_error: Optional[ClientError] = None
        removed = 0
        for client_id in self._app_client_ids:
            try:
                if self._set_providers_on_client(client_id, provider_name, present=False):
                    removed += 1
            except ClientError as exc:
                logger.error(
                    "Failed removing provider '%s' from client %s",
                    provider_name,
                    client_id,
                    exc_info=True,
                )
                first_error = first_error or exc

        logger.info(f"Removed '{provider_name}' from {removed} of " f"{len(self._app_client_ids)} app client(s)")
        if first_error is not None:
            raise first_error

    def _build_client_update_params(self, client_config: dict, supported_providers: List[str], client_id: Optional[str] = None) -> dict:
        """Build UpdateUserPoolClient params preserving existing settings."""
        params: dict = {
            "UserPoolId": self._user_pool_id,
            "ClientId": client_id or self._app_client_id,
            "SupportedIdentityProviders": supported_providers,
        }

        # Preserve key existing settings
        preserve_keys = [
            "ClientName",
            "RefreshTokenValidity",
            "AccessTokenValidity",
            "IdTokenValidity",
            "TokenValidityUnits",
            "ExplicitAuthFlows",
            "CallbackURLs",
            "LogoutURLs",
            "AllowedOAuthFlows",
            "AllowedOAuthScopes",
            "AllowedOAuthFlowsUserPoolClient",
            "PreventUserExistenceErrors",
        ]
        for key in preserve_keys:
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
