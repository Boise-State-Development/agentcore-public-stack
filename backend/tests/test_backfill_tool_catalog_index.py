"""Tests for the tool-catalog EntityTypeIndex backfill.

The stakes are asymmetric: a row this script misses is not stale in the index,
it is absent from a sparse index forever — and once `list_tools` queries that
index, an absent row is a tool that silently vanished from every user's
catalog. So the tests care most about *which rows get stamped* and *which are
left alone*, not just the happy path.
"""

import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from backfill_tool_catalog_index import (  # noqa: E402
    ENTITY_TYPE_TOOL,
    backfill,
    plan_row,
)

REGION = "us-east-1"
TABLE = "test-app-roles"


@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    with mock_aws():
        yield


@pytest.fixture()
def table(aws):
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=TABLE,
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
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _seed_shared_table(table):
    """The app-roles table as it really is — tools mixed with everything else."""
    rows = [
        {"PK": "TOOL#calculator", "SK": "METADATA", "toolId": "calculator"},
        {"PK": "TOOL#web_search", "SK": "METADATA", "toolId": "web_search"},
        # Not tools, and must not be stamped:
        {"PK": "TOOL#calculator", "SK": "CAPABILITIES", "prompts": []},
        {"PK": "SKILL#research", "SK": "METADATA", "skillId": "research"},
        {"PK": "ROLE#student", "SK": "DEFINITION", "roleId": "student"},
        {"PK": "ROLE#student", "SK": "TOOL_GRANT#calculator"},
        {"PK": "USER#u1", "SK": "TOOL_PREFERENCES", "userId": "u1"},
    ]
    for r in rows:
        table.put_item(Item=r)


class TestPlanRow:
    def test_derives_both_keys_from_the_pk(self):
        plan = plan_row({"PK": "TOOL#calculator", "SK": "METADATA"})
        assert plan == {"gsi5pk": ENTITY_TYPE_TOOL, "gsi5sk": "TOOL#calculator"}

    def test_already_stamped_row_needs_nothing(self):
        assert plan_row(
            {"PK": "TOOL#calculator", "SK": "METADATA", "GSI5PK": ENTITY_TYPE_TOOL}
        ) is None

    def test_unexpected_pk_is_skipped_not_guessed(self):
        assert "skip" in plan_row({"PK": "SKILL#research", "SK": "METADATA"})

    def test_empty_tool_id_is_skipped(self):
        assert "skip" in plan_row({"PK": "TOOL#", "SK": "METADATA"})

    def test_key_ignores_a_disagreeing_toolid_attribute(self):
        """Identity comes from the PK, so the index cannot diverge from the
        base table even if `toolId` is wrong or missing."""
        plan = plan_row({"PK": "TOOL#real", "SK": "METADATA", "toolId": "WRONG"})
        assert plan["gsi5sk"] == "TOOL#real"


class TestBackfill:
    def test_dry_run_writes_nothing(self, table):
        _seed_shared_table(table)

        stats = backfill(table, apply=False)

        assert stats["stamped"] == 2
        for tool_id in ("calculator", "web_search"):
            item = table.get_item(
                Key={"PK": f"TOOL#{tool_id}", "SK": "METADATA"}
            )["Item"]
            assert "GSI5PK" not in item

    def test_stamps_only_tool_metadata_rows(self, table):
        _seed_shared_table(table)

        stats = backfill(table, apply=True)

        assert stats["tool_rows"] == 2
        assert stats["stamped"] == 2
        assert stats["skipped"] == 0
        assert stats["failed"] == 0

        for tool_id in ("calculator", "web_search"):
            item = table.get_item(
                Key={"PK": f"TOOL#{tool_id}", "SK": "METADATA"}
            )["Item"]
            assert item["GSI5PK"] == ENTITY_TYPE_TOOL
            assert item["GSI5SK"] == f"TOOL#{tool_id}"

        # Everything that is not a tool stays untouched — the tool partition of
        # the index must contain exactly the tool catalog.
        for key in (
            {"PK": "TOOL#calculator", "SK": "CAPABILITIES"},
            {"PK": "SKILL#research", "SK": "METADATA"},
            {"PK": "ROLE#student", "SK": "DEFINITION"},
            {"PK": "ROLE#student", "SK": "TOOL_GRANT#calculator"},
            {"PK": "USER#u1", "SK": "TOOL_PREFERENCES"},
        ):
            assert "GSI5PK" not in table.get_item(Key=key)["Item"]

    def test_is_idempotent(self, table):
        _seed_shared_table(table)
        backfill(table, apply=True)

        second = backfill(table, apply=True)

        assert second["stamped"] == 0
        assert second["already"] == 2
        assert second["failed"] == 0

    def test_leaves_a_row_the_writer_already_stamped(self, table):
        _seed_shared_table(table)
        table.put_item(
            Item={
                "PK": "TOOL#fresh",
                "SK": "METADATA",
                "toolId": "fresh",
                "GSI5PK": ENTITY_TYPE_TOOL,
                "GSI5SK": "TOOL#fresh",
            }
        )

        stats = backfill(table, apply=True)

        assert stats["already"] == 1
        assert stats["stamped"] == 2

    def test_paginates_past_one_scan_page(self, table):
        """A short table would hide a missing ExclusiveStartKey, and the rows
        beyond page one would be the ones silently dropped from the index."""
        for i in range(120):
            table.put_item(
                Item={"PK": f"TOOL#t{i:03d}", "SK": "METADATA", "toolId": f"t{i:03d}"}
            )

        stats = backfill(table, apply=True)

        assert stats["tool_rows"] == 120
        assert stats["stamped"] == 120


class TestContractWithTheWriter:
    def test_partition_value_matches_the_model(self):
        """The script hardcodes the constant so it can run standalone; this is
        what stops the two drifting."""
        from apis.shared.tools.models import ENTITY_TYPE_TOOL as MODEL_CONSTANT

        assert ENTITY_TYPE_TOOL == MODEL_CONSTANT

    def test_backfilled_keys_match_what_the_writer_would_write(self):
        """A stamped row must be byte-identical to a freshly written one, or
        the index would sort or resolve differently for old vs new tools."""
        from apis.shared.tools.models import ToolDefinition

        written = ToolDefinition(
            tool_id="calculator",
            display_name="Calculator",
            description="d",
            category="utility",
            protocol="local",
        ).to_dynamo_item()

        planned = plan_row({"PK": "TOOL#calculator", "SK": "METADATA"})

        assert planned["gsi5pk"] == written["GSI5PK"]
        assert planned["gsi5sk"] == written["GSI5SK"]
