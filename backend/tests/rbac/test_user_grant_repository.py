"""Direct user grants in the app-roles table, against a moto DynamoDB.

The grant shares the user's ``USER#<id>`` partition with their picker
preference rows and shares the role reverse-lookup indexes with the role
grant rows. These tests pin the two things that could go wrong there: a
grant write or sweep touching a preferences row, and a user row surfacing
as a role in ``get_roles_for_*``.
"""

import os

import boto3
import pytest
from moto import mock_aws

from apis.shared.rbac.models import AppRole, EffectivePermissions, UserGrant
from apis.shared.rbac.repository import AppRoleRepository

TABLE = "test-app-roles"


def _create_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": n, "AttributeType": "S"}
            for n in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK",
                      "GSI3PK", "GSI3SK", "GSI5PK", "GSI5SK")
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": name,
                "KeySchema": [
                    {"AttributeName": pk, "KeyType": "HASH"},
                    {"AttributeName": sk, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
            for name, pk, sk in (
                ("JwtRoleMappingIndex", "GSI1PK", "GSI1SK"),
                ("ToolRoleMappingIndex", "GSI2PK", "GSI2SK"),
                ("ModelRoleMappingIndex", "GSI3PK", "GSI3SK"),
                ("EntityTypeIndex", "GSI5PK", "GSI5SK"),
            )
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(TABLE)


@pytest.fixture
def repo():
    with mock_aws():
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        table = _create_table()
        # The user's picker preferences row, which lives in the same partition.
        table.put_item(
            Item={"PK": "USER#user-1", "SK": "TOOL_PREFERENCES", "toolPreferences": {"x": True}}
        )
        yield AppRoleRepository(table_name=TABLE), table


def _role(role_id: str, tools, models=(), skills=()) -> AppRole:
    return AppRole(
        role_id=role_id,
        display_name=role_id,
        description="",
        granted_tools=list(tools),
        granted_models=list(models),
        granted_skills=list(skills),
        effective_permissions=EffectivePermissions(
            tools=list(tools), models=list(models), skills=list(skills)
        ),
    )


@pytest.mark.asyncio
async def test_put_then_get_round_trips_and_keeps_created_at(repo):
    r, _ = repo
    stored = await r.put_user_grant(
        UserGrant(user_id="user-1", granted_tools=["browse_web"], note="pilot", granted_by="a@x")
    )
    created = stored.created_at
    assert created

    again = await r.put_user_grant(UserGrant(user_id="user-1", granted_tools=["calculator"]))
    got = await r.get_user_grant("user-1")

    assert got.granted_tools == ["calculator"]
    assert got.created_at == created
    assert again.updated_at >= created


@pytest.mark.asyncio
async def test_get_missing_grant_is_none(repo):
    r, _ = repo
    assert await r.get_user_grant("nobody") is None


@pytest.mark.asyncio
async def test_write_and_delete_leave_the_preferences_row_alone(repo):
    r, table = repo
    await r.put_user_grant(
        UserGrant(user_id="user-1", granted_tools=["browse_web"], granted_models=["m"])
    )
    await r.put_user_grant(UserGrant(user_id="user-1", granted_skills=["s"]))
    assert await r.delete_user_grant("user-1") is True

    rows = table.query(
        KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": "USER#user-1"}
    )["Items"]
    assert [row["SK"] for row in rows] == ["TOOL_PREFERENCES"]


@pytest.mark.asyncio
async def test_replace_rebuilds_the_mapping_rows(repo):
    r, table = repo
    await r.put_user_grant(UserGrant(user_id="user-1", granted_tools=["a", "b"]))
    await r.put_user_grant(UserGrant(user_id="user-1", granted_tools=["b"], granted_models=["m"]))

    rows = table.query(
        KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": "USER#user-1"}
    )["Items"]
    sks = sorted(row["SK"] for row in rows)
    assert sks == ["GRANTS", "GRANT_MODEL#m", "GRANT_TOOL#b", "TOOL_PREFERENCES"]


@pytest.mark.asyncio
async def test_role_reverse_lookups_never_report_a_user_row(repo):
    r, _ = repo
    await r.create_role(_role("staff", tools=["browse_web"], models=["m"], skills=["s"]))
    await r.put_user_grant(
        UserGrant(user_id="user-1", granted_tools=["browse_web"], granted_models=["m"],
                  granted_skills=["s"])
    )

    assert [x["roleId"] for x in await r.get_roles_for_tool("browse_web")] == ["staff"]
    assert [x["roleId"] for x in await r.get_roles_for_model("m")] == ["staff"]
    assert [x["roleId"] for x in await r.get_roles_for_skill("s")] == ["staff"]


@pytest.mark.asyncio
async def test_user_reverse_lookups_report_only_users(repo):
    r, _ = repo
    await r.create_role(_role("staff", tools=["browse_web"], models=["m"], skills=["s"]))
    await r.put_user_grant(
        UserGrant(user_id="user-2", granted_tools=["browse_web"], granted_models=["m"],
                  granted_skills=["s"])
    )
    await r.put_user_grant(UserGrant(user_id="user-1", granted_tools=["browse_web"]))

    assert await r.get_user_ids_for_tool("browse_web") == ["user-1", "user-2"]
    assert await r.get_user_ids_for_model("m") == ["user-2"]
    assert await r.get_user_ids_for_skill("s") == ["user-2"]
    assert await r.get_user_ids_for_tool("nothing") == []


@pytest.mark.asyncio
async def test_list_user_grants_uses_the_entity_index(repo):
    r, _ = repo
    await r.create_role(_role("staff", tools=["browse_web"]))
    await r.put_user_grant(UserGrant(user_id="user-1", granted_tools=["browse_web"]))
    await r.put_user_grant(UserGrant(user_id="user-2", granted_models=["m"]))

    listed = await r.list_user_grants()

    assert sorted(g.user_id for g in listed) == ["user-1", "user-2"]


@pytest.mark.asyncio
async def test_delete_missing_grant_is_false(repo):
    r, _ = repo
    assert await r.delete_user_grant("nobody") is False


@pytest.mark.asyncio
async def test_role_update_sweep_does_not_reach_user_rows(repo):
    """A role edit sweeps ``ROLE#`` mapping rows only; user grants are elsewhere."""
    r, _ = repo
    await r.create_role(_role("staff", tools=["browse_web"]))
    await r.put_user_grant(UserGrant(user_id="user-1", granted_tools=["browse_web"]))

    await r.update_role(_role("staff", tools=["calculator"]))

    assert await r.get_user_ids_for_tool("browse_web") == ["user-1"]
    assert (await r.get_user_grant("user-1")).granted_tools == ["browse_web"]
