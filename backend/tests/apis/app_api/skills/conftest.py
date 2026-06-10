"""Moto fixtures for the skills admin service + routes tests.

One app-roles table holds SKILL#, TOOL#, and ROLE# items (the skill catalog,
tool catalog, and RBAC roles all share it), so a single moto table backs the
whole SkillCatalogService.
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.auth.models import User

AWS_REGION = "us-east-1"
TABLE = "test-app-roles"
SKILL_RESOURCES_BUCKET = "test-skill-resources"


def _gsi(name, hash_key, range_key):
    return {
        "IndexName": name,
        "KeySchema": [
            {"AttributeName": hash_key, "KeyType": "HASH"},
            {"AttributeName": range_key, "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }


@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture(autouse=True)
def _reset_skill_freshness():
    from apis.shared.skills import freshness

    freshness._reset_for_tests()
    yield
    freshness._reset_for_tests()


@pytest.fixture()
def app_roles_table(aws, monkeypatch):
    monkeypatch.setenv("DYNAMODB_APP_ROLES_TABLE_NAME", TABLE)
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
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
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            _gsi("JwtRoleMappingIndex", "GSI1PK", "GSI1SK"),
            _gsi("ToolRoleMappingIndex", "GSI2PK", "GSI2SK"),
            _gsi("ModelRoleMappingIndex", "GSI3PK", "GSI3SK"),
            _gsi("SkillOwnerIndex", "GSI4PK", "GSI4SK"),
        ],
    )
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE)


@pytest.fixture()
def tool_repo(app_roles_table):
    """ToolCatalogRepository bound to the moto table (for seeding bound tools)."""
    from apis.shared.tools.repository import ToolCatalogRepository

    return ToolCatalogRepository(table_name=TABLE)


@pytest.fixture()
def skill_resource_store(aws):
    """SkillResourceStore bound to a moto S3 bucket (for reference files)."""
    from apis.shared.skills.resource_store import SkillResourceStore

    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.create_bucket(Bucket=SKILL_RESOURCES_BUCKET)
    return SkillResourceStore(
        bucket_name=SKILL_RESOURCES_BUCKET,
        s3_client=s3,
    )


@pytest.fixture()
def skill_service(app_roles_table, skill_resource_store):
    """SkillCatalogService wired to the moto table with isolated RBAC caches."""
    from apis.app_api.skills.service import SkillCatalogService
    from apis.shared.rbac.admin_service import AppRoleAdminService
    from apis.shared.rbac.cache import AppRoleCache
    from apis.shared.rbac.repository import AppRoleRepository
    from apis.shared.rbac.service import AppRoleService
    from apis.shared.skills.repository import SkillCatalogRepository
    from apis.shared.tools.repository import ToolCatalogRepository

    cache = AppRoleCache()
    role_repo = AppRoleRepository(table_name=TABLE)
    return SkillCatalogService(
        repository=SkillCatalogRepository(table_name=TABLE),
        tool_repository=ToolCatalogRepository(table_name=TABLE),
        app_role_service=AppRoleService(repository=role_repo, cache=cache),
        app_role_admin_service=AppRoleAdminService(repository=role_repo, cache=cache),
        resource_store=skill_resource_store,
    )


@pytest.fixture()
def admin_user() -> User:
    return User(
        user_id="admin-1",
        email="admin@example.com",
        name="Admin",
        roles=["system_admin"],
        raw_token="test-token",
    )
