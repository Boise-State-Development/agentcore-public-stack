"""Tests for seed_system_admin_role and seed_default_tools in seed_bootstrap_data.py."""

import sys
import os
import pytest
import boto3
from moto import mock_aws

# Add the scripts directory to the path so we can import the module
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from seed_bootstrap_data import (  # noqa: E402
    seed_system_admin_role,
    seed_default_tools,
)

TABLE_NAME = "test-app-roles"
REGION = "us-east-1"


@pytest.fixture
def dynamodb_table():
    """Create a mock DynamoDB table matching the app-roles schema."""
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
        yield table


class TestSeedSystemAdminRole:
    def test_creates_role_with_grants(self, dynamodb_table):
        """Creates DEFINITION + TOOL_GRANT#* + MODEL_GRANT#* + JWT_MAPPING#system_admin."""
        result = seed_system_admin_role(TABLE_NAME, REGION)

        assert result.created == 1
        assert result.failed == 0

        # Verify DEFINITION
        resp = dynamodb_table.get_item(
            Key={"PK": "ROLE#system_admin", "SK": "DEFINITION"}
        )
        item = resp["Item"]
        assert item["roleId"] == "system_admin"
        assert item["jwtRoleMappings"] == ["system_admin"]
        assert item["grantedTools"] == ["*"]
        assert item["grantedModels"] == ["*"]
        assert item["isSystemRole"] is True
        assert item["priority"] == 1000

        # Verify TOOL_GRANT#*
        resp = dynamodb_table.get_item(
            Key={"PK": "ROLE#system_admin", "SK": "TOOL_GRANT#*"}
        )
        grant = resp["Item"]
        assert grant["GSI2PK"] == "TOOL#*"
        assert grant["GSI2SK"] == "ROLE#system_admin"
        assert grant["enabled"] is True

        # Verify MODEL_GRANT#*
        resp = dynamodb_table.get_item(
            Key={"PK": "ROLE#system_admin", "SK": "MODEL_GRANT#*"}
        )
        grant = resp["Item"]
        assert grant["GSI3PK"] == "MODEL#*"
        assert grant["GSI3SK"] == "ROLE#system_admin"
        assert grant["enabled"] is True

        # Verify JWT_MAPPING#system_admin (maps Cognito group → AppRole)
        resp = dynamodb_table.get_item(
            Key={"PK": "ROLE#system_admin", "SK": "JWT_MAPPING#system_admin"}
        )
        mapping = resp["Item"]
        assert mapping["GSI1PK"] == "JWT_ROLE#system_admin"
        assert mapping["GSI1SK"] == "ROLE#system_admin"
        assert mapping["roleId"] == "system_admin"
        assert mapping["enabled"] is True

    def test_skips_when_role_exists(self, dynamodb_table):
        """Skips if system_admin DEFINITION already present."""
        seed_system_admin_role(TABLE_NAME, REGION)

        result = seed_system_admin_role(TABLE_NAME, REGION)

        assert result.skipped == 1
        assert result.created == 0


class TestSeedDefaultTools:
    def test_creates_both_tools(self, dynamodb_table):
        """Creates fetch_url_content and create_visualization tool entries."""
        result = seed_default_tools(TABLE_NAME, REGION)

        assert result.created == 2
        assert result.failed == 0

        # Verify fetch_url_content
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#fetch_url_content", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "fetch_url_content"
        assert item["displayName"] == "URL Fetcher"
        assert item["category"] == "search"
        assert item["protocol"] == "local"
        assert item["status"] == "active"
        assert item["enabledByDefault"] is True
        assert item["isPublic"] is False
        assert item["GSI1PK"] == "CATEGORY#search"
        assert item["GSI1SK"] == "TOOL#fetch_url_content"

        # Verify create_visualization
        resp = dynamodb_table.get_item(
            Key={"PK": "TOOL#create_visualization", "SK": "METADATA"}
        )
        item = resp["Item"]
        assert item["toolId"] == "create_visualization"
        assert item["displayName"] == "Charts & Graphs"
        assert item["category"] == "data"
        assert item["enabledByDefault"] is False
        assert item["GSI1PK"] == "CATEGORY#data"
        assert item["GSI1SK"] == "TOOL#create_visualization"

    def test_skips_existing_tools(self, dynamodb_table):
        """Skips tools that already exist."""
        seed_default_tools(TABLE_NAME, REGION)

        result = seed_default_tools(TABLE_NAME, REGION)

        assert result.skipped == 2
        assert result.created == 0

    def test_partial_skip(self, dynamodb_table):
        """Skips only the tool that already exists, creates the other."""
        # Pre-create one tool
        dynamodb_table.put_item(Item={
            "PK": "TOOL#fetch_url_content",
            "SK": "METADATA",
            "toolId": "fetch_url_content",
        })

        result = seed_default_tools(TABLE_NAME, REGION)

        assert result.created == 1
        assert result.skipped == 1
