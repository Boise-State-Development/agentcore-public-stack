"""Tests for Cognito Identity Provider service and create_provider Cognito integration.

Covers:
- CognitoIdentityProviderService CRUD operations
- AuthProviderService.create_provider Cognito registration with rollback
- cognitoProviderName stored in DynamoDB
"""

import json
import pytest
import boto3
from moto import mock_aws
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from apis.shared.auth_providers.models import AuthProviderCreate

AWS_REGION = "us-east-1"
USER_POOL_NAME = "test-pool"


def _make_create(**kw):
    defaults = dict(
        provider_id="okta-1",
        display_name="Okta",
        provider_type="oidc",
        issuer_url="https://okta.example.com",
        client_id="cid",
        client_secret="secret",
        enabled=True,
        authorization_endpoint="https://okta.example.com/authorize",
        token_endpoint="https://okta.example.com/token",
        jwks_uri="https://okta.example.com/keys",
    )
    defaults.update(kw)
    return AuthProviderCreate(**defaults)


@pytest.fixture()
def aws_env(monkeypatch):
    """Activate moto mock_aws and set default env vars."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture()
def cognito_pool(aws_env):
    """Create a Cognito User Pool and App Client for testing."""
    client = boto3.client("cognito-idp", region_name=AWS_REGION)

    pool = client.create_user_pool(
        PoolName=USER_POOL_NAME,
        Schema=[
            {"Name": "email", "AttributeDataType": "String", "Required": True},
        ],
    )
    pool_id = pool["UserPool"]["Id"]

    app_client = client.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName="test-app-client",
        GenerateSecret=False,
        SupportedIdentityProviders=["COGNITO"],
        AllowedOAuthFlows=["code"],
        AllowedOAuthScopes=["openid", "profile", "email"],
        CallbackURLs=["http://localhost:4200/auth/callback"],
    )
    client_id = app_client["UserPoolClient"]["ClientId"]

    return {"pool_id": pool_id, "client_id": client_id, "boto_client": client}


@pytest.fixture()
def cognito_idp_service(cognito_pool):
    """Create a CognitoIdentityProviderService with the moto pool."""
    from apis.shared.auth_providers.cognito_idp_service import (
        CognitoIdentityProviderService,
    )

    return CognitoIdentityProviderService(
        user_pool_id=cognito_pool["pool_id"],
        app_client_id=cognito_pool["client_id"],
        region=AWS_REGION,
    )


@pytest.fixture()
def auth_providers_table(aws_env, monkeypatch):
    """Create the auth providers DynamoDB table."""
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-auth-providers"
    monkeypatch.setenv("DYNAMODB_AUTH_PROVIDERS_TABLE_NAME", name)
    ddb.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "EnabledProvidersIndex",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(name)


@pytest.fixture()
def secrets_manager(aws_env, monkeypatch):
    """Create Secrets Manager secret for auth provider secrets."""
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    sm.create_secret(Name="auth-provider-secrets", SecretString="{}")
    monkeypatch.setenv("AUTH_PROVIDER_SECRETS_ARN", "auth-provider-secrets")
    return sm


@pytest.fixture()
def auth_repo(auth_providers_table, secrets_manager):
    """Create an AuthProviderRepository."""
    from apis.shared.auth_providers.repository import AuthProviderRepository

    return AuthProviderRepository(
        table_name="test-auth-providers",
        secrets_arn="auth-provider-secrets",
        region=AWS_REGION,
    )


@pytest.fixture()
def service_with_cognito(auth_repo, cognito_idp_service):
    """Create an AuthProviderService with Cognito IdP integration."""
    from apis.shared.auth_providers.service import AuthProviderService

    return AuthProviderService(
        repository=auth_repo,
        cognito_idp_service=cognito_idp_service,
    )


# ===================================================================
# CognitoIdentityProviderService unit tests
# ===================================================================


class TestCognitoIdentityProviderService:
    def test_enabled(self, cognito_idp_service):
        assert cognito_idp_service.enabled is True

    def test_disabled_when_no_pool(self):
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )

        svc = CognitoIdentityProviderService(user_pool_id=None, app_client_id=None)
        assert svc.enabled is False

    def test_create_identity_provider(self, cognito_idp_service, cognito_pool):
        cognito_idp_service.create_identity_provider(
            provider_name="okta-1",
            issuer_url="https://okta.example.com",
            client_id="cid",
            client_secret="secret",
        )
        # Verify it was created
        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        assert resp["IdentityProvider"]["ProviderName"] == "okta-1"
        assert resp["IdentityProvider"]["ProviderType"] == "OIDC"
        details = resp["IdentityProvider"]["ProviderDetails"]
        assert details["oidc_issuer"] == "https://okta.example.com"
        assert details["client_id"] == "cid"

    def test_create_identity_provider_with_custom_mapping(
        self, cognito_idp_service, cognito_pool
    ):
        custom_mapping = {
            "email": "mail",
            "name": "displayName",
            "custom:provider_sub": "sub",
        }
        cognito_idp_service.create_identity_provider(
            provider_name="custom-1",
            issuer_url="https://custom.example.com",
            client_id="cid",
            client_secret="secret",
            attribute_mapping=custom_mapping,
        )
        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="custom-1",
        )
        mapping = resp["IdentityProvider"]["AttributeMapping"]
        assert mapping["email"] == "mail"
        assert mapping["name"] == "displayName"

    def test_delete_identity_provider(self, cognito_idp_service, cognito_pool):
        cognito_idp_service.create_identity_provider(
            provider_name="to-delete",
            issuer_url="https://example.com",
            client_id="cid",
            client_secret="secret",
        )
        cognito_idp_service.delete_identity_provider("to-delete")
        # Verify it's gone
        client = cognito_pool["boto_client"]
        with pytest.raises(ClientError) as exc_info:
            client.describe_identity_provider(
                UserPoolId=cognito_pool["pool_id"],
                ProviderName="to-delete",
            )
        assert exc_info.value.response["Error"]["Code"] == "ResourceNotFoundException"

    def test_delete_nonexistent_is_idempotent(self, cognito_idp_service):
        # Should not raise
        cognito_idp_service.delete_identity_provider("nonexistent-provider")

    def test_add_provider_to_app_client(self, cognito_idp_service, cognito_pool):
        # First create the IdP
        cognito_idp_service.create_identity_provider(
            provider_name="okta-1",
            issuer_url="https://okta.example.com",
            client_id="cid",
            client_secret="secret",
        )
        cognito_idp_service.add_provider_to_app_client("okta-1")

        providers = cognito_idp_service.get_supported_identity_providers()
        assert "COGNITO" in providers
        assert "okta-1" in providers

    def test_add_duplicate_provider_is_idempotent(
        self, cognito_idp_service, cognito_pool
    ):
        cognito_idp_service.create_identity_provider(
            provider_name="okta-1",
            issuer_url="https://okta.example.com",
            client_id="cid",
            client_secret="secret",
        )
        cognito_idp_service.add_provider_to_app_client("okta-1")
        cognito_idp_service.add_provider_to_app_client("okta-1")

        providers = cognito_idp_service.get_supported_identity_providers()
        assert providers.count("okta-1") == 1

    def test_remove_provider_from_app_client(
        self, cognito_idp_service, cognito_pool
    ):
        cognito_idp_service.create_identity_provider(
            provider_name="okta-1",
            issuer_url="https://okta.example.com",
            client_id="cid",
            client_secret="secret",
        )
        cognito_idp_service.add_provider_to_app_client("okta-1")
        cognito_idp_service.remove_provider_from_app_client("okta-1")

        providers = cognito_idp_service.get_supported_identity_providers()
        assert "okta-1" not in providers
        assert "COGNITO" in providers

    def test_get_supported_identity_providers_default(self, cognito_idp_service):
        providers = cognito_idp_service.get_supported_identity_providers()
        assert "COGNITO" in providers


# ===================================================================
# AuthProviderService create_provider with Cognito integration
# ===================================================================


class TestCreateProviderWithCognito:
    @pytest.mark.asyncio
    async def test_create_registers_in_cognito(
        self, service_with_cognito, cognito_pool
    ):
        """Creating a provider should register it in Cognito and store cognitoProviderName."""
        data = _make_create()
        provider = await service_with_cognito.create_provider(data, created_by="admin@test.com")

        assert provider.cognito_provider_name == "okta-1"

        # Verify in Cognito
        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        assert resp["IdentityProvider"]["ProviderType"] == "OIDC"

    @pytest.mark.asyncio
    async def test_create_adds_to_app_client(
        self, service_with_cognito, cognito_idp_service
    ):
        """Creating a provider should add it to the App Client's supported providers."""
        data = _make_create()
        await service_with_cognito.create_provider(data)

        providers = cognito_idp_service.get_supported_identity_providers()
        assert "okta-1" in providers
        assert "COGNITO" in providers

    @pytest.mark.asyncio
    async def test_create_stores_cognito_provider_name_in_dynamo(
        self, service_with_cognito, auth_providers_table
    ):
        """The DynamoDB item should contain cognitoProviderName."""
        data = _make_create()
        await service_with_cognito.create_provider(data)

        resp = auth_providers_table.get_item(
            Key={"PK": "AUTH_PROVIDER#okta-1", "SK": "AUTH_PROVIDER#okta-1"}
        )
        item = resp["Item"]
        assert item["cognitoProviderName"] == "okta-1"

    @pytest.mark.asyncio
    async def test_create_attribute_mapping_uses_claim_config(
        self, service_with_cognito, cognito_pool
    ):
        """Attribute mapping should reflect the provider's claim configuration."""
        data = _make_create(
            email_claim="mail",
            name_claim="displayName",
            first_name_claim="firstName",
            last_name_claim="lastName",
            picture_claim="avatar",
        )
        await service_with_cognito.create_provider(data)

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        mapping = resp["IdentityProvider"]["AttributeMapping"]
        assert mapping["email"] == "mail"
        assert mapping["name"] == "displayName"
        assert mapping["given_name"] == "firstName"
        assert mapping["family_name"] == "lastName"
        assert mapping["picture"] == "avatar"
        assert mapping["custom:provider_sub"] == "sub"

    @pytest.mark.asyncio
    async def test_rollback_on_update_client_failure(
        self, auth_repo, cognito_pool
    ):
        """If UpdateUserPoolClient fails, the identity provider should be rolled back."""
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )
        from apis.shared.auth_providers.service import AuthProviderService

        svc = CognitoIdentityProviderService(
            user_pool_id=cognito_pool["pool_id"],
            app_client_id=cognito_pool["client_id"],
            region=AWS_REGION,
        )

        # Patch add_provider_to_app_client to fail
        original_add = svc.add_provider_to_app_client
        def failing_add(name):
            raise ClientError(
                {"Error": {"Code": "InvalidParameterException", "Message": "test failure"}},
                "UpdateUserPoolClient",
            )
        svc.add_provider_to_app_client = failing_add

        service = AuthProviderService(repository=auth_repo, cognito_idp_service=svc)

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await service.create_provider(_make_create())
        assert exc_info.value.status_code == 502

        # Verify the identity provider was rolled back (deleted from Cognito)
        client = cognito_pool["boto_client"]
        with pytest.raises(ClientError) as exc_info:
            client.describe_identity_provider(
                UserPoolId=cognito_pool["pool_id"],
                ProviderName="okta-1",
            )
        assert exc_info.value.response["Error"]["Code"] == "ResourceNotFoundException"

    @pytest.mark.asyncio
    async def test_rollback_on_dynamo_failure(
        self, cognito_pool, secrets_manager, monkeypatch
    ):
        """If DynamoDB write fails, the Cognito identity provider should be rolled back."""
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )
        from apis.shared.auth_providers.repository import AuthProviderRepository
        from apis.shared.auth_providers.service import AuthProviderService

        cognito_svc = CognitoIdentityProviderService(
            user_pool_id=cognito_pool["pool_id"],
            app_client_id=cognito_pool["client_id"],
            region=AWS_REGION,
        )

        # Create a repo that will fail on put_item
        repo = AuthProviderRepository(
            table_name="test-auth-providers",
            secrets_arn="auth-provider-secrets",
            region=AWS_REGION,
        )
        original_put = repo._table.put_item
        def failing_put(**kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "DynamoDB failure"}},
                "PutItem",
            )
        repo._table.put_item = failing_put

        service = AuthProviderService(
            repository=repo, cognito_idp_service=cognito_svc
        )

        with pytest.raises(ClientError):
            await service.create_provider(_make_create())

        # Verify the identity provider was rolled back
        client = cognito_pool["boto_client"]
        with pytest.raises(ClientError) as exc_info:
            client.describe_identity_provider(
                UserPoolId=cognito_pool["pool_id"],
                ProviderName="okta-1",
            )
        assert exc_info.value.response["Error"]["Code"] == "ResourceNotFoundException"

        # Verify it was also removed from App Client
        providers = cognito_svc.get_supported_identity_providers()
        assert "okta-1" not in providers

    @pytest.mark.asyncio
    async def test_create_without_cognito_still_works(self, auth_repo):
        """When Cognito IdP service is None, create_provider should work as before."""
        from apis.shared.auth_providers.service import AuthProviderService

        service = AuthProviderService(repository=auth_repo, cognito_idp_service=None)
        data = _make_create()
        provider = await service.create_provider(data)

        assert provider.provider_id == "okta-1"
        assert provider.cognito_provider_name is None

    @pytest.mark.asyncio
    async def test_create_with_disabled_cognito_skips_registration(self, auth_repo):
        """When Cognito IdP service is disabled, create_provider should skip Cognito."""
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )
        from apis.shared.auth_providers.service import AuthProviderService

        disabled_svc = CognitoIdentityProviderService(
            user_pool_id=None, app_client_id=None
        )
        service = AuthProviderService(
            repository=auth_repo, cognito_idp_service=disabled_svc
        )
        data = _make_create()
        provider = await service.create_provider(data)

        assert provider.provider_id == "okta-1"
        assert provider.cognito_provider_name is None


# ===================================================================
# CognitoIdentityProviderService.update_identity_provider tests
# ===================================================================


class TestUpdateIdentityProvider:
    def test_update_identity_provider_changes_issuer(
        self, cognito_idp_service, cognito_pool
    ):
        """update_identity_provider should update ProviderDetails with new issuer URL."""
        cognito_idp_service.create_identity_provider(
            provider_name="okta-1",
            issuer_url="https://okta.example.com",
            client_id="cid",
            client_secret="secret",
        )

        cognito_idp_service.update_identity_provider(
            provider_name="okta-1",
            issuer_url="https://okta-new.example.com",
        )

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        details = resp["IdentityProvider"]["ProviderDetails"]
        assert details["oidc_issuer"] == "https://okta-new.example.com"
        # Unchanged fields preserved
        assert details["client_id"] == "cid"

    def test_update_identity_provider_changes_client_id_and_secret(
        self, cognito_idp_service, cognito_pool
    ):
        """update_identity_provider should update client_id and client_secret."""
        cognito_idp_service.create_identity_provider(
            provider_name="okta-1",
            issuer_url="https://okta.example.com",
            client_id="old-cid",
            client_secret="old-secret",
        )

        cognito_idp_service.update_identity_provider(
            provider_name="okta-1",
            client_id="new-cid",
            client_secret="new-secret",
        )

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        details = resp["IdentityProvider"]["ProviderDetails"]
        assert details["client_id"] == "new-cid"
        assert details["client_secret"] == "new-secret"
        # Issuer unchanged
        assert details["oidc_issuer"] == "https://okta.example.com"

    def test_update_identity_provider_changes_attribute_mapping(
        self, cognito_idp_service, cognito_pool
    ):
        """update_identity_provider should replace attribute mapping when provided."""
        cognito_idp_service.create_identity_provider(
            provider_name="okta-1",
            issuer_url="https://okta.example.com",
            client_id="cid",
            client_secret="secret",
        )

        new_mapping = {"email": "mail", "custom:provider_sub": "sub"}
        cognito_idp_service.update_identity_provider(
            provider_name="okta-1",
            attribute_mapping=new_mapping,
        )

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        mapping = resp["IdentityProvider"]["AttributeMapping"]
        assert mapping["email"] == "mail"

    def test_update_identity_provider_raises_when_disabled(self):
        """update_identity_provider should raise RuntimeError when service is disabled."""
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )

        svc = CognitoIdentityProviderService(user_pool_id=None, app_client_id=None)
        with pytest.raises(RuntimeError, match="not enabled"):
            svc.update_identity_provider(provider_name="x", issuer_url="https://x.com")


# ===================================================================
# AuthProviderService.update_provider with Cognito sync tests
# ===================================================================


class TestUpdateProviderWithCognito:
    @pytest.mark.asyncio
    async def test_update_syncs_oidc_changes_to_cognito(
        self, service_with_cognito, cognito_pool
    ):
        """Updating OIDC fields should call Cognito UpdateIdentityProvider."""
        # Create a provider first
        data = _make_create()
        await service_with_cognito.create_provider(data)

        # Update issuer_url and client_id
        from apis.shared.auth_providers.models import AuthProviderUpdate

        updates = AuthProviderUpdate(
            issuer_url="https://new-issuer.example.com",
            client_id="new-client-id",
        )
        result = await service_with_cognito.update_provider("okta-1", updates)
        assert result is not None

        # Verify Cognito was updated
        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        details = resp["IdentityProvider"]["ProviderDetails"]
        assert details["oidc_issuer"] == "https://new-issuer.example.com"
        assert details["client_id"] == "new-client-id"

    @pytest.mark.asyncio
    async def test_update_syncs_attribute_mapping_changes(
        self, service_with_cognito, cognito_pool
    ):
        """Updating claim fields should rebuild and sync attribute mapping to Cognito."""
        data = _make_create()
        await service_with_cognito.create_provider(data)

        from apis.shared.auth_providers.models import AuthProviderUpdate

        updates = AuthProviderUpdate(email_claim="mail", name_claim="displayName")
        await service_with_cognito.update_provider("okta-1", updates)

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        mapping = resp["IdentityProvider"]["AttributeMapping"]
        assert mapping["email"] == "mail"
        assert mapping["name"] == "displayName"
        assert mapping["custom:provider_sub"] == "sub"

    @pytest.mark.asyncio
    async def test_update_skips_cognito_when_no_oidc_fields_changed(
        self, service_with_cognito, cognito_pool
    ):
        """Updating non-OIDC fields should NOT call Cognito UpdateIdentityProvider."""
        data = _make_create()
        await service_with_cognito.create_provider(data)

        # Get original Cognito state
        client = cognito_pool["boto_client"]
        before = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        before_details = before["IdentityProvider"]["ProviderDetails"]

        from apis.shared.auth_providers.models import AuthProviderUpdate

        # Only update display_name (not an OIDC field)
        updates = AuthProviderUpdate(display_name="Okta Renamed")
        result = await service_with_cognito.update_provider("okta-1", updates)
        assert result is not None
        assert result.display_name == "Okta Renamed"

        # Cognito provider details should be unchanged
        after = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        after_details = after["IdentityProvider"]["ProviderDetails"]
        assert after_details["oidc_issuer"] == before_details["oidc_issuer"]
        assert after_details["client_id"] == before_details["client_id"]

    @pytest.mark.asyncio
    async def test_update_works_when_cognito_disabled(self, auth_repo):
        """When Cognito is disabled, update_provider should work without Cognito calls."""
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )
        from apis.shared.auth_providers.service import AuthProviderService
        from apis.shared.auth_providers.models import AuthProviderUpdate

        disabled_svc = CognitoIdentityProviderService(
            user_pool_id=None, app_client_id=None
        )
        service = AuthProviderService(
            repository=auth_repo, cognito_idp_service=disabled_svc
        )

        # Create provider without Cognito
        no_cognito_service = AuthProviderService(
            repository=auth_repo, cognito_idp_service=None
        )
        data = _make_create()
        await no_cognito_service.create_provider(data)

        # Update with disabled Cognito service
        updates = AuthProviderUpdate(issuer_url="https://new.example.com")
        result = await service.update_provider("okta-1", updates)
        assert result is not None
        assert result.issuer_url == "https://new.example.com"

    @pytest.mark.asyncio
    async def test_update_cognito_failure_blocks_dynamo_update(
        self, auth_repo, cognito_pool
    ):
        """If Cognito UpdateIdentityProvider fails, DynamoDB should NOT be updated."""
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )
        from apis.shared.auth_providers.service import AuthProviderService
        from apis.shared.auth_providers.models import AuthProviderUpdate
        from fastapi import HTTPException

        cognito_svc = CognitoIdentityProviderService(
            user_pool_id=cognito_pool["pool_id"],
            app_client_id=cognito_pool["client_id"],
            region=AWS_REGION,
        )

        service = AuthProviderService(
            repository=auth_repo, cognito_idp_service=cognito_svc
        )

        # Create provider with Cognito
        data = _make_create()
        await service.create_provider(data)

        # Patch update_identity_provider to fail
        original_update = cognito_svc.update_identity_provider
        def failing_update(**kwargs):
            raise ClientError(
                {"Error": {"Code": "InvalidParameterException", "Message": "test failure"}},
                "UpdateIdentityProvider",
            )
        cognito_svc.update_identity_provider = failing_update

        updates = AuthProviderUpdate(issuer_url="https://should-not-persist.example.com")
        with pytest.raises(HTTPException) as exc_info:
            await service.update_provider("okta-1", updates)
        assert exc_info.value.status_code == 502

        # Verify DynamoDB was NOT updated
        provider = await auth_repo.get_provider("okta-1")
        assert provider.issuer_url == "https://okta.example.com"


# ===================================================================
# AuthProviderService.delete_provider with Cognito cleanup tests
# ===================================================================


class TestDeleteProviderWithCognito:
    @pytest.mark.asyncio
    async def test_delete_provider_with_cognito_registration(
        self, service_with_cognito, cognito_pool, cognito_idp_service, auth_providers_table
    ):
        """Deleting a provider with Cognito registration should remove from Cognito, App Client, and DynamoDB."""
        # Create a provider (registers in Cognito + adds to App Client)
        data = _make_create()
        await service_with_cognito.create_provider(data)

        # Verify it exists in Cognito and App Client
        providers = cognito_idp_service.get_supported_identity_providers()
        assert "okta-1" in providers

        # Delete
        result = await service_with_cognito.delete_provider("okta-1")
        assert result is True

        # Verify removed from Cognito
        client = cognito_pool["boto_client"]
        with pytest.raises(ClientError) as exc_info:
            client.describe_identity_provider(
                UserPoolId=cognito_pool["pool_id"],
                ProviderName="okta-1",
            )
        assert exc_info.value.response["Error"]["Code"] == "ResourceNotFoundException"

        # Verify removed from App Client
        providers = cognito_idp_service.get_supported_identity_providers()
        assert "okta-1" not in providers
        assert "COGNITO" in providers

        # Verify removed from DynamoDB
        resp = auth_providers_table.get_item(
            Key={"PK": "AUTH_PROVIDER#okta-1", "SK": "AUTH_PROVIDER#okta-1"}
        )
        assert "Item" not in resp

    @pytest.mark.asyncio
    async def test_delete_provider_without_cognito_registration(
        self, auth_repo, auth_providers_table
    ):
        """Deleting a provider without cognito_provider_name should skip Cognito calls."""
        from apis.shared.auth_providers.service import AuthProviderService

        # Create provider without Cognito
        service = AuthProviderService(repository=auth_repo, cognito_idp_service=None)
        data = _make_create()
        await service.create_provider(data)

        # Delete — should succeed without touching Cognito
        result = await service.delete_provider("okta-1")
        assert result is True

        # Verify removed from DynamoDB
        resp = auth_providers_table.get_item(
            Key={"PK": "AUTH_PROVIDER#okta-1", "SK": "AUTH_PROVIDER#okta-1"}
        )
        assert "Item" not in resp

    @pytest.mark.asyncio
    async def test_delete_provider_when_cognito_disabled(
        self, auth_repo, auth_providers_table
    ):
        """When Cognito service is disabled, delete should still work for DynamoDB."""
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )
        from apis.shared.auth_providers.service import AuthProviderService

        disabled_svc = CognitoIdentityProviderService(
            user_pool_id=None, app_client_id=None
        )

        # Create provider without Cognito first
        no_cognito_service = AuthProviderService(
            repository=auth_repo, cognito_idp_service=None
        )
        data = _make_create()
        await no_cognito_service.create_provider(data)

        # Delete with disabled Cognito service
        service = AuthProviderService(
            repository=auth_repo, cognito_idp_service=disabled_svc
        )
        result = await service.delete_provider("okta-1")
        assert result is True

        # Verify removed from DynamoDB
        resp = auth_providers_table.get_item(
            Key={"PK": "AUTH_PROVIDER#okta-1", "SK": "AUTH_PROVIDER#okta-1"}
        )
        assert "Item" not in resp

    @pytest.mark.asyncio
    async def test_delete_cognito_failure_still_deletes_from_dynamo(
        self, auth_repo, cognito_pool, auth_providers_table
    ):
        """If Cognito delete fails, DynamoDB delete should still proceed (best-effort)."""
        from apis.shared.auth_providers.cognito_idp_service import (
            CognitoIdentityProviderService,
        )
        from apis.shared.auth_providers.service import AuthProviderService

        cognito_svc = CognitoIdentityProviderService(
            user_pool_id=cognito_pool["pool_id"],
            app_client_id=cognito_pool["client_id"],
            region=AWS_REGION,
        )

        service = AuthProviderService(
            repository=auth_repo, cognito_idp_service=cognito_svc
        )

        # Create provider with Cognito
        data = _make_create()
        await service.create_provider(data)

        # Patch both Cognito methods to fail
        def failing_remove(name):
            raise ClientError(
                {"Error": {"Code": "InternalErrorException", "Message": "test failure"}},
                "UpdateUserPoolClient",
            )

        def failing_delete(name):
            raise ClientError(
                {"Error": {"Code": "InternalErrorException", "Message": "test failure"}},
                "DeleteIdentityProvider",
            )

        cognito_svc.remove_provider_from_app_client = failing_remove
        cognito_svc.delete_identity_provider = failing_delete

        # Delete should still succeed (best-effort Cognito cleanup)
        result = await service.delete_provider("okta-1")
        assert result is True

        # Verify removed from DynamoDB despite Cognito failures
        resp = auth_providers_table.get_item(
            Key={"PK": "AUTH_PROVIDER#okta-1", "SK": "AUTH_PROVIDER#okta-1"}
        )
        assert "Item" not in resp

    @pytest.mark.asyncio
    async def test_delete_nonexistent_provider_returns_false(
        self, service_with_cognito
    ):
        """Deleting a provider that doesn't exist should return False."""
        result = await service_with_cognito.delete_provider("nonexistent-provider")
        assert result is False


# ===================================================================
# Configurable attribute mappings and OIDC discovery tests (Task 6.4)
# ===================================================================


class TestAttributeMappings:
    """Tests for _build_attribute_mapping and custom claim passthrough to Cognito."""

    @pytest.mark.asyncio
    async def test_default_attribute_mapping(
        self, service_with_cognito, cognito_pool
    ):
        """Default claim fields should produce default Cognito attribute mapping."""
        data = _make_create()
        await service_with_cognito.create_provider(data)

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        mapping = resp["IdentityProvider"]["AttributeMapping"]
        # Default: email→email, custom:provider_sub→sub
        assert mapping["email"] == "email"
        assert mapping["custom:provider_sub"] == "sub"

    @pytest.mark.asyncio
    async def test_custom_email_claim_mapping(
        self, service_with_cognito, cognito_pool
    ):
        """Custom email_claim should map Cognito 'email' to the custom claim name."""
        data = _make_create(email_claim="preferred_email")
        await service_with_cognito.create_provider(data)

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        mapping = resp["IdentityProvider"]["AttributeMapping"]
        assert mapping["email"] == "preferred_email"

    @pytest.mark.asyncio
    async def test_all_custom_claim_mappings(
        self, service_with_cognito, cognito_pool
    ):
        """All custom claim fields should be reflected in Cognito attribute mapping."""
        data = _make_create(
            email_claim="mail",
            name_claim="full_name",
            first_name_claim="fname",
            last_name_claim="lname",
            picture_claim="photo_url",
        )
        await service_with_cognito.create_provider(data)

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        mapping = resp["IdentityProvider"]["AttributeMapping"]
        assert mapping["email"] == "mail"
        assert mapping["name"] == "full_name"
        assert mapping["given_name"] == "fname"
        assert mapping["family_name"] == "lname"
        assert mapping["picture"] == "photo_url"
        assert mapping["custom:provider_sub"] == "sub"

    @pytest.mark.asyncio
    async def test_partial_custom_claim_mappings(
        self, service_with_cognito, cognito_pool
    ):
        """Only specified custom claims should appear in mapping; unset ones omitted."""
        data = _make_create(
            name_claim="displayName",
            first_name_claim=None,
            last_name_claim=None,
            picture_claim=None,
        )
        await service_with_cognito.create_provider(data)

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        mapping = resp["IdentityProvider"]["AttributeMapping"]
        assert mapping["email"] == "email"
        assert mapping["name"] == "displayName"
        assert mapping["custom:provider_sub"] == "sub"
        assert "given_name" not in mapping
        assert "family_name" not in mapping
        assert "picture" not in mapping

    @pytest.mark.asyncio
    async def test_provider_sub_always_mapped(
        self, service_with_cognito, cognito_pool
    ):
        """custom:provider_sub→sub should always be present regardless of other claims."""
        data = _make_create(
            email_claim="e",
            first_name_claim=None,
            last_name_claim=None,
            picture_claim=None,
        )
        await service_with_cognito.create_provider(data)

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        mapping = resp["IdentityProvider"]["AttributeMapping"]
        assert mapping["custom:provider_sub"] == "sub"

    @pytest.mark.asyncio
    async def test_update_attribute_mapping_syncs_to_cognito(
        self, service_with_cognito, cognito_pool
    ):
        """Updating claim fields should rebuild and sync attribute mapping to Cognito."""
        data = _make_create()
        await service_with_cognito.create_provider(data)

        from apis.shared.auth_providers.models import AuthProviderUpdate

        updates = AuthProviderUpdate(
            email_claim="work_email",
            first_name_claim="givenName",
            last_name_claim="surname",
        )
        await service_with_cognito.update_provider("okta-1", updates)

        client = cognito_pool["boto_client"]
        resp = client.describe_identity_provider(
            UserPoolId=cognito_pool["pool_id"],
            ProviderName="okta-1",
        )
        mapping = resp["IdentityProvider"]["AttributeMapping"]
        assert mapping["email"] == "work_email"
        assert mapping["given_name"] == "givenName"
        assert mapping["family_name"] == "surname"
        assert mapping["custom:provider_sub"] == "sub"


class TestOIDCDiscoveryAutoDiscover:
    """Tests for the auto_discover flag controlling OIDC endpoint discovery."""

    @pytest.mark.asyncio
    async def test_auto_discover_true_triggers_discovery(
        self, service_with_cognito
    ):
        """When auto_discover=True and endpoints missing, discovery should be attempted."""
        data = _make_create(
            authorization_endpoint=None,
            token_endpoint=None,
            jwks_uri=None,
        )
        data.auto_discover = True

        # Patch discover_endpoints to track if it was called
        called = {"count": 0}
        original = service_with_cognito.discover_endpoints

        async def tracking_discover(issuer_url):
            called["count"] += 1
            from apis.shared.auth_providers.models import OIDCDiscoveryResponse
            return OIDCDiscoveryResponse(
                issuer=issuer_url,
                authorization_endpoint="https://okta.example.com/authorize",
                token_endpoint="https://okta.example.com/token",
                jwks_uri="https://okta.example.com/keys",
                userinfo_endpoint="https://okta.example.com/userinfo",
            )

        service_with_cognito.discover_endpoints = tracking_discover

        provider = await service_with_cognito.create_provider(data)
        assert called["count"] == 1
        assert provider.authorization_endpoint == "https://okta.example.com/authorize"
        assert provider.token_endpoint == "https://okta.example.com/token"
        assert provider.jwks_uri == "https://okta.example.com/keys"

    @pytest.mark.asyncio
    async def test_auto_discover_false_skips_discovery(
        self, service_with_cognito
    ):
        """When auto_discover=False, discovery should NOT be attempted even if endpoints missing."""
        data = _make_create(
            authorization_endpoint=None,
            token_endpoint=None,
            jwks_uri=None,
        )
        data.auto_discover = False

        # Patch discover_endpoints to track if it was called
        called = {"count": 0}

        async def tracking_discover(issuer_url):
            called["count"] += 1
            from apis.shared.auth_providers.models import OIDCDiscoveryResponse
            return OIDCDiscoveryResponse(issuer=issuer_url)

        service_with_cognito.discover_endpoints = tracking_discover

        provider = await service_with_cognito.create_provider(data)
        assert called["count"] == 0
        # Endpoints remain None since discovery was skipped
        assert provider.authorization_endpoint is None
        assert provider.token_endpoint is None
        assert provider.jwks_uri is None

    @pytest.mark.asyncio
    async def test_auto_discover_default_is_false(self):
        """auto_discover should default to False (opt-in)."""
        data = _make_create()
        assert data.auto_discover is False

    @pytest.mark.asyncio
    async def test_auto_discover_skipped_when_endpoints_provided(
        self, service_with_cognito
    ):
        """When all endpoints are already provided, discovery should not run even with auto_discover=True."""
        data = _make_create(
            authorization_endpoint="https://okta.example.com/authorize",
            token_endpoint="https://okta.example.com/token",
            jwks_uri="https://okta.example.com/keys",
        )
        data.auto_discover = True

        called = {"count": 0}

        async def tracking_discover(issuer_url):
            called["count"] += 1
            from apis.shared.auth_providers.models import OIDCDiscoveryResponse
            return OIDCDiscoveryResponse(issuer=issuer_url)

        service_with_cognito.discover_endpoints = tracking_discover

        provider = await service_with_cognito.create_provider(data)
        assert called["count"] == 0
        assert provider.authorization_endpoint == "https://okta.example.com/authorize"

    @pytest.mark.asyncio
    async def test_auto_discover_populates_missing_endpoints_only(
        self, service_with_cognito
    ):
        """Discovery should only fill in missing endpoints, not overwrite provided ones."""
        data = _make_create(
            authorization_endpoint="https://custom.example.com/auth",
            token_endpoint=None,
            jwks_uri=None,
        )
        data.auto_discover = True

        async def mock_discover(issuer_url):
            from apis.shared.auth_providers.models import OIDCDiscoveryResponse
            return OIDCDiscoveryResponse(
                issuer=issuer_url,
                authorization_endpoint="https://discovered.example.com/authorize",
                token_endpoint="https://discovered.example.com/token",
                jwks_uri="https://discovered.example.com/keys",
                userinfo_endpoint="https://discovered.example.com/userinfo",
            )

        service_with_cognito.discover_endpoints = mock_discover

        provider = await service_with_cognito.create_provider(data)
        # Provided endpoint should NOT be overwritten
        assert provider.authorization_endpoint == "https://custom.example.com/auth"
        # Missing endpoints should be filled from discovery
        assert provider.token_endpoint == "https://discovered.example.com/token"
        assert provider.jwks_uri == "https://discovered.example.com/keys"
