"""Tests for the artifact tool-merge backfill script.

The script collapses the two artifact catalog rows (``create_artifact`` +
``update_artifact``) into a single "Artifacts" toggle keyed on
``create_artifact``. Its real job is not the rename but the promotion: every
place the retired id could be the *only* thing granting artifact access must
be moved onto the keeper before the retired id is deleted, or a role/user/
assistant silently loses the capability.
"""

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backfill_artifact_tool_merge import (  # noqa: E402
    delete_retired_catalog_row,
    promote_assistant_bindings,
    promote_role_grants,
    promote_user_preferences,
    retitle_catalog_row,
)

REGION = "us-east-1"
ROLES_TABLE = "test-app-roles"
ASSISTANTS_TABLE = "test-assistants"


@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture()
def roles(aws):
    """app-roles table, including the GSI2 the grant lookup queries."""
    ddb = boto3.resource("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=ROLES_TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "ToolRoleMappingIndex",
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table = ddb.Table(ROLES_TABLE)
    table.put_item(
        Item={
            "PK": "TOOL#create_artifact",
            "SK": "METADATA",
            "toolId": "create_artifact",
            "displayName": "Create Artifact",
        }
    )
    table.put_item(
        Item={
            "PK": "TOOL#update_artifact",
            "SK": "METADATA",
            "toolId": "update_artifact",
            "displayName": "Update Artifact",
        }
    )
    return table


@pytest.fixture()
def assistants(aws):
    ddb = boto3.resource("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=ASSISTANTS_TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(ASSISTANTS_TABLE)


def _put_role(table, role_id, granted):
    table.put_item(
        Item={
            "PK": f"ROLE#{role_id}",
            "SK": "DEFINITION",
            "roleId": role_id,
            "grantedTools": list(granted),
            "effectivePermissions": {"tools": list(granted)},
        }
    )
    for tool_id in granted:
        table.put_item(
            Item={
                "PK": f"ROLE#{role_id}",
                "SK": f"TOOL_GRANT#{tool_id}",
                "GSI2PK": f"TOOL#{tool_id}",
                "GSI2SK": f"ROLE#{role_id}",
                "roleId": role_id,
                "displayName": role_id.upper(),
                "enabled": True,
            }
        )


def _definition(table, role_id):
    return table.get_item(Key={"PK": f"ROLE#{role_id}", "SK": "DEFINITION"})["Item"]


def _has(table, pk, sk):
    return "Item" in table.get_item(Key={"PK": pk, "SK": sk})


def _put_prefs(table, user_id, prefs):
    table.put_item(
        Item={"PK": f"USER#{user_id}", "SK": "TOOL_PREFERENCES", "toolPreferences": prefs}
    )


def _prefs(table, user_id):
    return table.get_item(Key={"PK": f"USER#{user_id}", "SK": "TOOL_PREFERENCES"})[
        "Item"
    ]["toolPreferences"]


class TestCatalogRow:
    def test_retitles_keeper_and_deletes_retired(self, roles):
        assert retitle_catalog_row(roles, apply=True) == 1
        assert delete_retired_catalog_row(roles, apply=True) == 1

        item = roles.get_item(Key={"PK": "TOOL#create_artifact", "SK": "METADATA"})[
            "Item"
        ]
        assert item["displayName"] == "Artifacts"
        assert not _has(roles, "TOOL#update_artifact", "METADATA")

    def test_dry_run_does_not_mutate(self, roles):
        retitle_catalog_row(roles, apply=False)
        delete_retired_catalog_row(roles, apply=False)

        item = roles.get_item(Key={"PK": "TOOL#create_artifact", "SK": "METADATA"})[
            "Item"
        ]
        assert item["displayName"] == "Create Artifact"
        assert _has(roles, "TOOL#update_artifact", "METADATA")

    def test_rerun_is_a_noop(self, roles):
        retitle_catalog_row(roles, apply=True)
        delete_retired_catalog_row(roles, apply=True)

        assert retitle_catalog_row(roles, apply=True) == 0
        assert delete_retired_catalog_row(roles, apply=True) == 0


class TestRoleGrants:
    def test_role_holding_only_retired_id_is_promoted(self, roles):
        """The case that would otherwise silently revoke artifact access."""
        _put_role(roles, "a", ["update_artifact", "other"])

        promote_role_grants(roles, apply=True)

        definition = _definition(roles, "a")
        assert sorted(definition["grantedTools"]) == ["create_artifact", "other"]
        assert sorted(definition["effectivePermissions"]["tools"]) == [
            "create_artifact",
            "other",
        ]
        assert _has(roles, "ROLE#a", "TOOL_GRANT#create_artifact")
        assert not _has(roles, "ROLE#a", "TOOL_GRANT#update_artifact")

    def test_role_holding_both_is_just_pruned(self, roles):
        _put_role(roles, "b", ["create_artifact", "update_artifact"])

        promote_role_grants(roles, apply=True)

        assert _definition(roles, "b")["grantedTools"] == ["create_artifact"]
        assert _has(roles, "ROLE#b", "TOOL_GRANT#create_artifact")
        assert not _has(roles, "ROLE#b", "TOOL_GRANT#update_artifact")

    def test_wildcard_role_gains_no_explicit_grant(self, roles):
        """`*` already covers the keeper — don't narrow it to a concrete list."""
        _put_role(roles, "c", ["*"])
        roles.put_item(
            Item={
                "PK": "ROLE#c",
                "SK": "TOOL_GRANT#update_artifact",
                "GSI2PK": "TOOL#update_artifact",
                "GSI2SK": "ROLE#c",
                "roleId": "c",
                "enabled": True,
            }
        )

        promote_role_grants(roles, apply=True)

        assert _definition(roles, "c")["grantedTools"] == ["*"]
        assert not _has(roles, "ROLE#c", "TOOL_GRANT#create_artifact")
        assert not _has(roles, "ROLE#c", "TOOL_GRANT#update_artifact")

    def test_dry_run_does_not_mutate(self, roles):
        _put_role(roles, "a", ["update_artifact"])

        promote_role_grants(roles, apply=False)

        assert _definition(roles, "a")["grantedTools"] == ["update_artifact"]
        assert _has(roles, "ROLE#a", "TOOL_GRANT#update_artifact")


class TestUserPreferences:
    def test_explicit_enable_carries_over(self, roles):
        _put_prefs(roles, "u1", {"update_artifact": True})

        promote_user_preferences(roles, apply=True)

        assert _prefs(roles, "u1") == {"create_artifact": True}

    def test_create_wins_when_both_are_set(self, roles):
        _put_prefs(roles, "u2", {"create_artifact": False, "update_artifact": True})

        promote_user_preferences(roles, apply=True)

        assert _prefs(roles, "u2") == {"create_artifact": False}

    def test_explicit_disable_does_not_carry_over(self, roles):
        """Turning update off never meant "disable artifacts entirely" — the
        user left create at its default-on, so the key just drops."""
        _put_prefs(roles, "u3", {"update_artifact": False})

        promote_user_preferences(roles, apply=True)

        assert _prefs(roles, "u3") == {}

    def test_unrelated_prefs_untouched(self, roles):
        _put_prefs(roles, "u4", {"other": True})

        assert promote_user_preferences(roles, apply=True) == 0
        assert _prefs(roles, "u4") == {"other": True}

    def test_dry_run_does_not_mutate(self, roles):
        _put_prefs(roles, "u1", {"update_artifact": True})

        promote_user_preferences(roles, apply=False)

        assert _prefs(roles, "u1") == {"update_artifact": True}


class TestAssistantBindings:
    def test_sole_retired_binding_is_promoted(self, assistants):
        assistants.put_item(
            Item={
                "PK": "AST#1",
                "SK": "METADATA",
                "bindings": [{"kind": "tool", "ref": "update_artifact"}],
            }
        )

        promote_assistant_bindings(assistants, apply=True)

        item = assistants.get_item(Key={"PK": "AST#1", "SK": "METADATA"})["Item"]
        assert item["bindings"] == [{"kind": "tool", "ref": "create_artifact"}]

    def test_binding_pair_is_deduped(self, assistants):
        assistants.put_item(
            Item={
                "PK": "AST#2",
                "SK": "METADATA",
                "bindings": [
                    {"kind": "tool", "ref": "create_artifact"},
                    {"kind": "tool", "ref": "update_artifact"},
                    {"kind": "skill", "ref": "s"},
                ],
            }
        )

        promote_assistant_bindings(assistants, apply=True)

        item = assistants.get_item(Key={"PK": "AST#2", "SK": "METADATA"})["Item"]
        assert item["bindings"] == [
            {"kind": "tool", "ref": "create_artifact"},
            {"kind": "skill", "ref": "s"},
        ]

    def test_unrelated_bindings_untouched(self, assistants):
        assistants.put_item(
            Item={
                "PK": "AST#3",
                "SK": "METADATA",
                "bindings": [{"kind": "tool", "ref": "other"}],
            }
        )

        assert promote_assistant_bindings(assistants, apply=True) == 0

    def test_dry_run_does_not_mutate(self, assistants):
        assistants.put_item(
            Item={
                "PK": "AST#1",
                "SK": "METADATA",
                "bindings": [{"kind": "tool", "ref": "update_artifact"}],
            }
        )

        promote_assistant_bindings(assistants, apply=False)

        item = assistants.get_item(Key={"PK": "AST#1", "SK": "METADATA"})["Item"]
        assert item["bindings"] == [{"kind": "tool", "ref": "update_artifact"}]
