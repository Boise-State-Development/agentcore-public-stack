"""
Test fixtures for runtime-provisioner Lambda.

Handles the tricky module-level side effects:
  - pip install at import time (lines 20-22)
  - boto3 client creation at module level (lines 31-34)
  - os.environ reads at module level (lines 37-39)
"""
import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_PREFIX = "test-project"
AWS_REGION = "us-east-1"
AUTH_PROVIDERS_TABLE = "test-auth-providers"

# All SSM parameters the Lambda reads, with deterministic test values.
SSM_PARAMS: dict[str, str] = {
    # DynamoDB tables (get_runtime_environment_variables)
    f"/{PROJECT_PREFIX}/users/users-table-name": "test-users-table",
    f"/{PROJECT_PREFIX}/rbac/app-roles-table-name": "test-app-roles-table",
    f"/{PROJECT_PREFIX}/auth/oidc-state-table-name": "test-oidc-state-table",
    f"/{PROJECT_PREFIX}/auth/api-keys-table-name": "test-api-keys-table",
    f"/{PROJECT_PREFIX}/oauth/providers-table-name": "test-oauth-providers-table",
    f"/{PROJECT_PREFIX}/oauth/user-tokens-table-name": "test-user-tokens-table",
    f"/{PROJECT_PREFIX}/rag/assistants-table-name": "test-assistants-table",
    f"/{PROJECT_PREFIX}/quota/user-quotas-table-name": "test-user-quotas-table",
    f"/{PROJECT_PREFIX}/quota/quota-events-table-name": "test-quota-events-table",
    f"/{PROJECT_PREFIX}/cost-tracking/sessions-metadata-table-name": "test-sessions-metadata-table",
    f"/{PROJECT_PREFIX}/cost-tracking/user-cost-summary-table-name": "test-user-cost-summary-table",
    f"/{PROJECT_PREFIX}/cost-tracking/system-cost-rollup-table-name": "test-system-cost-rollup-table",
    f"/{PROJECT_PREFIX}/admin/managed-models-table-name": "test-managed-models-table",
    # File upload
    f"/{PROJECT_PREFIX}/user-file-uploads/table-name": "test-user-files-table",
    # Auth / OAuth secrets & URLs
    f"/{PROJECT_PREFIX}/auth/auth-provider-secrets-arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-auth-secrets",
    f"/{PROJECT_PREFIX}/oauth/token-encryption-key-arn": "arn:aws:kms:us-east-1:123456789012:key/test-token-key",
    f"/{PROJECT_PREFIX}/oauth/client-secrets-arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-client-secrets",
    f"/{PROJECT_PREFIX}/oauth/callback-url": "https://app.example.com/oauth/callback",
    # S3 / RAG
    f"/{PROJECT_PREFIX}/rag/vector-bucket-name": "test-vector-bucket",
    f"/{PROJECT_PREFIX}/rag/vector-index-name": "test-vector-index",
    # Network / Frontend
    f"/{PROJECT_PREFIX}/network/alb-url": "https://alb.example.com",
    f"/{PROJECT_PREFIX}/frontend/url": "https://app.example.com",
    f"/{PROJECT_PREFIX}/frontend/cors-origins": "https://app.example.com",
    # create_runtime() params
    f"/{PROJECT_PREFIX}/inference-api/image-tag": "latest",
    f"/{PROJECT_PREFIX}/inference-api/ecr-repository-uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/test-repo",
    f"/{PROJECT_PREFIX}/inference-api/runtime-execution-role-arn": "arn:aws:iam::123456789012:role/test-runtime-role",
    # Shared resources (get_shared_resource_ids)
    f"/{PROJECT_PREFIX}/inference-api/memory-arn": "arn:aws:bedrock:us-east-1:123456789012:memory/test-memory",
    f"/{PROJECT_PREFIX}/inference-api/memory-id": "test-memory-id",
    f"/{PROJECT_PREFIX}/inference-api/code-interpreter-id": "test-code-interpreter-id",
    f"/{PROJECT_PREFIX}/inference-api/browser-id": "test-browser-id",
    f"/{PROJECT_PREFIX}/gateway/gateway-url": "https://gateway.example.com",
}

# ---------------------------------------------------------------------------
# A – Environment variables  (autouse so every test gets them)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env_vars(monkeypatch):
    """Set the environment variables the Lambda reads at module level."""
    monkeypatch.setenv("PROJECT_PREFIX", PROJECT_PREFIX)
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AUTH_PROVIDERS_TABLE", AUTH_PROVIDERS_TABLE)
    # moto needs a dummy credential set
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


# ---------------------------------------------------------------------------
# B – pip install no-op
# ---------------------------------------------------------------------------

@pytest.fixture()
def _patch_pip():
    """Prevent the Lambda from running pip install at import time."""
    with patch("pip._internal.main", return_value=None):
        yield


# ---------------------------------------------------------------------------
# C / D / E – moto-backed AWS services + bedrock mock + module import
# ---------------------------------------------------------------------------

def _make_mock_bedrock_client() -> MagicMock:
    """Return a MagicMock that behaves like a bedrock-agentcore-control client."""
    client = MagicMock(name="bedrock-agentcore-control")
    client.create_agent_runtime.return_value = {
        "agentRuntimeArn": "arn:aws:bedrock:us-east-1:123456789012:agent-runtime/test-runtime-id",
        "agentRuntimeId": "test-runtime-id",
    }
    client.update_agent_runtime.return_value = {}
    client.delete_agent_runtime.return_value = {}
    client.get_agent_runtime.return_value = {
        "agentRuntimeId": "test-runtime-id",
        "agentRuntimeArn": "arn:aws:bedrock:us-east-1:123456789012:agent-runtime/test-runtime-id",
        "agentRuntimeName": "test_project_runtime_provider1",
        "agentRuntimeArtifact": {
            "containerConfiguration": {
                "containerUri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/test-repo:latest",
            }
        },
        "authorizerConfiguration": {
            "customJWTAuthorizer": {
                "discoveryUrl": "https://auth.example.com/.well-known/openid-configuration",
                "allowedAudience": ["test-client-id"],
            }
        },
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "roleArn": "arn:aws:iam::123456789012:role/test-runtime-role",
        "requestHeaderConfiguration": {
            "requestHeaderAllowlist": ["Authorization"]
        },
        "environmentVariables": {"TABLE_NAME": "my-table", "API_KEY": "secret-123"},
        "status": "ACTIVE",
    }
    return client


@pytest.fixture()
def mock_bedrock_client():
    """Expose the mock bedrock-agentcore-control client for assertions."""
    return _make_mock_bedrock_client()


@pytest.fixture()
def lambda_module(_env_vars, _patch_pip, mock_bedrock_client):
    """Import (or reimport) lambda_function inside fully-mocked AWS context.

    Yields a tuple of ``(module, mock_bedrock_client)`` so tests can
    both invoke handlers and assert on the bedrock mock.
    """
    bedrock_mock = mock_bedrock_client

    # Intercept boto3.client: route bedrock-agentcore-control to our mock,
    # let everything else fall through to moto.
    _real_boto3_client = boto3.client

    def _patched_client(service_name, *args, **kwargs):
        if service_name == "bedrock-agentcore-control":
            return bedrock_mock
        return _real_boto3_client(service_name, *args, **kwargs)

    with mock_aws():
        # Pre-populate moto resources BEFORE the module import so that
        # module-level code (and any eager reads) find them.
        _create_dynamodb_table()
        _create_ssm_parameters()

        with patch("boto3.client", side_effect=_patched_client):
            # Remove cached module so the reload picks up our patches.
            sys.modules.pop("lambda_function", None)

            # Ensure the Lambda directory is on sys.path so the bare
            # ``import lambda_function`` inside the fixture works.
            lambda_dir = os.path.join(
                os.path.dirname(__file__), os.pardir
            )
            abs_lambda_dir = os.path.abspath(lambda_dir)
            if abs_lambda_dir not in sys.path:
                sys.path.insert(0, abs_lambda_dir)

            import lambda_function  # noqa: F811

            importlib.reload(lambda_function)

            # Replace module-level client refs so test-time calls also
            # go through moto / mock (reload already did this, but be
            # explicit for safety).
            lambda_function.bedrock_agentcore = bedrock_mock

            yield lambda_function, bedrock_mock

        # Cleanup: remove from sys.modules to avoid polluting other tests.
        sys.modules.pop("lambda_function", None)


# ---------------------------------------------------------------------------
# D – DynamoDB table
# ---------------------------------------------------------------------------

def _create_dynamodb_table():
    """Create the AuthProviders table in moto."""
    client = boto3.client("dynamodb", region_name=AWS_REGION)
    client.create_table(
        TableName=AUTH_PROVIDERS_TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        StreamSpecification={
            "StreamEnabled": True,
            "StreamViewType": "NEW_AND_OLD_IMAGES",
        },
    )


@pytest.fixture()
def auth_providers_table(lambda_module):
    """Return a ready-to-use DynamoDB table name (table already created)."""
    return AUTH_PROVIDERS_TABLE


# ---------------------------------------------------------------------------
# E – SSM parameters
# ---------------------------------------------------------------------------

def _create_ssm_parameters():
    """Seed all SSM parameters into moto."""
    client = boto3.client("ssm", region_name=AWS_REGION)
    for name, value in SSM_PARAMS.items():
        client.put_parameter(Name=name, Value=value, Type="String")


# ---------------------------------------------------------------------------
# G – DynamoDB Stream event factories
# ---------------------------------------------------------------------------

def make_insert_event(
    provider_id: str,
    issuer_url: str,
    client_id: str,
    jwks_uri: str | None = None,
    display_name: str | None = None,
) -> dict:
    """Create a DynamoDB Stream INSERT event."""
    new_image: dict = {
        "PK": {"S": f"AUTH_PROVIDER#{provider_id}"},
        "SK": {"S": f"AUTH_PROVIDER#{provider_id}"},
        "providerId": {"S": provider_id},
        "issuerUrl": {"S": issuer_url},
        "clientId": {"S": client_id},
        "displayName": {"S": display_name or f"Test Provider {provider_id}"},
    }
    if jwks_uri is not None:
        new_image["jwksUri"] = {"S": jwks_uri}
    return {
        "Records": [
            {
                "eventName": "INSERT",
                "dynamodb": {"NewImage": new_image},
            }
        ]
    }


def make_modify_event(
    provider_id: str,
    old_issuer_url: str,
    new_issuer_url: str,
    old_client_id: str,
    new_client_id: str,
    runtime_id: str = "test-runtime-id",
    old_jwks_uri: str | None = None,
    new_jwks_uri: str | None = None,
) -> dict:
    """Create a DynamoDB Stream MODIFY event."""
    old_image: dict = {
        "PK": {"S": f"AUTH_PROVIDER#{provider_id}"},
        "SK": {"S": f"AUTH_PROVIDER#{provider_id}"},
        "providerId": {"S": provider_id},
        "issuerUrl": {"S": old_issuer_url},
        "clientId": {"S": old_client_id},
        "displayName": {"S": f"Test Provider {provider_id}"},
        "agentcoreRuntimeId": {"S": runtime_id},
    }
    new_image: dict = {
        "PK": {"S": f"AUTH_PROVIDER#{provider_id}"},
        "SK": {"S": f"AUTH_PROVIDER#{provider_id}"},
        "providerId": {"S": provider_id},
        "issuerUrl": {"S": new_issuer_url},
        "clientId": {"S": new_client_id},
        "displayName": {"S": f"Test Provider {provider_id}"},
        "agentcoreRuntimeId": {"S": runtime_id},
    }
    if old_jwks_uri is not None:
        old_image["jwksUri"] = {"S": old_jwks_uri}
    if new_jwks_uri is not None:
        new_image["jwksUri"] = {"S": new_jwks_uri}
    return {
        "Records": [
            {
                "eventName": "MODIFY",
                "dynamodb": {
                    "OldImage": old_image,
                    "NewImage": new_image,
                },
            }
        ]
    }


def make_remove_event(
    provider_id: str,
    runtime_id: str | None = None,
) -> dict:
    """Create a DynamoDB Stream REMOVE event."""
    old_image: dict = {
        "PK": {"S": f"AUTH_PROVIDER#{provider_id}"},
        "SK": {"S": f"AUTH_PROVIDER#{provider_id}"},
        "providerId": {"S": provider_id},
        "displayName": {"S": f"Test Provider {provider_id}"},
    }
    if runtime_id is not None:
        old_image["agentcoreRuntimeId"] = {"S": runtime_id}
    return {
        "Records": [
            {
                "eventName": "REMOVE",
                "dynamodb": {"OldImage": old_image},
            }
        ]
    }
