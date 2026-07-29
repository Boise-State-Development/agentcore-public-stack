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
            {"AttributeName": "GSI4PK", "AttributeType": "S"},
            {"AttributeName": "GSI4SK", "AttributeType": "S"},
        ],
        gsis=[
            _gsi("JwtRoleMappingIndex", "GSI1PK", "GSI1SK"),
            _gsi("ToolRoleMappingIndex", "GSI2PK", "GSI2SK"),
            _gsi("ModelRoleMappingIndex", "GSI3PK", "GSI3SK"),
            _gsi("SkillOwnerIndex", "GSI4PK", "GSI4SK"),
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
            {"AttributeName": "GSI3_PK", "AttributeType": "S"},
            {"AttributeName": "GSI3_SK", "AttributeType": "S"},
            {"AttributeName": "GSI4_PK", "AttributeType": "S"},
            {"AttributeName": "GSI4_SK", "AttributeType": "S"},
        ],
        gsis=[
            _gsi("UserTimestampIndex", "GSI1PK", "GSI1SK"),
            _gsi("SessionLookupIndex", "GSI_PK", "GSI_SK"),
            _gsi("DueScheduleIndex", "GSI3_PK", "GSI3_SK"),
            _gsi("SessionRecencyIndex", "GSI4_PK", "GSI4_SK"),
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
            {"AttributeName": "GSI4_PK", "AttributeType": "S"},
            {"AttributeName": "GSI4_SK", "AttributeType": "S"},
        ],
        gsis=[
            _gsi("OwnerStatusIndex", "GSI_PK", "GSI_SK"),
            _gsi("VisibilityStatusIndex", "GSI2_PK", "GSI2_SK"),
            _gsi("SharedWithIndex", "GSI3_PK", "GSI3_SK"),
            _gsi("DueSyncIndex", "GSI4_PK", "GSI4_SK"),
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


@pytest.fixture()
def skill_repository(roles_table):
    from apis.shared.skills.repository import SkillCatalogRepository
    return SkillCatalogRepository(table_name="test-app-roles")


async def publish_agent_version(
    agent_id: str,
    category: str,
    created_at: str,
    *,
    publisher_id: str = "pub-registrar",
) -> int:
    """Publish a seeded Agent the way production does, and return the version number.

    Shared by every marketplace test that needs something *on the shelf*, because getting
    onto the shelf is no longer a single write. A published listing is three facts that have
    to agree: an immutable ``VERSION#`` snapshot, a ``listing`` block pointing at it, and the
    GSI5 key on that snapshot row.

    ⚠️ **Deliberately not a shortcut.** The obvious test helper — put the GSI5 keys straight
    onto the Agent row — is exactly the shape this feature removed, and a test that seeded
    it that way would keep passing while the store served the author's draft. Going through
    ``create_version`` / ``set_version_index`` means these tests break if the real publish
    path stops writing what browse reads, which is the whole reason they run against a real
    (moto) table instead of a mock.
    """
    from apis.shared.assistants.listing import gsi5_keys
    from apis.shared.assistants.listing_repository import write_listing
    from apis.shared.assistants.models import AgentListing, Assistant
    from apis.shared.assistants.serialization import from_ddb
    from apis.shared.assistants.version_repository import create_version, set_version_index
    from apis.shared.assistants.versions import snapshot_of

    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(
        os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"]
    )
    item = table.get_item(Key={"PK": f"AST#{agent_id}", "SK": "METADATA"})["Item"]
    assistant = Assistant.model_validate(from_ddb(item))

    listing = AgentListing(state="published", category=category, publisher_id=publisher_id)
    version = await create_version(
        agent_id,
        snapshot_of(assistant.model_copy(update={"listing": listing}), created_at=created_at),
    )
    await write_listing(
        agent_id,
        listing.model_copy(
            update={"published_version": version.version, "submitted_version": version.version}
        ),
        created_at,
    )
    await set_version_index(agent_id, version.version, gsi5_keys("published", category, created_at))
    return version.version


async def unpublish_agent_version(
    agent_id: str,
    category: str,
    created_at: str,
    *,
    state: str = "taken_down",
    publisher_id: str = "pub-registrar",
) -> None:
    """Take a published Agent off the shelf the way production does.

    Clears the key **before** rewriting the listing block, which is the ordering
    ``listing_service._unindex_version`` documents: with the index on a different item than
    the listing, a half-failed delisting has to leave the Agent invisible rather than
    visible-but-recorded-down.
    """
    from apis.shared.assistants.listing_repository import write_listing
    from apis.shared.assistants.models import AgentListing
    from apis.shared.assistants.version_repository import get_latest_version, set_version_index

    latest = await get_latest_version(agent_id)
    if latest and latest.version is not None:
        await set_version_index(agent_id, latest.version, None)
    await write_listing(
        agent_id,
        AgentListing(state=state, category=category, publisher_id=publisher_id),
        created_at,
    )
