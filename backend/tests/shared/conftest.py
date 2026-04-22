"""Shared conftest: moto fixtures for DynamoDB, KMS, S3, Secrets Manager.

Every fixture uses @mock_aws so tests hit fake AWS services with real
query logic (GSI queries, update_item expressions, etc.).
"""

import os
import pytest
import boto3
from moto import mock_aws

AWS_REGION = "us-east-1"


@pytest.fixture()
def aws(monkeypatch):
    """Activate moto mock_aws and set default env vars."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


# ===================================================================
# DynamoDB helpers
# ===================================================================

def _create_table(dynamodb, table_name, key_schema, attribute_defs, gsis=None):
    params = dict(
        TableName=table_name,
        KeySchema=key_schema,
        AttributeDefinitions=attribute_defs,
        BillingMode="PAY_PER_REQUEST",
    )
    if gsis:
        params["GlobalSecondaryIndexes"] = gsis
    dynamodb.create_table(**params)
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(table_name)


def _pk_sk_schema():
    return [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]


def _gsi(name, hash_key, range_key=None):
    ks = [{"AttributeName": hash_key, "KeyType": "HASH"}]
    if range_key:
        ks.append({"AttributeName": range_key, "KeyType": "RANGE"})
    return {
        "IndexName": name,
        "KeySchema": ks,
        "Projection": {"ProjectionType": "ALL"},
    }


# ===================================================================
# Users table
# ===================================================================
@pytest.fixture()
def users_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-users"
    monkeypatch.setenv("DYNAMODB_USERS_TABLE_NAME", name)
    return _create_table(
        ddb, name, _pk_sk_schema(),
        [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "userId", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
            {"AttributeName": "GSI3PK", "AttributeType": "S"},
            {"AttributeName": "GSI3SK", "AttributeType": "S"},
        ],
        gsis=[
            _gsi("UserIdIndex", "userId"),
            _gsi("EmailIndex", "email"),
            _gsi("EmailDomainIndex", "GSI2PK", "GSI2SK"),
            _gsi("StatusLoginIndex", "GSI3PK", "GSI3SK"),
        ],
    )


# ===================================================================
# Auth providers table
# ===================================================================
@pytest.fixture()
def auth_providers_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-auth-providers"
    monkeypatch.setenv("DYNAMODB_AUTH_PROVIDERS_TABLE_NAME", name)
    return _create_table(
        ddb, name, _pk_sk_schema(),
        [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
        ],
        gsis=[_gsi("EnabledProvidersIndex", "GSI1PK")],
    )


# ===================================================================
# OAuth providers table
# ===================================================================
@pytest.fixture()
def oauth_providers_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-oauth-providers"
    monkeypatch.setenv("DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME", name)
    return _create_table(
        ddb, name, _pk_sk_schema(),
        [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
        ],
        gsis=[_gsi("EnabledProvidersIndex", "GSI1PK")],
    )


# ===================================================================
# OAuth user tokens table
# ===================================================================
@pytest.fixture()
def oauth_tokens_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-oauth-user-tokens"
    monkeypatch.setenv("DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME", name)
    return _create_table(
        ddb, name, _pk_sk_schema(),
        [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
        ],
        gsis=[_gsi("ProviderUsersIndex", "GSI1PK")],
    )


# ===================================================================
# User files table
# ===================================================================
@pytest.fixture()
def files_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-user-files"
    monkeypatch.setenv("DYNAMODB_USER_FILES_TABLE_NAME", name)
    return _create_table(
        ddb, name, _pk_sk_schema(),
        [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        gsis=[_gsi("SessionIndex", "GSI1PK", "GSI1SK")],
    )


# ===================================================================
# App roles table
# ===================================================================
@pytest.fixture()
def roles_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-app-roles"
    monkeypatch.setenv("DYNAMODB_APP_ROLES_TABLE_NAME", name)
    return _create_table(
        ddb, name, _pk_sk_schema(),
        [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
            {"AttributeName": "GSI3PK", "AttributeType": "S"},
            {"AttributeName": "GSI3SK", "AttributeType": "S"},
        ],
        gsis=[
            _gsi("JwtRoleMappingIndex", "GSI1PK", "GSI1SK"),
            _gsi("ToolRoleMappingIndex", "GSI2PK", "GSI2SK"),
            _gsi("ModelRoleMappingIndex", "GSI3PK", "GSI3SK"),
        ],
    )


# ===================================================================
# Managed models table
# ===================================================================
@pytest.fixture()
def managed_models_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-managed-models"
    monkeypatch.setenv("DYNAMODB_MANAGED_MODELS_TABLE_NAME", name)
    return _create_table(
        ddb, name, _pk_sk_schema(),
        [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        gsis=[_gsi("ModelIdIndex", "GSI1PK", "GSI1SK")],
    )


# ===================================================================
# Sessions metadata table
# ===================================================================
@pytest.fixture()
def sessions_metadata_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-sessions-metadata"
    monkeypatch.setenv("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", name)
    return _create_table(
        ddb, name, _pk_sk_schema(),
        [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI_PK", "AttributeType": "S"},
            {"AttributeName": "GSI_SK", "AttributeType": "S"},
        ],
        gsis=[
            _gsi("UserTimestampIndex", "GSI1PK", "GSI1SK"),
            _gsi("SessionLookupIndex", "GSI_PK", "GSI_SK"),
        ],
    )


# ===================================================================
# Assistants table
# ===================================================================
@pytest.fixture()
def assistants_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    name = "test-assistants"
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", name)
    return _create_table(
        ddb, name, _pk_sk_schema(),
        [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI_PK", "AttributeType": "S"},
            {"AttributeName": "GSI_SK", "AttributeType": "S"},
            {"AttributeName": "GSI2_PK", "AttributeType": "S"},
            {"AttributeName": "GSI2_SK", "AttributeType": "S"},
            {"AttributeName": "GSI3_PK", "AttributeType": "S"},
            {"AttributeName": "GSI3_SK", "AttributeType": "S"},
        ],
        gsis=[
            _gsi("OwnerStatusIndex", "GSI_PK", "GSI_SK"),
            _gsi("VisibilityStatusIndex", "GSI2_PK", "GSI2_SK"),
            _gsi("SharedWithIndex", "GSI3_PK", "GSI3_SK"),
        ],
    )


# ===================================================================
# KMS key
# ===================================================================
@pytest.fixture()
def kms_key_arn(aws, monkeypatch):
    kms = boto3.client("kms", region_name=AWS_REGION)
    key = kms.create_key(Description="test-oauth-encryption")
    arn = key["KeyMetadata"]["Arn"]
    monkeypatch.setenv("OAUTH_TOKEN_ENCRYPTION_KEY_ARN", arn)
    return arn


# ===================================================================
# S3 bucket
# ===================================================================
@pytest.fixture()
def s3_bucket(aws, monkeypatch):
    s3 = boto3.client("s3", region_name=AWS_REGION)
    bucket = "test-file-uploads"
    s3.create_bucket(Bucket=bucket)
    monkeypatch.setenv("FILE_UPLOAD_BUCKET", bucket)
    return bucket


# ===================================================================
# Secrets Manager
# ===================================================================
@pytest.fixture()
def secrets_manager(aws, monkeypatch):
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    sm.create_secret(Name="auth-provider-secrets", SecretString="{}")
    monkeypatch.setenv("AUTH_PROVIDER_SECRETS_ARN", "auth-provider-secrets")
    sm.create_secret(Name="oauth-client-secrets", SecretString="{}")
    monkeypatch.setenv("OAUTH_CLIENT_SECRETS_ARN", "oauth-client-secrets")
    return sm


# ===================================================================
# Repository factories
# ===================================================================
@pytest.fixture()
def user_repository(users_table):
    from apis.shared.users.repository import UserRepository
    return UserRepository(table_name="test-users")


@pytest.fixture()
def auth_provider_repository(auth_providers_table, secrets_manager):
    from apis.shared.auth_providers.repository import AuthProviderRepository
    return AuthProviderRepository(
        table_name="test-auth-providers",
        secrets_arn="auth-provider-secrets",
        region=AWS_REGION,
    )


@pytest.fixture()
def oauth_provider_repository(oauth_providers_table):
    from apis.shared.oauth.provider_repository import OAuthProviderRepository
    return OAuthProviderRepository(
        table_name="test-oauth-providers",
        region=AWS_REGION,
    )


@pytest.fixture()
def file_repository(files_table):
    from apis.shared.files.repository import FileUploadRepository
    return FileUploadRepository(table_name="test-user-files")


@pytest.fixture()
def role_repository(roles_table):
    from apis.shared.rbac.repository import AppRoleRepository
    return AppRoleRepository(table_name="test-app-roles")
