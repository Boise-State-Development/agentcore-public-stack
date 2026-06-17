"""Service layer for OIDC authentication provider management."""

import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from botocore.exceptions import ClientError

from apis.shared.security import UrlValidationError, validate_external_url
from fastapi import HTTPException, status

from .cognito_idp_service import (
    CognitoIdentityProviderService,
    get_cognito_idp_service,
)
from .models import (
    AuthProvider,
    AuthProviderCreate,
    AuthProviderUpdate,
    OIDCDiscoveryResponse,
)
from .repository import AuthProviderRepository, get_auth_provider_repository

logger = logging.getLogger(__name__)


class AuthProviderService:
    """Business logic for OIDC authentication provider management."""

    def __init__(
        self,
        repository: AuthProviderRepository,
        cognito_idp_service: Optional[CognitoIdentityProviderService] = None,
    ):
        self._repo = repository
        self._cognito_idp = cognito_idp_service

    @property
    def enabled(self) -> bool:
        return self._repo.enabled

    # =========================================================================
    # OIDC Discovery
    # =========================================================================

    async def discover_endpoints(self, issuer_url: str) -> OIDCDiscoveryResponse:
        """
        Fetch OIDC endpoints from the provider's .well-known/openid-configuration.

        Args:
            issuer_url: The OIDC issuer URL

        Returns:
            OIDCDiscoveryResponse with discovered endpoints

        Raises:
            HTTPException: If discovery fails
        """
        # Refuse anything the SSRF validator rejects: loopback,
        # link-local (incl. cloud metadata), private, multicast,
        # reserved, or schemes other than https. OIDC issuers are
        # public HTTPS endpoints by spec; we're strict on purpose.
        try:
            validate_external_url(issuer_url, allow_schemes={"https"})
        except UrlValidationError:
            logger.warning("OIDC discovery refused by URL validator")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Issuer URL is not permitted.",
            )

        discovery_url = issuer_url.rstrip("/") + "/.well-known/openid-configuration"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(discovery_url, timeout=10.0)
                response.raise_for_status()
                data = response.json()

            return OIDCDiscoveryResponse(
                issuer=data.get("issuer", issuer_url),
                authorization_endpoint=data.get("authorization_endpoint"),
                token_endpoint=data.get("token_endpoint"),
                jwks_uri=data.get("jwks_uri"),
                userinfo_endpoint=data.get("userinfo_endpoint"),
                end_session_endpoint=data.get("end_session_endpoint"),
                scopes_supported=data.get("scopes_supported"),
                response_types_supported=data.get("response_types_supported"),
                claims_supported=data.get("claims_supported"),
            )

        except httpx.HTTPStatusError as e:
            logger.error(f"OIDC discovery failed for {issuer_url}: {e.response.status_code}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"OIDC discovery failed: HTTP {e.response.status_code} from {discovery_url}",
            )
        except httpx.RequestError as e:
            logger.error(f"OIDC discovery request error for {issuer_url}: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not reach OIDC discovery endpoint: {discovery_url}",
            )
        except Exception as e:
            logger.error(f"OIDC discovery error for {issuer_url}: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid OIDC discovery response from {discovery_url}",
            )

    # =========================================================================
    # Provider CRUD
    # =========================================================================

    async def create_provider(
        self, data: AuthProviderCreate, created_by: Optional[str] = None
    ) -> AuthProvider:
        """
        Create a new auth provider with optional auto-discovery.

        If endpoints are not explicitly provided and an issuer_url is set,
        endpoints are auto-discovered from .well-known/openid-configuration.

        When Cognito IdP service is enabled, the provider is also registered
        as a federated identity provider in the Cognito User Pool and added
        to the App Client's supported providers list.

        Rollback strategy:
        - If UpdateUserPoolClient fails → delete the identity provider from Cognito
        - If DynamoDB write fails → delete the identity provider from Cognito
        """
        # Validate provider_id format
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", data.provider_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="provider_id must be lowercase alphanumeric with hyphens",
            )

        # Auto-discover endpoints if not all provided and auto_discover is enabled
        needs_discovery = not all([
            data.authorization_endpoint,
            data.token_endpoint,
            data.jwks_uri,
        ])

        if needs_discovery and data.issuer_url and data.auto_discover:
            try:
                discovered = await self.discover_endpoints(data.issuer_url)
                # Fill in missing endpoints from discovery
                if not data.authorization_endpoint:
                    data.authorization_endpoint = discovered.authorization_endpoint
                if not data.token_endpoint:
                    data.token_endpoint = discovered.token_endpoint
                if not data.jwks_uri:
                    data.jwks_uri = discovered.jwks_uri
                if not data.userinfo_endpoint:
                    data.userinfo_endpoint = discovered.userinfo_endpoint
                if not data.end_session_endpoint:
                    data.end_session_endpoint = discovered.end_session_endpoint
            except HTTPException:
                logger.warning(
                    f"Auto-discovery failed for {data.issuer_url}, "
                    "proceeding with manually provided endpoints"
                )

        # Validate user_id_pattern is a valid regex if provided
        if data.user_id_pattern:
            try:
                re.compile(data.user_id_pattern)
            except re.error as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid user_id_pattern regex: {e}",
                )

        # Build attribute mapping from provider claim config
        attribute_mapping = self._build_attribute_mapping(data)

        # Register in Cognito if enabled
        cognito_provider_name: Optional[str] = None
        if self._cognito_idp and self._cognito_idp.enabled:
            cognito_provider_name = data.provider_id
            try:
                # Step 1: Create identity provider in Cognito
                self._cognito_idp.create_identity_provider(
                    provider_name=cognito_provider_name,
                    issuer_url=data.issuer_url,
                    client_id=data.client_id,
                    client_secret=data.client_secret,
                    scopes=data.scopes,
                    attribute_mapping=attribute_mapping,
                )
            except ClientError as e:
                logger.error(f"Cognito CreateIdentityProvider failed: {e}")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Failed to register provider in Cognito: {e.response['Error']['Message']}",
                )

            try:
                # Step 2: Add provider to App Client's supported providers
                self._cognito_idp.add_provider_to_app_client(cognito_provider_name)
            except ClientError as e:
                logger.error(
                    f"Cognito UpdateUserPoolClient failed, rolling back identity provider: {e}"
                )
                # Rollback: delete the identity provider we just created
                self._cognito_idp.delete_identity_provider(cognito_provider_name)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Failed to update App Client in Cognito: {e.response['Error']['Message']}",
                )

        # Step 3: Save to DynamoDB + Secrets Manager (existing logic)
        try:
            provider = await self._repo.create_provider(
                data, created_by=created_by, cognito_provider_name=cognito_provider_name
            )
        except Exception as e:
            # Rollback: delete from Cognito if DynamoDB write fails
            if cognito_provider_name and self._cognito_idp and self._cognito_idp.enabled:
                logger.error(
                    f"DynamoDB write failed, rolling back Cognito identity provider: {e}"
                )
                try:
                    self._cognito_idp.remove_provider_from_app_client(cognito_provider_name)
                except Exception:
                    logger.exception("Failed to remove provider from App Client during rollback")
                self._cognito_idp.delete_identity_provider(cognito_provider_name)
            raise

        return provider

    def _build_attribute_mapping(self, data: AuthProviderCreate) -> Dict[str, str]:
        """Build Cognito attribute mapping from provider claim configuration.

        Maps Cognito standard attributes to the provider's claim names.
        Uses the configured user_id_claim for custom:provider_sub and
        roles_claim for custom:roles.
        """
        mapping: Dict[str, str] = {
            "email": data.email_claim or "email",
            "custom:provider_sub": data.user_id_claim or "sub",
        }
        if data.roles_claim:
            mapping["custom:roles"] = data.roles_claim
        if data.name_claim:
            mapping["name"] = data.name_claim
        if data.first_name_claim:
            mapping["given_name"] = data.first_name_claim
        if data.last_name_claim:
            mapping["family_name"] = data.last_name_claim
        if data.picture_claim:
            mapping["picture"] = data.picture_claim
        return mapping

    # Fields that, when changed, require a Cognito UpdateIdentityProvider call
    _OIDC_COGNITO_FIELDS = {
        "issuer_url",
        "client_id",
        "client_secret",
        "scopes",
        "user_id_claim",
        "email_claim",
        "name_claim",
        "roles_claim",
        "first_name_claim",
        "last_name_claim",
        "picture_claim",
    }

    async def update_provider(
        self, provider_id: str, updates: AuthProviderUpdate
    ) -> Optional[AuthProvider]:
        """Update an auth provider. Re-discovers endpoints if issuer_url changes.

        When Cognito IdP service is enabled and the provider has a
        cognito_provider_name, OIDC-relevant field changes are synced
        to Cognito via UpdateIdentityProvider before updating DynamoDB.
        If the Cognito update fails, DynamoDB is not updated.
        """
        # If issuer_url is being changed, re-discover endpoints
        if updates.issuer_url:
            try:
                discovered = await self.discover_endpoints(updates.issuer_url)
                if not updates.authorization_endpoint:
                    updates.authorization_endpoint = discovered.authorization_endpoint
                if not updates.token_endpoint:
                    updates.token_endpoint = discovered.token_endpoint
                if not updates.jwks_uri:
                    updates.jwks_uri = discovered.jwks_uri
                if not updates.userinfo_endpoint:
                    updates.userinfo_endpoint = discovered.userinfo_endpoint
                if not updates.end_session_endpoint:
                    updates.end_session_endpoint = discovered.end_session_endpoint
            except HTTPException:
                logger.warning(
                    f"Auto-discovery failed for {updates.issuer_url}, "
                    "proceeding with manually provided endpoints"
                )

        # Validate user_id_pattern if provided
        if updates.user_id_pattern:
            try:
                re.compile(updates.user_id_pattern)
            except re.error as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid user_id_pattern regex: {e}",
                )

        # Sync OIDC changes to Cognito if applicable
        if self._cognito_idp and self._cognito_idp.enabled:
            existing = await self._repo.get_provider(provider_id)
            if existing and existing.cognito_provider_name:
                # Determine which OIDC-relevant fields actually changed
                update_fields = updates.model_dump(exclude_none=True)
                changed_oidc = {
                    k for k in update_fields if k in self._OIDC_COGNITO_FIELDS
                }

                if changed_oidc:
                    # Build Cognito update kwargs from changed fields
                    cognito_kwargs: Dict[str, Any] = {}
                    if "issuer_url" in changed_oidc:
                        cognito_kwargs["issuer_url"] = updates.issuer_url
                    if "client_id" in changed_oidc:
                        cognito_kwargs["client_id"] = updates.client_id
                    if "client_secret" in changed_oidc:
                        cognito_kwargs["client_secret"] = updates.client_secret
                    if "scopes" in changed_oidc:
                        cognito_kwargs["scopes"] = updates.scopes

                    # Rebuild attribute mapping if any claim fields changed
                    claim_fields = changed_oidc & {
                        "user_id_claim",
                        "email_claim",
                        "name_claim",
                        "roles_claim",
                        "first_name_claim",
                        "last_name_claim",
                        "picture_claim",
                    }
                    if claim_fields:
                        # Merge existing claims with updates
                        email_claim = updates.email_claim or existing.email_claim or "email"
                        user_id_claim = updates.user_id_claim if updates.user_id_claim is not None else existing.user_id_claim
                        mapping: Dict[str, str] = {
                            "email": email_claim,
                            "custom:provider_sub": user_id_claim or "sub",
                        }
                        roles_claim = updates.roles_claim if updates.roles_claim is not None else existing.roles_claim
                        if roles_claim:
                            mapping["custom:roles"] = roles_claim
                        name_claim = updates.name_claim if updates.name_claim is not None else existing.name_claim
                        if name_claim:
                            mapping["name"] = name_claim
                        first_name = updates.first_name_claim if updates.first_name_claim is not None else existing.first_name_claim
                        if first_name:
                            mapping["given_name"] = first_name
                        last_name = updates.last_name_claim if updates.last_name_claim is not None else existing.last_name_claim
                        if last_name:
                            mapping["family_name"] = last_name
                        picture = updates.picture_claim if updates.picture_claim is not None else existing.picture_claim
                        if picture:
                            mapping["picture"] = picture
                        cognito_kwargs["attribute_mapping"] = mapping

                    try:
                        self._cognito_idp.update_identity_provider(
                            provider_name=existing.cognito_provider_name,
                            **cognito_kwargs,
                        )
                    except ClientError as e:
                        logger.error(f"Cognito UpdateIdentityProvider failed: {e}")
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Failed to update provider in Cognito: {e.response['Error']['Message']}",
                        )

        return await self._repo.update_provider(provider_id, updates)

    async def get_provider(self, provider_id: str) -> Optional[AuthProvider]:
        """Get a provider by ID."""
        return await self._repo.get_provider(provider_id)

    async def list_providers(self, enabled_only: bool = False) -> List[AuthProvider]:
        """List all providers."""
        return await self._repo.list_providers(enabled_only=enabled_only)

    async def delete_provider(self, provider_id: str) -> bool:
        """Delete a provider, removing from Cognito first, then DynamoDB and Secrets Manager.

        If the provider has a cognito_provider_name and the Cognito IdP service
        is enabled, the provider is removed from the App Client's supported
        providers list and deleted from the Cognito User Pool before the
        DynamoDB/Secrets Manager cleanup. Cognito "not found" errors are
        handled gracefully (idempotent delete). Cognito failures are logged
        but do not prevent the DynamoDB deletion (best-effort cleanup).
        """
        # Fetch provider to check for Cognito registration
        provider = await self._repo.get_provider(provider_id)
        if not provider:
            return False

        # Remove from Cognito if applicable
        if provider.cognito_provider_name and self._cognito_idp and self._cognito_idp.enabled:
            try:
                self._cognito_idp.remove_provider_from_app_client(provider.cognito_provider_name)
            except Exception:
                logger.exception(
                    f"Failed to remove provider '{provider.cognito_provider_name}' from App Client "
                    "(proceeding with delete)"
                )
            try:
                self._cognito_idp.delete_identity_provider(provider.cognito_provider_name)
            except Exception:
                logger.exception(
                    f"Failed to delete Cognito identity provider '{provider.cognito_provider_name}' "
                    "(proceeding with delete)"
                )

        # Delete from DynamoDB and Secrets Manager
        return await self._repo.delete_provider(provider_id)

    async def get_client_secret(self, provider_id: str) -> Optional[str]:
        """Get client secret for a provider."""
        return await self._repo.get_client_secret(provider_id)

    async def test_provider(self, provider_id: str) -> dict:
        """
        Test provider connectivity by verifying JWKS and token endpoints are reachable.

        Each stored URL is run through the SSRF validator before any
        outbound call. URLs that fail validation are reported as
        unreachable with a generic error string and are not contacted —
        the same protection applied at the discover endpoint, repeated
        here as defense in depth so a bad URL persisted from elsewhere
        cannot be turned into an outbound dispatch.

        Returns:
            Dict with test results
        """
        provider = await self._repo.get_provider(provider_id)
        if not provider:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Auth provider '{provider_id}' not found",
            )

        results = {
            "provider_id": provider_id,
            "jwks_reachable": False,
            "discovery_reachable": False,
            "token_endpoint_reachable": False,
            "errors": [],
        }

        def _safe(url: Optional[str], schemes: set[str] = {"https"}) -> Optional[str]:
            """Return the URL if it passes the SSRF validator, else None
            (and append a generic error to the result map)."""
            if not url:
                return None
            try:
                validate_external_url(url, allow_schemes=schemes)
                return url
            except UrlValidationError:
                results["errors"].append("Endpoint URL is not permitted.")
                return None

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Test JWKS endpoint
            jwks_uri = _safe(provider.jwks_uri)
            if jwks_uri:
                try:
                    resp = await client.get(jwks_uri)
                    results["jwks_reachable"] = resp.status_code == 200
                    if resp.status_code != 200:
                        results["errors"].append(
                            f"JWKS endpoint returned {resp.status_code}"
                        )
                except Exception as e:
                    results["errors"].append(f"JWKS endpoint error: {str(e)}")

            # Test OIDC discovery
            issuer = _safe(provider.issuer_url)
            if issuer:
                discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
                try:
                    resp = await client.get(discovery_url)
                    results["discovery_reachable"] = resp.status_code == 200
                    if resp.status_code != 200:
                        results["errors"].append(
                            f"Discovery endpoint returned {resp.status_code}"
                        )
                except Exception as e:
                    results["errors"].append(f"Discovery endpoint error: {str(e)}")

            # Test token endpoint (HEAD/OPTIONS only)
            token_endpoint = _safe(provider.token_endpoint)
            if token_endpoint:
                try:
                    resp = await client.options(token_endpoint)
                    results["token_endpoint_reachable"] = resp.status_code < 500
                except Exception as e:
                    results["errors"].append(f"Token endpoint error: {str(e)}")

        return results


# Singleton instance
_service: Optional[AuthProviderService] = None


def get_auth_provider_service() -> AuthProviderService:
    """Get the auth provider service singleton."""
    global _service
    if _service is None:
        cognito_idp = get_cognito_idp_service()
        _service = AuthProviderService(
            get_auth_provider_repository(),
            cognito_idp_service=cognito_idp,
        )
    return _service
