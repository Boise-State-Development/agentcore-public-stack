"""``scripts/seed_e2e_role.py`` — the RBAC role the nightly E2E user runs under.

Pins what the turn-path smoke matrix depends on: the role maps to the Cognito
group ``seed-e2e-users.sh`` puts the user in, grants exactly the two tools the
matrix exercises plus every model, mirrors each grant into
``effective_permissions`` (the half the access checks actually read), and is
idempotent — a second run updates in place rather than failing on the
existing row.
"""

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from seed_e2e_role import (  # noqa: E402
    DEFAULT_GROUP,
    DEFAULT_TOOLS,
    ROLE_ID,
    build_role,
    parse_args,
    upsert,
)

TABLE_NAME = "test-app-roles"
REGION = "us-east-1"


@pytest.fixture
def repository():
    """A real AppRoleRepository over a moto table with the app-roles schema."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "JwtRoleMappingIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName=TABLE_NAME)
        os.environ["AWS_DEFAULT_REGION"] = REGION
        from apis.shared.rbac.repository import AppRoleRepository

        yield AppRoleRepository(table_name=TABLE_NAME), table


class TestBuildRole:
    def test_defaults_match_what_the_smoke_matrix_needs(self):
        role = build_role(group=DEFAULT_GROUP, tools=DEFAULT_TOOLS, models=["*"])
        assert role.role_id == ROLE_ID
        assert role.jwt_role_mappings == ["e2e-users"]
        assert role.granted_tools == ["ask_user_question", "calculator"]
        assert role.granted_models == ["*"]
        assert role.granted_skills == []
        assert role.enabled is True
        assert role.is_system_role is False

    def test_effective_permissions_mirror_the_grants(self):
        # The access checks read effective_permissions; a role with only the
        # granted_* half grants nothing (CLAUDE.md, RBAC).
        role = build_role(group="g", tools=["b_tool", "a_tool", "a_tool"], models=["m2", "m1"])
        assert role.effective_permissions.tools == role.granted_tools == ["a_tool", "b_tool"]
        assert role.effective_permissions.models == role.granted_models == ["m1", "m2"]
        assert role.effective_permissions.skills == []
        assert role.effective_permissions.quota_tier is None

    def test_cli_defaults(self):
        args = parse_args([])
        assert args.group == DEFAULT_GROUP
        assert args.tools.split(",") == DEFAULT_TOOLS
        assert args.models == "*"
        assert args.dry_run is False


class TestUpsert:
    @pytest.mark.asyncio
    async def test_first_run_creates_every_row_the_resolver_reads(self, repository):
        repo, table = repository
        role = build_role(group=DEFAULT_GROUP, tools=DEFAULT_TOOLS, models=["*"])

        assert await upsert(role, repo) == "created"

        items = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(f"ROLE#{ROLE_ID}"))["Items"]
        sks = sorted(i["SK"] for i in items)
        assert sks == [
            "DEFINITION",
            "JWT_MAPPING#e2e-users",
            "MODEL_GRANT#*",
            "TOOL_GRANT#ask_user_question",
            "TOOL_GRANT#calculator",
        ]
        definition = next(i for i in items if i["SK"] == "DEFINITION")
        assert definition["effectivePermissions"]["tools"] == ["ask_user_question", "calculator"]
        assert definition["effectivePermissions"]["models"] == ["*"]
        # The JWT mapping row is what the login-time resolver's GSI query hits.
        assert await repo.get_roles_for_jwt_role("e2e-users") == [ROLE_ID]

    @pytest.mark.asyncio
    async def test_second_run_updates_in_place(self, repository):
        repo, table = repository
        assert await upsert(build_role(group=DEFAULT_GROUP, tools=["calculator"], models=["*"]), repo) == "created"
        assert await upsert(build_role(group=DEFAULT_GROUP, tools=DEFAULT_TOOLS, models=["*"]), repo) == "updated"

        items = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(f"ROLE#{ROLE_ID}"))["Items"]
        tool_grants = sorted(i["SK"] for i in items if i["SK"].startswith("TOOL_GRANT#"))
        assert tool_grants == ["TOOL_GRANT#ask_user_question", "TOOL_GRANT#calculator"]
        stored = await repo.get_role(ROLE_ID)
        assert stored is not None
        assert stored.granted_tools == ["ask_user_question", "calculator"]
        assert stored.created_at  # preserved by update_role
